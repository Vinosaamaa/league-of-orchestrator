"""Recognize repository effects for the existing shared lifecycle policy.

This preflight covers native edits and common shell writes. The provider sandbox
remains authoritative for arbitrary programs; diagnostics and recovery stay usable.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Mapping


EDIT_TOOLS = frozenset({"apply_patch", "edit", "multiedit", "notebookedit", "write", "write_file", "delete"})
SHELL_TOOLS = frozenset({"bash", "exec_command", "shell"})
SHELL_WORDS = re.compile(
    r'''(?:\\[\s\S]|'[^']*'|"(?:\\[\s\S]|[^"\\])*"|[^\s;&|<>'"\\])+|[;&|<>]+|\n|\S'''
)


def _shell_literal(word: str) -> str | None:
    """Decode quoting only. Expansion-dependent words stay unresolved."""
    value: list[str] = []
    quote = None
    index = 0
    while index < len(word):
        char = word[index]
        following = word[index + 1:index + 2]
        if char == "\\" and quote != "'":
            if not following:
                return None
            if quote is None or following in '$`"\\\n':
                if following != "\n":
                    value.append(following)
                index += 2
                continue
        if char in "\"'" and (quote is None or quote == char):
            quote = char if quote is None else None
        elif quote != "'" and (
            char == "`" or (char == "$" and following and
                            (following.isalnum() or following in "_@*#?$!({[-"
                             or (quote is None and following in "'\"")))
        ):
            return None
        elif quote is None and (
            char in "*?" or (char == "[" and "]" in word[index + 1:])
            or (char == "~" and index == 0)
            or (char == "{" and re.search(r"\{[^}]*?(?:,|\.\.)[^}]*\}", word[index:]))
        ):
            return None
        else:
            value.append(char)
        index += 1
    return None if quote else "".join(value)


def _shell_path(word: str, cwd: Path | None) -> Path | None:
    literal = _shell_literal(word)
    if literal is None:
        return None
    path = Path(literal)
    if not path.is_absolute():
        if cwd is None:
            return None
        path = cwd / path
    return path


def _shell_target(word: str, cwd: Path | None) -> bool:
    path = _shell_path(word, cwd)
    return path is None or _repository_path(str(path), Path("/"))


def _repository_path(value: str, cwd: Path) -> bool:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    candidate = candidate.resolve()
    return any((parent / ".git").exists() for parent in (candidate, *candidate.parents))


def repository_implementation(payload: Mapping[str, Any]) -> bool:
    """Inspect tool effects only; never infer authority from caller flags."""

    name = str(payload.get("tool_name", "")).rsplit(".", 1)[-1].casefold()
    inputs = payload.get("tool_input", {})
    if name == "apply_patch" and isinstance(inputs, str):
        inputs = {"input": inputs}
    if not isinstance(inputs, Mapping):
        return False
    raw_cwd = payload.get("cwd")
    if name in SHELL_TOOLS:
        raw_cwd = inputs.get("workdir", raw_cwd)
    cwd = Path(raw_cwd) if isinstance(raw_cwd, str) and Path(raw_cwd).is_absolute() else None
    if name in EDIT_TOOLS:
        targets = [inputs[key] for key in ("path", "file_path", "notebook_path") if isinstance(inputs.get(key), str)]
        if name == "apply_patch":
            patch = inputs.get("patch", inputs.get("input", ""))
            if isinstance(patch, str):
                targets.extend(re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE))
                targets.extend(re.findall(r"^\*\*\* Move to: (.+)$", patch, re.MULTILINE))
        if cwd is None:
            return not targets or any(
                not Path(target).is_absolute() or _repository_path(target, Path("/"))
                for target in targets
            )
        return any(_repository_path(target, cwd) for target in targets) if targets else _repository_path(str(cwd), cwd)
    if name not in SHELL_TOOLS:
        return False
    command = inputs.get("command", inputs.get("cmd"))
    if not isinstance(command, str):
        return False
    segment: list[str] = []
    segments: list[list[str]] = []
    comment = False
    # Keep concatenated quoted fragments in one word, without stripping the
    # distinction between an escaped/single-quoted dollar and an expansion.
    for match in SHELL_WORDS.finditer(command):
        token = match.group()
        if token == "\n":
            comment = False
        elif token.startswith("#"):
            comment = True
        if comment:
            continue
        if token in {";", "&&", "||", "|", "&", "\n"}:
            segments.append(segment)
            segment = []
        else:
            segment.append(token)
    segments.append(segment)
    for words in segments:
        if not words:
            continue
        for index, word in enumerate(words[:-1]):
            if word == ">&":
                descriptor = _shell_literal(words[index + 1])
                if descriptor is not None and (descriptor.isdigit() or descriptor == "-"):
                    continue  # Descriptor duplication/closure is not a file write.
            if word in {">", ">>", ">|", ">&", "&>", "&>>"} and _shell_target(words[index + 1], cwd):
                return True
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if not words:
            continue
        executable = Path(_shell_literal(words[0]) or "").name
        args = words[1:]
        if executable == "cd":
            while args and _shell_literal(args[0]) in {"--", "-L", "-P"}:
                args = args[1:]
            cwd = _shell_path(args[0], cwd) if args and _shell_literal(args[0]) != "-" else None
        elif executable == "git":
            git_cwd = cwd
            while args and _shell_literal(args[0]) == "-C" and len(args) > 2:
                git_cwd = _shell_path(args[1], git_cwd)
                args = args[2:]
            if args and _shell_literal(args[0]) in {"apply", "am"} and _shell_target(".", git_cwd):
                return True
        elif executable in {"apply_patch", "patch"}:
            if _shell_target(".", cwd):
                return True
        elif executable in {"sed", "perl"} and any(
            re.match(r"^-[^-]*i", _shell_literal(arg) or "") or _shell_literal(arg) == "--in-place"
            for arg in args
        ):
            if args and _shell_target(args[-1], cwd):
                return True
        elif executable in {"tee", "touch", "cp", "mv", "rm"}:
            targets = args[-1:] if executable == "cp" else args
            if executable in {"cp", "mv"}:
                for index, arg in enumerate(args):
                    if _shell_literal(arg) in {"-t", "--target-directory"} and index + 1 < len(args):
                        targets = [args[index + 1], *(targets if executable == "mv" else [])]
                    elif arg.startswith("--target-directory="):
                        targets = [arg.split("=", 1)[1], *(targets if executable == "mv" else [])]
            if any(_shell_target(arg, cwd) for arg in targets if not arg.startswith("-")):
                return True
    return False

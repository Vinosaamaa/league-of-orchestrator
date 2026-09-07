"""Recognize repository effects for the existing shared lifecycle policy.

This preflight covers native edits and common shell writes. The provider sandbox
remains authoritative for arbitrary programs; diagnostics and recovery stay usable.
"""

from __future__ import annotations

from pathlib import Path
import re
import shlex
from typing import Any, Mapping


EDIT_TOOLS = frozenset({"apply_patch", "edit", "multiedit", "notebookedit", "write", "write_file", "delete"})
SHELL_TOOLS = frozenset({"bash", "exec_command", "shell"})


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
    if name not in SHELL_TOOLS or cwd is None:
        return False
    command = inputs.get("command", inputs.get("cmd"))
    if not isinstance(command, str):
        return False
    try:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    segment: list[str] = []
    segments: list[list[str]] = []
    for token in tokens:
        if token in {";", "&&", "||", "|", "&"}:
            segments.append(segment)
            segment = []
        else:
            segment.append(token)
    segments.append(segment)
    for words in segments:
        if not words:
            continue
        for index, word in enumerate(words[:-1]):
            if word in {">", ">>"} and _repository_path(shlex.split(words[index + 1])[0], cwd):
                return True
        # Keep quoted punctuation distinct from actual shell redirections.
        executable = Path(shlex.split(words[0])[0]).name
        args = [shlex.split(word)[0] if shlex.split(word) else "" for word in words[1:]]
        if executable == "cd" and args:
            cwd = (cwd / args[0]).resolve()
        elif executable == "git":
            if args[:1] == ["-C"] and len(args) > 2:
                cwd = (cwd / args[1]).resolve()
                args = args[2:]
            if args[:1] in (["apply"], ["am"]) and _repository_path(str(cwd), cwd):
                return True
        elif executable in {"apply_patch", "patch"}:
            if _repository_path(str(cwd), cwd):
                return True
        elif executable in {"sed", "perl"} and any(re.match(r"^-[^-]*i", arg) or arg == "--in-place" for arg in args):
            if args and _repository_path(args[-1], cwd):
                return True
        elif executable in {"tee", "touch", "cp", "mv", "rm"}:
            targets = args[-1:] if executable == "cp" else args
            if any(_repository_path(arg, cwd) for arg in targets if not arg.startswith("-")):
                return True
    return False

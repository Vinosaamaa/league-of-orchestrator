// League's per-process Pi lifecycle bridge. It never rewrites global Pi config.
// @ts-nocheck

import crypto from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { spawnSync } from "node:child_process";

const stateRoot = process.env.LEAGUE_STATE_ROOT;
const watcher = process.env.LEAGUE_WATCHER_COMMAND;
const worktree = process.env.LEAGUE_WORKTREE;
const sandboxProfile = process.env.LEAGUE_PI_SANDBOX_PROFILE;
const paneId = process.env.HERDR_PANE_ID;
const socketPath = process.env.HERDR_SOCKET_PATH;

function exactRoot(value: string | undefined): string | undefined {
  if (!value || !path.isAbsolute(value) || value === "/") return undefined;
  return path.resolve(value);
}

const exactStateRoot = exactRoot(stateRoot);
const exactWorktree = exactRoot(worktree);

function quoted(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

function inside(candidate: string, root: string): boolean {
  const relative = path.relative(root, path.resolve(candidate));
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function canonicalMutationPath(candidate: string): string | undefined {
  try {
    return fs.realpathSync.native(candidate);
  } catch {
    try {
      return path.join(
        fs.realpathSync.native(path.dirname(candidate)),
        path.basename(candidate),
      );
    } catch {
      return undefined;
    }
  }
}

function runWatcher(
  command: string,
  payload: Record<string, unknown>,
): Record<string, unknown> | undefined {
  if (!watcher || !exactStateRoot || !path.isAbsolute(watcher)) return undefined;
  const completed = spawnSync(watcher, [command], {
    encoding: "utf8",
    input: `${JSON.stringify(payload)}\n`,
    env: { ...process.env, LEAGUE_STATE_ROOT: exactStateRoot },
    timeout: 5000,
    maxBuffer: 1024 * 1024,
  });
  if (completed.status !== 0 || completed.error || !completed.stdout) return undefined;
  try {
    const value = JSON.parse(completed.stdout);
    return value && typeof value === "object" ? value : {};
  } catch {
    return undefined;
  }
}

function reportExactSession(sessionId: string): void {
  if (
    process.env.HERDR_ENV !== "1" ||
    !socketPath ||
    !paneId ||
    !sessionId
  ) return;
  const endpoint = process.platform === "win32" ? `\\\\.\\pipe\\${socketPath}` : socketPath;
  const request = {
    id: `league:pi:${Date.now()}:${crypto.randomUUID()}`,
    method: "pane.report_agent_session",
    params: {
      pane_id: paneId,
      source: "league:pi",
      agent: "pi",
      seq: Date.now() * 1000 + 999,
      agent_session_id: sessionId,
    },
  };
  const socket = net.createConnection(endpoint);
  socket.on("error", () => socket.destroy());
  socket.on("connect", () => {
    socket.end(`${JSON.stringify(request)}\n`);
  });
  socket.setTimeout(1500, () => socket.destroy());
}

export default function (pi) {
  let sessionId: string | undefined;
  let inputId: string | undefined;

  function refreshSession(ctx): string | undefined {
    const observed = ctx?.sessionManager?.getSessionId?.();
    sessionId = typeof observed === "string" && observed ? observed : undefined;
    if (sessionId) reportExactSession(sessionId);
    return sessionId;
  }

  pi.on("session_start", (_event, ctx) => {
    refreshSession(ctx);
    pi.setActiveTools(
      pi.getActiveTools().filter((name) =>
        ["read", "grep", "find", "ls", "bash", "edit", "write"].includes(name),
      ),
    );
  });

  pi.on("agent_start", (_event, ctx) => {
    refreshSession(ctx);
  });

  pi.on("input", (event, ctx) => {
    if (event.source === "extension") return { action: "continue" };
    const exactSession = refreshSession(ctx);
    if (!exactSession || typeof event.text !== "string" || !event.text) {
      return { action: "continue" };
    }
    inputId = crypto.randomUUID();
    const captured = runWatcher("pi-input-hook", {
      hook_event_name: "PiInput",
      session_id: exactSession,
      input_id: inputId,
      prompt: event.text,
    });
    if (!captured) {
      ctx.ui.notify("League prompt capture is unavailable; input was not submitted.", "error");
      return { action: "handled" };
    }
    return { action: "continue" };
  });

  pi.on("agent_settled", (_event, ctx) => {
    const exactSession = refreshSession(ctx);
    if (!exactSession || !inputId) return;
    const result = runWatcher("pi-stop-hook", {
      hook_event_name: "PiStop",
      session_id: exactSession,
      input_id: inputId,
    });
    if (!result) {
      inputId = undefined;
      pi.sendUserMessage(
        "League canonical Stop guard is unavailable. Preserve this session and wait for recovery.",
        { deliverAs: "followUp" },
      );
      return;
    }
    const followup = result.followup_message;
    if (typeof followup === "string" && followup) {
      pi.sendUserMessage(followup, { deliverAs: "followUp" });
    }
  });

  pi.on("tool_call", (event, ctx) => {
    if (!exactWorktree || !exactStateRoot || !sandboxProfile) {
      return { block: true, reason: "League Pi sandbox identity is incomplete", terminate: true };
    }
    if (event.toolName === "write" || event.toolName === "edit") {
      const supplied = event.input?.path;
      if (typeof supplied !== "string") {
        return { block: true, reason: "League requires an exact mutation path", terminate: true };
      }
      const lexical = path.isAbsolute(supplied) ? supplied : path.resolve(ctx.cwd, supplied);
      const candidate = canonicalMutationPath(lexical);
      if (
        !candidate ||
        (!inside(candidate, exactWorktree) && !inside(candidate, exactStateRoot))
      ) {
        return { block: true, reason: "League blocked a write outside assignment roots", terminate: true };
      }
      return;
    }
    if (event.toolName === "bash") {
      const command = event.input?.command;
      if (typeof command !== "string" || !command) {
        return { block: true, reason: "League requires an exact shell command", terminate: true };
      }
      event.input.command = [
        "/usr/bin/sandbox-exec",
        "-f",
        quoted(sandboxProfile),
        "-D",
        quoted(`WORKTREE=${exactWorktree}`),
        "-D",
        quoted(`STATE_ROOT=${exactStateRoot}`),
        "/bin/zsh",
        "-lc",
        quoted(command),
      ].join(" ");
      return;
    }
    if (!["read", "grep", "find", "ls"].includes(event.toolName)) {
      return { block: true, reason: "League blocked an undeclared Pi tool", terminate: true };
    }
  });
}

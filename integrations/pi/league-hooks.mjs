// League's profile-loaded Pi hook bootstrap. Launch sandbox and presentation
// remain owned by league-runtime.ts.

import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

export const PROFILE_BOOTSTRAP = "league.provider-hook-bootstrap.v1";

function exactRoot(value) {
  if (!value || !path.isAbsolute(value) || value === "/") return undefined;
  return path.resolve(value);
}

function installedPaths() {
  const home = process.env.HOME || os.homedir();
  return {
    watcher:
      process.env.LEAGUE_WATCHER_COMMAND ||
      (home ? path.join(home, ".local", "bin", "agent-watcher") : undefined),
    stateRoot: exactRoot(
      process.env.LEAGUE_STATE_ROOT ||
        (home ? path.join(home, ".local", "state", "league") : undefined),
    ),
  };
}

function invokeInstalledWatcher(command, payload) {
  const configured = installedPaths();
  if (
    !configured.watcher ||
    !path.isAbsolute(configured.watcher) ||
    !configured.stateRoot
  ) {
    return undefined;
  }
  const completed = spawnSync(configured.watcher, [command], {
    encoding: "utf8",
    input: `${JSON.stringify(payload)}\n`,
    env: { ...process.env, LEAGUE_STATE_ROOT: configured.stateRoot },
    timeout: 5000,
    maxBuffer: 1024 * 1024,
  });
  if (completed.status !== 0 || completed.error || !completed.stdout) {
    return undefined;
  }
  try {
    const value = JSON.parse(completed.stdout);
    return value && typeof value === "object" && !Array.isArray(value)
      ? value
      : undefined;
  } catch {
    return undefined;
  }
}

function sessionIdentity(ctx) {
  const id = ctx?.sessionManager?.getSessionId?.();
  const file = ctx?.sessionManager?.getSessionFile?.();
  return typeof id === "string" &&
    id &&
    typeof file === "string" &&
    path.isAbsolute(file)
    ? { id, file: path.resolve(file) }
    : undefined;
}

function envelope(session, inputId, fields = {}) {
  return {
    league_profile_bootstrap: PROFILE_BOOTSTRAP,
    session_id: session.id,
    session_path: session.file,
    input_id: inputId,
    ...fields,
  };
}

function unavailableInput(ctx) {
  ctx.ui.notify(
    "League prompt capture is unavailable; input was not submitted.",
    "error",
  );
  return { action: "handled" };
}

export function createLeagueHookBootstrap(options = {}) {
  const invoke = options.runWatcher || invokeInstalledWatcher;
  const randomUUID = options.randomUUID || (() => crypto.randomUUID());
  return function registerLeagueHookBootstrap(pi) {
    let currentInput;

    function captureCurrentInput(ctx) {
      const session = sessionIdentity(ctx);
      if (!session || !currentInput) return { state: "unavailable" };
      const result = invoke(
        "pi-input-hook",
        envelope(session, currentInput.id, {
          hook_event_name: "PiInput",
          prompt: currentInput.prompt,
        }),
      );
      if (result?.binding === "unbound") {
        currentInput.bound = false;
        return { state: "unbound" };
      }
      if (result?.binding !== "bound") return { state: "unavailable" };
      currentInput.bound = true;
      return { state: "bound" };
    }

    pi.on("input", (event, ctx) => {
      if (event.source === "extension") return { action: "continue" };
      const session = sessionIdentity(ctx);
      if (!session || typeof event.text !== "string" || !event.text) {
        return { action: "continue" };
      }
      currentInput = { id: randomUUID(), prompt: event.text, bound: false };
      const capture = captureCurrentInput(ctx);
      if (capture.state === "unavailable") return unavailableInput(ctx);
      return { action: "continue" };
    });

    pi.on("tool_call", (event, ctx) => {
      const session = sessionIdentity(ctx);
      if (!session || !currentInput) return;
      if (!currentInput.bound) {
        const capture = captureCurrentInput(ctx);
        if (capture.state === "unbound") return;
        if (capture.state !== "bound") {
          return {
            block: true,
            reason: "League prompt binding is unavailable",
            terminate: true,
          };
        }
      }
      const result = invoke(
        "pi-pre-tool-hook",
        envelope(session, currentInput.id, {
          hook_event_name: "PiToolCall",
          tool_name: event.toolName,
          authorized: true,
        }),
      );
      if (result?.binding === "unbound") {
        currentInput.bound = false;
        return;
      }
      if (result?.binding !== "bound" || result.decision !== "accept") {
        return {
          block: true,
          reason:
            typeof result?.reason_code === "string"
              ? result.reason_code
              : "League pre-mutation authorization is unavailable",
          terminate: true,
        };
      }
    });

    pi.on("agent_settled", (_event, ctx) => {
      const session = sessionIdentity(ctx);
      if (!session || !currentInput) return;
      if (!currentInput.bound) {
        const capture = captureCurrentInput(ctx);
        if (capture.state === "unbound") {
          currentInput = undefined;
          return;
        }
        if (capture.state !== "bound") {
          pi.sendUserMessage(
            "League canonical Stop guard is unavailable. Preserve this session and wait for recovery.",
            { deliverAs: "followUp" },
          );
          return;
        }
      }
      const result = invoke(
        "pi-stop-hook",
        envelope(session, currentInput.id, { hook_event_name: "PiStop" }),
      );
      if (result?.binding === "unbound") {
        currentInput = undefined;
        return;
      }
      if (result?.binding !== "bound") {
        pi.sendUserMessage(
          "League canonical Stop guard is unavailable. Preserve this session and wait for recovery.",
          { deliverAs: "followUp" },
        );
        return;
      }
      const followup = result.followup_message;
      if (typeof followup === "string" && followup) {
        pi.sendUserMessage(followup, { deliverAs: "followUp" });
      } else {
        currentInput = undefined;
      }
    });
  };
}

export default createLeagueHookBootstrap();

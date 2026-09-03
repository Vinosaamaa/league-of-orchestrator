// League's profile-loaded Pi hook bootstrap. Launch sandbox and presentation
// remain owned by league-runtime.ts.

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

export const ACTIVATION_SCHEMA = "league.pi-hook-activation.v1";

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
    session_id: session.id,
    session_path: session.file,
    input_id: inputId,
    ...fields,
  };
}

function launchManaged() {
  return Boolean(
    process.env.LEAGUE_RUNTIME_KIND === "pi" &&
      exactRoot(process.env.LEAGUE_STATE_ROOT) &&
      exactRoot(process.env.LEAGUE_WORKTREE) &&
      /^[0-9a-f]{64}$/.test(process.env.LEAGUE_LAUNCH_DESCRIPTOR_DIGEST || "") &&
      process.env.LEAGUE_LAUNCH_DESCRIPTOR_ID,
  );
}

function activationPath(session) {
  const home = process.env.HOME || os.homedir();
  const profile = exactRoot(
    process.env.PI_CODING_AGENT_DIR ||
      (home ? path.join(home, ".pi", "agent") : undefined),
  );
  if (!profile) return undefined;
  const key = crypto
    .createHash("sha256")
    .update(`${session.id}\0${session.file}`)
    .digest("hex");
  return path.join(profile, "league-bindings", `${key}.json`);
}

function defaultActivationStore() {
  return {
    isManaged(session) {
      const target = activationPath(session);
      if (!target) return false;
      try {
        const stat = fs.lstatSync(target);
        if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 8192) return false;
        const payload = fs.readFileSync(target, "utf8");
        const value = JSON.parse(payload);
        return Boolean(
          value &&
            Object.keys(value).length === 3 &&
            value.schema === ACTIVATION_SCHEMA &&
            value.session_id === session.id &&
            value.session_path === session.file &&
            payload === `${JSON.stringify(value)}\n`,
        );
      } catch {
        return false;
      }
    },
    markManaged(session) {
      const target = activationPath(session);
      if (!target) throw new Error("League Pi activation root is unavailable");
      if (this.isManaged(session)) return;
      const parent = path.dirname(target);
      fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
      const temporary = `${target}.${process.pid}.${crypto.randomUUID()}.tmp`;
      const payload = `${JSON.stringify({
        schema: ACTIVATION_SCHEMA,
        session_id: session.id,
        session_path: session.file,
      })}\n`;
      try {
        fs.writeFileSync(temporary, payload, { flag: "wx", mode: 0o600 });
        fs.renameSync(temporary, target);
        fs.chmodSync(target, 0o600);
      } finally {
        try {
          fs.unlinkSync(temporary);
        } catch {}
      }
      if (!this.isManaged(session)) {
        throw new Error("League Pi activation receipt verification failed");
      }
    },
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
  const rawInvoke = options.runWatcher || invokeInstalledWatcher;
  const invoke = (command, payload) => {
    try {
      return rawInvoke(command, payload);
    } catch {
      return undefined;
    }
  };
  const randomUUID = options.randomUUID || (() => crypto.randomUUID());
  const activation = options.activationStore || defaultActivationStore();
  return function registerLeagueHookBootstrap(pi) {
    let currentInput;
    let stopGuardUnavailable = false;

    function managed(session) {
      return launchManaged() || activation.isManaged(session);
    }

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
      currentInput.managed = true;
      try {
        activation.markManaged(session);
      } catch {
        return { state: "unavailable" };
      }
      currentInput.bound = true;
      return { state: "bound" };
    }

    pi.on("input", (event, ctx) => {
      if (event.source === "extension") return { action: "continue" };
      const session = sessionIdentity(ctx);
      if (!session || typeof event.text !== "string" || !event.text) {
        return { action: "continue" };
      }
      currentInput = {
        id: randomUUID(),
        prompt: event.text,
        bound: false,
        managed: managed(session),
      };
      stopGuardUnavailable = false;
      const capture = captureCurrentInput(ctx);
      if (
        (capture.state === "unavailable" || capture.state === "unbound") &&
        currentInput.managed
      ) {
        currentInput = undefined;
        return unavailableInput(ctx);
      }
      return { action: "continue" };
    });

    pi.on("tool_call", (event, ctx) => {
      const session = sessionIdentity(ctx);
      if (!session) return;
      if (!currentInput) {
        if (!managed(session)) return;
        return {
          block: true,
          reason: "League prompt binding is unavailable",
          terminate: true,
        };
      }
      if (!currentInput.bound) {
        const capture = captureCurrentInput(ctx);
        if (capture.state === "unbound") return;
        if (capture.state !== "bound") {
          if (!currentInput.managed) return;
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
          tool_input: event.input || {},
        }),
      );
      if (result?.binding === "unbound") {
        if (!currentInput.managed) {
          currentInput.bound = false;
          return;
        }
        return {
          block: true,
          reason: "League pre-mutation authorization is unavailable",
          terminate: true,
        };
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
      if (!session) return;
      if (!currentInput) return;
      const pauseForUnavailableGuard = () => {
        if (!stopGuardUnavailable) {
          ctx.ui.notify(
            "League Stop guard is unavailable; session is paused pending watcher recovery.",
            "error",
          );
          stopGuardUnavailable = true;
        }
      };
      if (!currentInput.bound) {
        const capture = captureCurrentInput(ctx);
        if (capture.state === "unbound") {
          if (currentInput.managed) {
            pauseForUnavailableGuard();
            return;
          }
          currentInput = undefined;
          return;
        }
        if (capture.state !== "bound") {
          if (!currentInput.managed) {
            currentInput = undefined;
            return;
          }
          pauseForUnavailableGuard();
          return;
        }
      }
      const result = invoke(
        "pi-stop-hook",
        envelope(session, currentInput.id, { hook_event_name: "PiStop" }),
      );
      if (result?.binding === "unbound") {
        if (currentInput.managed) {
          pauseForUnavailableGuard();
          return;
        }
        currentInput = undefined;
        return;
      }
      if (result?.binding !== "bound") {
        pauseForUnavailableGuard();
        return;
      }
      stopGuardUnavailable = false;
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

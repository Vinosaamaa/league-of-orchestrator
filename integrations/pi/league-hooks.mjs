// League's profile-loaded Pi hook bootstrap. Launch sandbox and presentation
// remain owned by league-runtime.ts.

import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

export const ACTIVATION_SCHEMA = "league.pi-hook-activation.v1";
export const INSTALLED_WATCHER = "__LEAGUE_STABLE_WATCHER__";
const PI_READ_ONLY_TOOLS = new Set(["read", "grep", "find", "ls", "glob"]);

function exactRoot(value) {
  if (!value || !path.isAbsolute(value) || value === "/") return undefined;
  return path.resolve(value);
}

export function installedPaths() {
  const home = process.env.HOME || os.homedir();
  return {
    watcher:
      exactRoot(INSTALLED_WATCHER) ||
      exactRoot(process.env.LEAGUE_WATCHER_COMMAND) ||
      (home ? path.join(home, ".local", "bin", "agent-watcher") : undefined),
    stateRoot: exactRoot(
      process.env.LEAGUE_STATE_ROOT ||
        (home ? path.join(home, ".local", "state", "league") : undefined),
    ),
  };
}

function invokeInstalledWatcherAsync(command, payload) {
  const configured = installedPaths();
  if (
    !configured.watcher ||
    !path.isAbsolute(configured.watcher) ||
    !configured.stateRoot
  ) {
    return Promise.resolve(undefined);
  }
  return new Promise((resolve) => {
    let stdout = "";
    let finished = false;
    let timer;
    const child = spawn(configured.watcher, [command], {
      env: { ...process.env, LEAGUE_STATE_ROOT: configured.stateRoot },
      stdio: ["pipe", "pipe", "ignore"],
    });
    const finish = (value) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      resolve(value);
    };
    timer = setTimeout(() => {
      child.kill("SIGTERM");
      finish(undefined);
    }, 5000);
    child.on("error", () => finish(undefined));
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
      if (Buffer.byteLength(stdout, "utf8") > 1024 * 1024) {
        child.kill("SIGTERM");
        finish(undefined);
      }
    });
    child.on("close", (code) => {
      if (code !== 0 || !stdout) return finish(undefined);
      try {
        const value = JSON.parse(stdout);
        finish(
          value && typeof value === "object" && !Array.isArray(value)
            ? value
            : undefined,
        );
      } catch {
        finish(undefined);
      }
    });
    child.stdin.on("error", () => finish(undefined));
    child.stdin.end(`${JSON.stringify(payload)}\n`);
  });
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
  const rawInvoke =
    options.runPromptWatcher || options.runWatcher || invokeInstalledWatcherAsync;
  const rawPreToolInvoke =
    options.runPreToolWatcher || options.runWatcher || invokeInstalledWatcherAsync;
  const rawStopInvoke =
    options.runStopWatcher || options.runWatcher || invokeInstalledWatcherAsync;
  const invoke = async (command, payload) => {
    try {
      return await rawInvoke(command, payload);
    } catch {
      return undefined;
    }
  };
  const invokeStop = async (command, payload) => {
    try {
      return await rawStopInvoke(command, payload);
    } catch {
      return undefined;
    }
  };
  const invokePreTool = async (command, payload) => {
    try {
      return await rawPreToolInvoke(command, payload);
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

    async function captureCurrentInput(ctx, expectedInput = currentInput) {
      const session = sessionIdentity(ctx);
      if (!session || !expectedInput || currentInput !== expectedInput) {
        return { state: "unavailable" };
      }
      const result = await invoke(
        "pi-input-hook",
        envelope(session, expectedInput.id, {
          hook_event_name: "PiInput",
          prompt: expectedInput.prompt,
        }),
      );
      if (currentInput !== expectedInput) return { state: "changed" };
      if (result?.binding === "unbound") {
        expectedInput.bound = false;
        return { state: "unbound" };
      }
      if (result?.binding !== "bound") return { state: "unavailable" };
      expectedInput.managed = true;
      try {
        activation.markManaged(session);
      } catch {
        return { state: "unavailable" };
      }
      expectedInput.bound = true;
      return { state: "bound" };
    }

    pi.on("input", async (event, ctx) => {
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
        stopCheckInFlight: false,
        stopFollowupPending: false,
      };
      stopGuardUnavailable = false;
      const input = currentInput;
      const capture = await captureCurrentInput(ctx, input);
      if (currentInput !== input) return unavailableInput(ctx);
      if (
        (capture.state === "unavailable" || capture.state === "unbound") &&
        currentInput.managed
      ) {
        currentInput = undefined;
        return unavailableInput(ctx);
      }
      return { action: "continue" };
    });

    pi.on("tool_call", async (event, ctx) => {
      if (PI_READ_ONLY_TOOLS.has(event.toolName)) return;
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
      const input = currentInput;
      if (!input.bound) {
        const capture = await captureCurrentInput(ctx, input);
        if (currentInput !== input) {
          return {
            block: true,
            reason: "League prompt binding changed during authorization",
            terminate: true,
          };
        }
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
      const result = await invokePreTool(
        "pi-pre-tool-hook",
        envelope(session, input.id, {
          hook_event_name: "PiToolCall",
          tool_name: event.toolName,
          tool_input: event.input || {},
        }),
      );
      if (currentInput !== input) {
        return {
          block: true,
          reason: "League prompt binding changed during authorization",
          terminate: true,
        };
      }
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

    pi.on("agent_settled", async (_event, ctx) => {
      const session = sessionIdentity(ctx);
      if (!session) return;
      if (!currentInput) return;
      const input = currentInput;
      if (input.stopFollowupPending) {
        input.stopFollowupPending = false;
        if (currentInput === input) currentInput = undefined;
        return;
      }
      if (input.stopCheckInFlight) return;
      input.stopCheckInFlight = true;
      const pauseForUnavailableGuard = () => {
        if (!stopGuardUnavailable) {
          ctx.ui.notify(
            "League Stop guard is unavailable; session is paused pending watcher recovery.",
            "error",
          );
          stopGuardUnavailable = true;
        }
      };
      try {
        if (!input.bound) {
          const capture = await captureCurrentInput(ctx, input);
          if (currentInput !== input) return;
          if (capture.state === "unbound") {
            if (input.managed) {
              pauseForUnavailableGuard();
              return;
            }
            currentInput = undefined;
            return;
          }
          if (capture.state !== "bound") {
            if (!input.managed) {
              currentInput = undefined;
              return;
            }
            pauseForUnavailableGuard();
            return;
          }
        }
        const result = await invokeStop(
          "pi-stop-hook",
          envelope(session, input.id, { hook_event_name: "PiStop" }),
        );
        if (currentInput !== input) return;
        if (result?.binding === "unbound") {
          if (input.managed) {
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
          input.stopFollowupPending = true;
          try {
            await pi.sendUserMessage(followup, { deliverAs: "followUp" });
          } catch {
            input.stopFollowupPending = false;
          }
        } else {
          currentInput = undefined;
        }
      } finally {
        input.stopCheckInFlight = false;
      }
    });
  };
}

export default createLeagueHookBootstrap();

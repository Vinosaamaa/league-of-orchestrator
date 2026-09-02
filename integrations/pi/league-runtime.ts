// League's per-process Pi lifecycle bridge. It never rewrites global Pi config.
// @ts-nocheck

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

let stateRoot = process.env.LEAGUE_STATE_ROOT;
let watcher = process.env.LEAGUE_WATCHER_COMMAND;
let worktree = process.env.LEAGUE_WORKTREE;
let sandboxProfile = process.env.LEAGUE_PI_SANDBOX_PROFILE;
let paneId = process.env.HERDR_PANE_ID;
let runtimeKind = process.env.LEAGUE_RUNTIME_KIND;
let providerKind = process.env.LEAGUE_PROVIDER_KIND;
let launchRole = process.env.LEAGUE_LAUNCH_ROLE;
let launchPlacement = process.env.LEAGUE_LAUNCH_PLACEMENT;
let callsign = process.env.LEAGUE_CALLSIGN;
let projectCode = process.env.LEAGUE_PROJECT_CODE;
let taskLabel = process.env.LEAGUE_TASK_LABEL;
let routingAlias = process.env.LEAGUE_ROUTING_ALIAS;
let descriptorDigest = process.env.LEAGUE_LAUNCH_DESCRIPTOR_DIGEST;
let descriptorId = process.env.LEAGUE_LAUNCH_DESCRIPTOR_ID;

function exactRoot(value: string | undefined): string | undefined {
  if (!value || !path.isAbsolute(value) || value === "/") return undefined;
  return path.resolve(value);
}

let exactStateRoot = exactRoot(stateRoot);
let exactWorktree = exactRoot(worktree);

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

type SessionIdentity = {
  id: string;
  file: string;
  parentFile?: string;
};

function exactMetadataInputs(): boolean {
  return Boolean(
    paneId &&
      exactStateRoot &&
      runtimeKind === "pi" &&
      (providerKind === "cursor" || providerKind === "codex") &&
      (launchRole === "shotcaller" || launchRole === "champion") &&
      (launchPlacement === "sibling_pane" || launchPlacement === "new_tab") &&
      callsign &&
      projectCode &&
      taskLabel &&
      routingAlias &&
      descriptorDigest &&
      descriptorId,
  );
}

let metadataSeq = Date.now() * 1000;

function reportLeagueMetadata(session: SessionIdentity): void {
  if (!exactMetadataInputs()) return;
  const threadTitle =
    launchRole === "shotcaller"
      ? callsign!
      : `${callsign} · ${projectCode}|${taskLabel}`;
  const source = `league:pi-launch:${descriptorDigest!.slice(0, 16)}`;
  const tokens = [
    `runtime_kind=${runtimeKind}`,
    `provider_kind=${providerKind}`,
    `role=${launchRole}`,
    `placement=${launchPlacement}`,
    `sidebar_name=${callsign}`,
    `project_code=${projectCode}`,
    `task_label=${taskLabel}`,
    `routing_alias=${routingAlias}`,
    `session_id=${session.id}`,
    `session_path=${session.file}`,
    `thread_title=${threadTitle}`,
    "activation_phase=session_started",
    `launch_runtime_kind=${runtimeKind}`,
    `launch_provider_kind=${providerKind}`,
    `launch_role=${launchRole}`,
    `launch_placement=${launchPlacement}`,
    `launch_callsign=${callsign}`,
    `launch_project_code=${projectCode}`,
    `launch_task_label=${taskLabel}`,
    `launch_routing_alias=${routingAlias}`,
    `launch_session_id=${session.id}`,
    `launch_session_path_digest=${crypto.createHash("sha256").update(session.file).digest("hex")}`,
    `launch_descriptor_sha256=${descriptorDigest}`,
    `launch_descriptor_id=${descriptorId}`,
    `launch_state_root=${exactStateRoot}`,
    "launch_activation_phase=session_started",
  ];
  if (session.parentFile) {
    tokens.push(`parent_session_path=${session.parentFile}`);
    tokens.push(
      `launch_parent_digest=${crypto.createHash("sha256").update(session.parentFile).digest("hex")}`,
    );
  }
  const commandArguments = [
    "pane",
    "report-metadata",
    paneId!,
    "--source",
    source,
    "--applies-to-source",
    "herdr:pi",
    "--agent",
    "pi",
    "--display-agent",
    providerKind!,
    "--title",
    threadTitle,
    "--seq",
    String(++metadataSeq),
  ];
  for (const token of tokens) commandArguments.push("--token", token);
  spawnSync("herdr", commandArguments, {
    encoding: "utf8",
    timeout: 5000,
    maxBuffer: 1024 * 1024,
  });
}

export default function (pi) {
  const flags = [
    "pane-id", "state-root", "watcher-command", "worktree", "sandbox-profile",
    "runtime-kind", "provider-kind", "role", "placement", "callsign",
    "project-code", "task-label", "routing-alias", "descriptor-digest",
    "descriptor-id",
  ];
  for (const name of flags) {
    pi.registerFlag(`league-${name}`, {
      description: `League durable launch ${name}`,
      type: "string",
    });
  }
  const supplied = (name: string, fallback: string | undefined) => {
    const value = pi.getFlag(`league-${name}`);
    return typeof value === "string" && value ? value : fallback;
  };
  stateRoot = supplied("state-root", stateRoot);
  paneId = supplied("pane-id", paneId);
  watcher = supplied("watcher-command", watcher);
  worktree = supplied("worktree", worktree);
  sandboxProfile = supplied("sandbox-profile", sandboxProfile);
  runtimeKind = supplied("runtime-kind", runtimeKind);
  providerKind = supplied("provider-kind", providerKind);
  launchRole = supplied("role", launchRole);
  launchPlacement = supplied("placement", launchPlacement);
  callsign = supplied("callsign", callsign);
  projectCode = supplied("project-code", projectCode);
  taskLabel = supplied("task-label", taskLabel);
  routingAlias = supplied("routing-alias", routingAlias);
  descriptorDigest = supplied("descriptor-digest", descriptorDigest);
  descriptorId = supplied("descriptor-id", descriptorId);
  exactStateRoot = exactRoot(stateRoot);
  exactWorktree = exactRoot(worktree);
  let sessionIdentity: SessionIdentity | undefined;
  let inputId: string | undefined;

  function refreshSession(ctx): SessionIdentity | undefined {
    const id = ctx?.sessionManager?.getSessionId?.();
    const file = ctx?.sessionManager?.getSessionFile?.();
    const parentFile = ctx?.sessionManager?.getHeader?.()?.parentSession;
    sessionIdentity =
      typeof id === "string" && id && typeof file === "string" && path.isAbsolute(file)
        ? {
            id,
            file: path.resolve(file),
            parentFile:
              typeof parentFile === "string" && path.isAbsolute(parentFile)
                ? path.resolve(parentFile)
                : undefined,
          }
        : undefined;
    if (sessionIdentity) reportLeagueMetadata(sessionIdentity);
    return sessionIdentity;
  }

  pi.registerCommand("league-sync", {
    description: "Republish exact League session and launch metadata",
    handler: async (_args, ctx) => {
      if (!refreshSession(ctx)) {
        ctx.ui.notify("League session identity is unavailable.", "error");
      }
    },
  });

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
      session_id: exactSession.id,
      session_path: exactSession.file,
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
      session_id: exactSession.id,
      session_path: exactSession.file,
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

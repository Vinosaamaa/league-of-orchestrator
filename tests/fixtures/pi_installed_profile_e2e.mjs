import path from "node:path";
import { pathToFileURL } from "node:url";

const [extensionPath, profileRoot] = process.argv.slice(2);
process.env.PI_CODING_AGENT_DIR = profileRoot;
delete process.env.LEAGUE_RUNTIME_KIND;
delete process.env.LEAGUE_LAUNCH_DESCRIPTOR_DIGEST;
delete process.env.LEAGUE_LAUNCH_DESCRIPTOR_ID;

const extension = await import(pathToFileURL(extensionPath));
const session = {
  id: "session-installed-profile",
  file: path.resolve(profileRoot, "sessions", "exact-child.jsonl"),
};
const captured = new Set();
let promptDeliveries = 0;
let toolAuthorizations = 0;
let stopCalls = 0;

function piInstance() {
  const handlers = new Map();
  const notifications = [];
  const messages = [];
  const pi = {
    on(event, handler) {
      const existing = handlers.get(event) || [];
      existing.push(handler);
      handlers.set(event, existing);
    },
    sendUserMessage(message, options) {
      messages.push({ message, options });
    },
  };
  const ctx = {
    sessionManager: {
      getSessionId: () => session.id,
      getSessionFile: () => session.file,
    },
    ui: {
      notify(message, level) {
        notifications.push({ message, level });
      },
    },
  };
  return { pi, handlers, notifications, messages, ctx };
}

function watcher(command, payload) {
  if (payload.session_id !== session.id || payload.session_path !== session.file) {
    throw new Error("installed profile changed exact Pi session identity");
  }
  if (command === "pi-input-hook") {
    if (!captured.has(payload.input_id)) {
      captured.add(payload.input_id);
      promptDeliveries += 1;
    }
    return { binding: "bound" };
  }
  if (command === "pi-pre-tool-hook") {
    if ("authorized" in payload || payload.tool_name !== "write") {
      throw new Error("installed Pi mutation hook used a fabricated policy field");
    }
    toolAuthorizations += 1;
    return { binding: "bound", decision: "accept", reason_code: "policy_accepted" };
  }
  if (command === "pi-stop-hook") {
    stopCalls += 1;
    return stopCalls === 1
      ? { binding: "bound", followup_message: "durable transition required" }
      : { binding: "bound" };
  }
  throw new Error(`unexpected watcher command: ${command}`);
}

function activate(instance, runWatcher) {
  extension.createLeagueHookBootstrap({
    runWatcher,
    randomUUID: () => "input-installed-profile",
  })(instance.pi);
  for (const event of ["input", "tool_call", "agent_settled"]) {
    if ((instance.handlers.get(event) || []).length !== 1) {
      throw new Error(`installed profile duplicated ${event}`);
    }
  }
}

const launched = piInstance();
activate(launched, watcher);
const launchInput = launched.handlers.get("input")[0](
  { source: "interactive", text: "exact installed launch prompt" },
  launched.ctx,
);
const launchTool = launched.handlers.get("tool_call")[0](
  { toolName: "write", input: { path: "synthetic.txt" } },
  launched.ctx,
);
launched.handlers.get("agent_settled")[0]({}, launched.ctx);
launched.handlers.get("agent_settled")[0]({}, launched.ctx);

const restartedDuringOutage = piInstance();
activate(restartedDuringOutage, () => undefined);
const restartInput = restartedDuringOutage.handlers.get("input")[0](
  { source: "interactive", text: "must fail closed after verified binding" },
  restartedDuringOutage.ctx,
);

const resumed = piInstance();
activate(resumed, watcher);
const resumeInput = resumed.handlers.get("input")[0](
  { source: "interactive", text: "same exact resumed prompt" },
  resumed.ctx,
);
const resumeTool = resumed.handlers.get("tool_call")[0](
  { toolName: "write", input: { path: "synthetic.txt" } },
  resumed.ctx,
);

process.stdout.write(
  JSON.stringify({
    session,
    launchInput,
    launchTool: launchTool ?? null,
    restartInput,
    restartNotifications: restartedDuringOutage.notifications,
    resumeInput,
    resumeTool: resumeTool ?? null,
    promptDeliveries,
    toolAuthorizations,
    stopCalls,
    stopContinuations: launched.messages,
  }),
);

import { pathToFileURL } from "node:url";

const [extensionPath, scenario] = process.argv.slice(2);
const extension = await import(pathToFileURL(extensionPath));
const handlers = new Map();
const calls = [];
const notifications = [];
const messages = [];
const eventLoopTicks = [];
let identifiers = 0;
let inputCalls = 0;
let activationManaged =
  scenario.startsWith("restored-") ||
  scenario === "outage-managed" ||
  scenario === "outage-stop";

const pi = {
  on(event, handler) {
    const registered = handlers.get(event) || [];
    registered.push(handler);
    handlers.set(event, registered);
  },
  async sendUserMessage(message, options) {
    messages.push({ message, options });
    if (scenario === "recursive-followup") {
      await handlers.get("agent_settled")[0]({}, ctx);
    }
  },
};

const ctx = {
  sessionManager: {
    getSessionId: () => "session-pi-bootstrap",
    getSessionFile: () => "/synthetic/pi/session.jsonl",
  },
  ui: {
    notify(message, level) {
      notifications.push({ message, level });
    },
  },
};

function runWatcher(command, payload) {
  calls.push({ command, payload });
  if (scenario === "outage-managed" || scenario === "outage-ordinary") {
    return undefined;
  }
  if (scenario === "outage-stop" && command === "pi-stop-hook") {
    return undefined;
  }
  if (scenario === "unbound") return { binding: "unbound" };
  if (scenario === "promoted" && command === "pi-input-hook" && inputCalls++ === 0) {
    return { binding: "unbound" };
  }
  if (command === "pi-pre-tool-hook") {
    return { binding: "bound", decision: "accept", reason_code: "policy_accepted" };
  }
  if (
    command === "pi-stop-hook" &&
    (scenario === "promoted" || scenario === "recursive-followup") &&
    messages.length === 0
  ) {
    return { binding: "bound", followup_message: "durable transition required" };
  }
  return { binding: "bound" };
}

function runPreToolWatcher(command, payload) {
  if (scenario !== "async-pretool") return runWatcher(command, payload);
  calls.push({ command, payload });
  return new Promise((resolve) => {
    setTimeout(() => {
      eventLoopTicks.push("pretool");
      resolve({ binding: "bound", decision: "accept", reason_code: "policy_accepted" });
    }, 0);
  });
}

function runPromptWatcher(command, payload) {
  if (scenario !== "async-input") return runWatcher(command, payload);
  calls.push({ command, payload });
  return new Promise((resolve) => {
    setTimeout(() => {
      eventLoopTicks.push("input");
      resolve({ binding: "bound" });
    }, 0);
  });
}

extension.createLeagueHookBootstrap({
  runWatcher,
  runPromptWatcher,
  runPreToolWatcher,
  randomUUID: () => `input-${++identifiers}`,
  activationStore: {
    isManaged: () => activationManaged,
    markManaged: () => {
      if (scenario === "activation-write-failure") {
        throw new Error("synthetic activation write failure");
      }
      activationManaged = true;
    },
  },
})(pi);

for (const event of ["input", "tool_call", "agent_settled"]) {
  if ((handlers.get(event) || []).length !== 1) {
    throw new Error(`duplicate or missing ${event} handler`);
  }
}

const invoke = (event, payload) => handlers.get(event)[0](payload, ctx);
const firstInput = await invoke("input", { source: "interactive", text: "first prompt" });
let secondInput;
let tool;
let settled;
let rearmed;
if (scenario === "promoted" || scenario === "outage-stop") {
  tool = await invoke("tool_call", { toolName: "write", input: { path: "file" } });
  settled = await invoke("agent_settled", {});
  rearmed = await invoke("agent_settled", {});
} else {
  tool = await invoke("tool_call", {
    toolName: scenario === "read-only" ? "read" : "write",
    input: scenario === "read-only" ? {} : { path: "file" },
  });
  settled = await invoke("agent_settled", {});
}

process.stdout.write(
  JSON.stringify({
    scenario,
    firstInput,
    secondInput: secondInput ?? null,
    tool: tool ?? null,
    settled: settled ?? null,
    rearmed: rearmed ?? null,
    calls,
    notifications,
    messages,
    eventLoopTicks,
    handlers: Object.fromEntries(
      [...handlers.entries()].map(([event, registered]) => [event, registered.length]),
    ),
  }),
);

import { pathToFileURL } from "node:url";

const [extensionPath, scenario] = process.argv.slice(2);
const extension = await import(pathToFileURL(extensionPath));
const handlers = new Map();
const calls = [];
const notifications = [];
const messages = [];
let identifiers = 0;
let inputCalls = 0;

const pi = {
  on(event, handler) {
    const registered = handlers.get(event) || [];
    registered.push(handler);
    handlers.set(event, registered);
  },
  sendUserMessage(message, options) {
    messages.push({ message, options });
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
  if (scenario === "unbound") return { binding: "unbound" };
  if (scenario === "promoted" && command === "pi-input-hook" && inputCalls++ === 0) {
    return { binding: "unbound" };
  }
  if (command === "pi-pre-tool-hook") {
    return { binding: "bound", decision: "accept", reason_code: "policy_accepted" };
  }
  if (command === "pi-stop-hook" && scenario === "promoted" && messages.length === 0) {
    return { binding: "bound", followup_message: "durable transition required" };
  }
  return { binding: "bound" };
}

extension.createLeagueHookBootstrap({
  runWatcher,
  randomUUID: () => `input-${++identifiers}`,
})(pi);

for (const event of ["input", "tool_call", "agent_settled"]) {
  if ((handlers.get(event) || []).length !== 1) {
    throw new Error(`duplicate or missing ${event} handler`);
  }
}

const invoke = (event, payload) => handlers.get(event)[0](payload, ctx);
const firstInput = invoke("input", { source: "interactive", text: "first prompt" });
let secondInput;
let tool;
let settled;
let rearmed;
if (scenario === "promoted") {
  tool = invoke("tool_call", { toolName: "write", input: { path: "file" } });
  settled = invoke("agent_settled", {});
  rearmed = invoke("agent_settled", {});
} else {
  tool = invoke("tool_call", { toolName: "read", input: {} });
  settled = invoke("agent_settled", {});
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
    handlers: Object.fromEntries(
      [...handlers.entries()].map(([event, registered]) => [event, registered.length]),
    ),
  }),
);

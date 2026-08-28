# Research-backed orchestration and model routing policy

**Issue:** [#36](https://github.com/Vinosaamaa/league-of-orchestrator/issues/36)

**Accepted clarifications:** [parent-request progress](https://github.com/Vinosaamaa/league-of-orchestrator/issues/36#issuecomment-5457951668) and [three orchestration outcomes](https://github.com/Vinosaamaa/league-of-orchestrator/issues/36#issuecomment-5457975122)

**Research date and source access date:** 2026-08-28

**Scope:** repository-local policy and tests only; no live installation, cutover,
learned router, planner, scheduler, or additional hierarchy.

## Decision

League should keep orchestration routing and model routing separate. Within
orchestration, owner routing and execution routing are also separate fields:

1. **Orchestration routing** combines an owner-routing field, which keeps the
   request local or offers it to a durable Squad's current Shotcaller, with an
   execution-routing field, which lets the resulting owner act directly or
   require a visible Champion.
2. **Model routing** resolves semantic task, risk, verification, and capability
   signals through versioned provider configuration to one provider/model/effort
   target.

The composed, visible-owner orchestration outcomes are `local_direct`,
`local_champion`, and `squad_route`. A recorded `hidden` scientist is a fourth
local execution mode, but never a direct execution outcome or owner route. A
`squad_route` decides ownership only;
after acknowledging the transfer, the target Shotcaller makes its own direct
versus Champion execution decision. Champion task delivery and parent-request
progress are likewise different records: child transitions go to the
coordinating owner, while only the owner emits bounded request-level updates to
the original requester.

The accepted unique-strong-match clarification supersedes the earlier blanket
prohibition on automatic project-based cross-Shotcaller routing. Project
suggestions alone, ties, and mixed-domain evidence remain insufficient; one
deterministic strong eligible Squad may now receive an ownership offer, and the
transfer is still ineffective until acknowledgement.

The reviewed frameworks do not define a universal delegation threshold. OpenAI
documents manager-owned agents-as-tools, ownership-transferring handoffs, and
code-controlled orchestration as different selectable patterns; LangChain says
that a single agent can often handle a complex task and distinguishes a
stateful supervisor from a one-step router; AutoGen recommends starting with a
single agent for simpler work and moving to a team when it proves inadequate
([OpenAI Agents SDK orchestration](https://openai.github.io/openai-agents-python/multi_agent/),
[LangChain multi-agent overview](https://docs.langchain.com/oss/python/langchain/multi-agent),
[AutoGen teams](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/teams.html)).
Therefore `direct-tiny` versus `worker-required` is an enforceable League
contract, not a framework fact.

Tool count is only a reassessment trigger. AutoGen supports a single agent with
multiple tool iterations, LangChain's pattern comparison shows that tool/model
call counts vary by architecture, and Anthropic documents that effort itself
can change how many tool calls a model makes
([AutoGen teams](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/teams.html),
[LangChain multi-agent overview](https://docs.langchain.com/oss/python/langchain/multi-agent),
[Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort)).
One consequential call can require durable ownership while several coordination
calls may not. The pre-declared bounds and action class, not a raw tool counter,
decide ownership.

## Merged baseline audit

| Decision | Existing repository behavior to preserve | Issue #36 gap |
| --- | --- | --- |
| Orchestration | The merged #4 slice classifies `direct`, `hidden`, or `champion`; rejects repository initialization/write, configuration write, migration, supervised test, and long-running work before a direct action; persists the reason and explicit model/effort/route inputs; and activates a Champion only from an exact verified receipt. See [`sqlite_request_ops.py`](../../src/league/sqlite_request_ops.py), [`request_services.py`](../../src/league/request_services.py), and the [assignment-receipt schema](../../schema/league-assignment-receipt.schema.json). | `question`, `short-check`, and `read-only` are still coarse categories. The gate does not encode every `direct-tiny` bound, continuation and explicit Shotcaller precedence, expansion transfer, or a bounded reason-code vocabulary. `explicit_route` is recorded but not resolved by this classifier. |
| Model | The merged #14 repository-local candidate has provider-neutral semantic tiers, a configured strongest worker baseline, evaluation-gated fast-worker selection, exact explicit model/effort preservation, one atomic escalation child, and outcome success/corrections/latency/cost. See [`routing.py`](../../src/league/routing.py), [routing configuration](../../config/agent-routing.example.json), and [`test_model_routing.py`](../../tests/test_model_routing.py). | It lacks an explicit provider field, policy version on each decision, structured task/risk/verification/capability signals, quality and correction thresholds for downgrade evidence, capability compatibility, bounded reason codes, and expiring operator overrides. |
| Squad ownership | The merged roster has durable Squad records with a replaceable current Shotcaller, while the project catalog stores ordered, many-to-many advisory suggestions. See [`ARCHITECTURE.md`](../ARCHITECTURE.md), [`sqlite_store.py`](../../src/league/sqlite_store.py), and [`sqlite_project_ops.py`](../../src/league/sqlite_project_ops.py). | Owner routing and execution mode are not yet independent decisions. A suggestion alone is insufficient; v1 needs explicit/continuation precedence, strong-evidence matching, accepting/live/capability filters, unique-result enforcement, and acknowledgement-gated transfer. |
| Progress delivery | Champion task transitions are durably addressed to their coordinating Shotcaller through an event and outbox written in one transaction; exact recipient receipts make retries idempotent. See [`sqlite_assignment_ops.py`](../../src/league/sqlite_assignment_ops.py), [`request_services.py`](../../src/league/request_services.py), and [`sqlite_store.py`](../../src/league/sqlite_store.py). | Child delivery is not parent-request progress. The owner needs immediate material events, coalesced changed routine progress, requester outbox durability, and a deduplicated overdue obligation without synthesizing a status. |
| Hidden scientist | The merged dispatch classifier recognizes `hidden`, and the inherited filesystem baseline has a hidden-worker callsign pool. | SQLite does not yet bind a hidden dispatch to durable assignment launch states, exact runtime/model/effort/budgets, terminal-only result delivery, cleanup receipts, or separate visible-Champion promotion. |

These are extensions of proven behavior, not a replacement state machine. The
current request owner, exact Champion activation receipt, and assignment-neutral
model-routing evidence remain authoritative.

## Evidence boundaries

The sources establish architectural options and useful mechanics, but not the
League-specific gate:

| Evidence layer | Primary-source finding | League consequence |
| --- | --- | --- |
| Architecture | Agents-as-tools return a specialist result to the manager, while a handoff changes the active owner. LangChain similarly separates centralized subagents from persistent state-driven handoffs ([OpenAI orchestration](https://openai.github.io/openai-agents-python/multi_agent/), [LangChain subagents](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents), [LangChain handoffs](https://docs.langchain.com/oss/python/langchain/multi-agent/handoffs)). | Hidden advice and visible ownership are different roles. Hidden advice may support the owner but cannot satisfy a visible-Champion requirement. |
| Deterministic control | OpenAI says code orchestration is more deterministic and predictable, registers handoff destinations explicitly, and can resume a serialized `RunState`; LangChain handoffs persist an active-state variable across turns ([OpenAI orchestration](https://openai.github.io/openai-agents-python/multi_agent/), [OpenAI handoffs](https://openai.github.io/openai-agents-python/handoffs/), [OpenAI running agents](https://openai.github.io/openai-agents-python/running_agents/), [LangChain handoffs](https://docs.langchain.com/oss/python/langchain/multi-agent/handoffs)). | Use a small explicit policy before action, honor an exact route, and return continuation to the durable owner. Do not add a planner or model-selected hierarchy. |
| Stable owner, replaceable endpoint | Temporal distinguishes a stable application-level Workflow ID from changing Run IDs and warns against making logical choices from the current Run ID. Kubernetes Services keep a stable abstraction while selected Pods change, and readiness removes an unready Pod from Service traffic ([Temporal Workflow and Run IDs](https://docs.temporal.io/workflow-execution/workflowid-runid), [Kubernetes Services](https://kubernetes.io/docs/concepts/services-networking/service/), [Kubernetes readiness probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/)). | Keep Squad identity durable and resolve its current accepting, live Shotcaller at offer/delivery time. A disposable runtime or agent instance is not the canonical Squad identity. These are analogies; League defines its own fences and receipt. |
| Capability discovery and acknowledgement | A2A Agent Cards declare identity, skills, and capabilities; its `submitted` task state means received and acknowledged. Temporal's `Accepted` Update stage waits for Worker contact and persistence, and OpenAI handoff validation can fail before a transfer proceeds ([A2A v1.0.1](https://github.com/a2aproject/A2A/blob/v1.0.1/docs/specification.md), [Temporal message passing](https://docs.temporal.io/encyclopedia/workflow-message-passing), [OpenAI handoffs](https://openai.github.io/openai-agents-python/handoffs/)). | Filter automatic candidates by current capabilities and do not change canonical ownership until the target acknowledges the exact offer. These sources do not define a League ownership receipt, confidence threshold, or Squad selection order. |
| Non-exclusive metadata | Kubernetes labels are non-unique, multi-dimensional selectors rather than a hierarchy; DMN's `Unique` decision-table policy requires non-overlapping rules and one matching output ([Kubernetes labels and selectors](https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/), [DMN 1.5](https://www.omg.org/spec/DMN/1.5/PDF)). | Project/domain mappings remain many-to-many advice. The table must produce only one deterministic strong eligible result; a tie, weak suggestion, mixed domain, or cross-cutting request stays local. Neither source defines League's evidence order. |
| Durable event delivery | A2A distinguishes task status updates, artifacts, input-required states, and terminal events but warns that transient Messages are not reliable critical delivery. The transactional-outbox pattern writes state and notification atomically and expects duplicate-safe consumers; CloudEvents makes `source` plus `id` a duplicate identity ([A2A v1.0.1](https://github.com/a2aproject/A2A/blob/v1.0.1/docs/specification.md), [AWS transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html), [CloudEvents 1.0.2](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md)). | Persist each required request event with its recipient outbox in one transaction and deduplicate by request generation and recipient. Atomic recording does not itself prove receipt. |
| Coalescing without invented progress | Alertmanager groups and deduplicates alerts with explicit wait/interval controls. An etcd watch progress notification contains zero events when nothing changed ([Prometheus Alertmanager](https://prometheus.io/docs/alerting/latest/alertmanager/), [Alertmanager configuration](https://prometheus.io/docs/alerting/latest/configuration/), [etcd v3.7 API](https://etcd.io/docs/v3.7/learning/api/)). | Immediate League classes bypass coalescing; routine changed progress is coalesced. An overdue `request_progress_due` obligation may truthfully contain no progress event. Alertmanager's repeat behavior is not adopted as a heartbeat rule. |
| Public event surface | CloudEvents recommends compact events and selective disclosure and warns that context attributes may be logged. OpenTelemetry recommends data minimization and allowlist/redaction controls; Temporal warns that user-defined identifiers appear in UIs, history, and logs ([CloudEvents 1.0.2](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md), [OpenTelemetry sensitive data](https://opentelemetry.io/docs/security/handling-sensitive-data/), [Temporal Workflow and Run IDs](https://docs.temporal.io/workflow-execution/workflowid-runid)). | Requester events contain a bounded allowlisted summary and counts, never raw child runtime, thread, worktree, path, credential, or private endpoint details. |

## Decision 1: orchestration routing v1

A Squad is durable routing/domain metadata: stable `squad_id`, active state,
current disposable Shotcaller, intake state and owner fence, bounded domain and
project references, and capabilities. Squad membership and project affinity
are many-to-many and advisory. A Squad may cover multiple projects and a
project may suggest multiple Squads without imposing exclusive membership.
The v1 registration schema deliberately permits only one primary active Squad
per live Shotcaller; this is a deterministic routing constraint, not an
authorization boundary, and the Shotcaller may still handle other projects.

Classify owner routing first, before the first task-action call:

1. **Honor an explicit owner route.** An exact user-selected Squad or
   Shotcaller is authoritative when its durable Squad identity resolves to a
   valid current target. An invalid or unavailable exact target refuses rather
   than falling through to inference.
2. **Continue durable ownership.** If the request already belongs to a Squad,
   return to that stable Squad and resolve its current Shotcaller. This survives
   Shotcaller rollover; a stale agent identity is not a new owner.
3. **Evaluate strong registered evidence.** With neither explicit nor
   continuation ownership, derive candidates only from registered project IDs,
   request history, referenced issues/tasks, and bounded project/domain
   metadata. Ordered project suggestions are evidence inputs, not authority.
4. **Filter mechanically.** Retain only active, accepting Squads whose current
   Shotcaller is live and whose declared capabilities include every request
   requirement.
5. **Offer only one deterministic strong match.** Exactly one eligible strong
   candidate produces `squad_route`. Unknown, weak, tied, mixed-domain,
   cross-project, or cross-cutting evidence keeps ownership with the current
   Shotcaller. Never pick the first or highest advisory suggestion.
6. **Gate transfer on acknowledgement.** Persist the offer event and target
   outbox atomically. Canonical owner and return-to fields do not change until
   the selected current Shotcaller acknowledges that exact offer. Rejection,
   timeout, or rollover leaves the original owner authoritative and requires a
   fresh decision; it does not silently try the next candidate.

After ownership is settled, that owner classifies execution independently:

1. **Honor an explicit Champion route.** A valid exact Champion selection is
   `local_champion` and still requires its verified visible assignment receipt.
2. **Allow `local_direct` only when every `direct-tiny` bound is true.** The work
   is pre-bounded, read-only, answer-or-routing-only, expected within five
   minutes and at most two task-action calls, and requires no artifact,
   mutation, reproduction, test, benchmark, browser/computer workflow, or
   project-specific implementation.
3. **Otherwise use `local_champion`.** Record `worker-required`, then obtain and
   persist the exact verified visible-Champion assignment receipt before
   mutation, test, benchmark, reproduction, or other substantive action.
4. **Transfer expanding direct work at a safe boundary.** Preserve evidence,
   stop before the next substantive call, record the expansion, and obtain the
   visible receipt. The second task-action call is a mandatory reassessment
   point, not permission for a third call.

A `hidden` dispatch is a recorded short-lived scientist assignment under the
current Shotcaller, not Shotcaller-direct execution. It reuses the same durable
`prepare` → `launching` → `active` assignment states with role-specific
validation: parent request, owner Shotcaller, bounded read-only subtask, exact
hidden-worker agent/runtime, model, effort, routing reason, one-to-five-minute
budget, and one-or-two-action scope budget. It uses the hidden-worker callsign
queue and requires no issue, repository, branch, worktree, or PR lifecycle.

Hidden scientists never become the request owner, enter the visible Champion
Roster, or satisfy a required Champion receipt. They emit no routine progress
or heartbeat; only cleanup-gated `completed`, `blocked`, `failed`, or
`promotion_required` delivery reaches the coordinator. Runtime reconciliation
durably fences a stale worker into cleanup-pending without inventing progress.
If the work becomes substantive, mutating, benchmark, reproduction, test,
browser/computer, project, or explicitly visible-worker-required, the scientist
stops and a new linked visible Champion assignment is created. The role is
never upgraded in place; the bounded scientist result is handoff evidence.

Store canonical owner routing separately from execution routing. Owner evidence
includes requester, current owner, return-to owner, optional Squad, policy
version, bounded reason, evidence class, and confidence class. Execution stores
direct or Champion under that owner; `squad_route` never pre-decides the target
owner's execution mode.

Use one primary orchestration reason code per decision:

| Code | Meaning |
| --- | --- |
| `explicit_squad` | Exact valid user-selected Squad route offered. |
| `continuation_squad` | Existing durable Squad ownership resumed. |
| `unique_strong_squad` | One strong eligible automatic match was offered. |
| `explicit_champion` | Exact valid Champion execution selected locally. |
| `direct_tiny` | Every direct bound passed. |
| `worker_required` | At least one direct bound failed. |
| `hidden_scientist` | Exact bounded read-only scientist support was recorded. |

The outcome (`local_direct`, `local_champion`, or `squad_route`) is not the
reason. Structured evidence, candidate/filter results, direct-bound results,
acknowledgement state, and safe-boundary transfer state carry detail; free-form
prose does not create reason codes.

### Child delivery and parent-request progress

Champion task delivery remains internal to the owner boundary: every material
child transition is immediately committed and queued in an outbox addressed
only to its coordinating Shotcaller. The Shotcaller consumes those transitions
and decides whether a separate request-level event is owed to the original
requester.

The owner emits once immediately for route accepted/rejected or owner
unavailable; awaiting-user/authority; parent-critical blocker; acceptance or
safety risk; material scope, target, authority, cost, deadline, owner, or
reroute change; and resolved/failed/cancelled. A child-local recoverable blocker
is routine unless it threatens the parent request. Child started/working,
tests/CI/PR milestones, partial completion, locally recovered blockers, and
count/phase/next-action changes are aggregated per request generation and
recipient behind a configurable default 15-minute minimum. Unchanged liveness,
tool chatter, repeated test results, duplicate/subsumed transitions, and an
unchanged fingerprint emit nothing. An immediate or final event clears the
pending routine aggregate.

Each required request event and its recipient outbox commit atomically and have
one uniqueness key over request, progress generation, and recipient. Retries
reuse that identity. The outbox remains durable while the requester is offline,
and exact receipt completes delivery. The event carries only settled/total
child counts, current phase, blocker count and severity,
`user_action_required`, deadline change, and a bounded next action. Source
event references stay internal and raw runtime, thread, session, worktree,
path, email, credential, or endpoint details never enter the requester payload.

Only changed routine progress buffered past its due time, or an expired promised
checkpoint, creates `request_progress_due` for the owner. After a configurable
default five-minute grace, reconciliation emits one overdue/stalled notice to
the requester; a stale owner/runtime lease is immediately stalled. No buffered
change means no due obligation. These classes, intervals, fingerprint, and
public projection are League policy, not A2A, CloudEvents, or AWS rules.

### Safe Squad registration

`league squad register` creates an idempotent expiring offer tied to one exact
verified live Shotcaller agent/runtime and optional project IDs; it cannot
activate routing. `league squad accept` verifies the same agent/runtime and
atomically creates the stable active Squad, accepting intake fence, immutable
event, and requester outbox. Rejection or expiry creates no active Squad.
`league squad status` reports the bounded offer/Squad state. An existing active
owner is replaced only through guarded `league rollover`; registration never
overwrites it.

## Decision 2: model routing v1

Provider APIs expose model and effort controls differently. The OpenAI Agents
SDK permits model/provider selection per run or per agent and warns that
features vary across providers; OpenAI, Anthropic, and Google expose different
effort names, defaults, and support matrices
([OpenAI Agents SDK models](https://openai.github.io/openai-agents-python/models/),
[OpenAI GPT-5.6 guidance](https://developers.openai.com/api/docs/guides/latest-model),
[Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort),
[Gemini thinking levels](https://ai.google.dev/gemini-api/docs/thinking)).
Anthropic also distinguishes pinned model IDs from convenience aliases, showing
why a current provider mapping must be versioned rather than embedded in core
lifecycle logic
([Claude model IDs and versioning](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions)).

Resolve a model target in this order:

1. **Explicit fields first.** Preserve every explicit user `provider`, `model`,
   and `effort` field exactly. Infer only unspecified fields. If the explicit
   tuple cannot provide a required capability, refuse it as unsupported; never
   silently replace an explicit value.
2. **Active operator override second.** For still-unspecified fields, apply the
   highest-priority matching override only when
   `starts_at <= chosen_at < expires_at`. Both timestamps are offset-qualified
   RFC 3339 instants ([RFC 3339](https://www.rfc-editor.org/rfc/rfc3339.html)).
   Expired overrides are ignored automatically.
3. **Semantic policy third.** Map structured signals—coordination/synthesis,
   bounded/checkable work, ambiguity, impact, verification strength, required
   capabilities, and concrete tool/schema failure—through the selected policy
   version. Core lifecycle records carry semantic tier, signals, policy
   version, and configured target key; the dedicated routing decision records
   the resolved provider/model/effort evidence.
4. **Capability gate before execution.** Filter inferred targets by declared
   capabilities. Select the strongest compatible configured target and record
   a capability fallback. If none is compatible, refuse before launch.
5. **Reliability baseline before optimization.** Default to the strongest
   reliability-qualified compatible target for that role. Coordinator and
   worker baselines may differ. A faster/cheaper target is eligible only while
   representative evaluation evidence meets the configured success/quality and
   correction thresholds.
6. **One evidence-triggered escalation.** At the next safe boundary, concrete
   ambiguity, conflicting evidence, failed acceptance, tool/schema failure, or
   high-impact uncertainty may create one stronger retry. A second failure,
   absence of a stronger compatible target, or an explicit pin that prevents a
   stronger retry produces `blocked`; it never loops.

OpenAI recommends task-specific evaluations that reflect real distributions,
defined success criteria, automated scoring where possible, and continuous
evaluation. It explicitly says the decision to add a multi-agent architecture
should be driven by evals
([OpenAI evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)).
OpenAI's current GPT-5.6 guidance likewise says to compare effort settings on
representative workloads and use high or xhigh only when they produce a
measured quality gain
([GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/latest-model)).

Primary routing research supports the possibility of savings but also the
conservative gate. RouteLLM learns strong/weak routing from preference data,
yet its routers trained only on Arena data performed near or below random on
out-of-distribution MMLU and GSM8K until augmented with relevant data; the paper
says real application distributions may differ substantially from its
benchmarks ([RouteLLM, ICLR 2025](https://openreview.net/forum?id=8sSqNntaMr)).
The newer LLMRouterBench evaluation covers more than 400,000 instances across
21 datasets and 33 models and finds that several recent and commercial routers
do not reliably beat a simple baseline, with persistent model-recall failures
([LLMRouterBench, Findings of ACL 2026](https://aclanthology.org/2026.findings-acl.1881/)).
Those results justify representative evidence and a strong fallback; they do
not justify shipping a learned router in v1.

Use one primary model-routing reason code per decision:

| Code | Meaning |
| --- | --- |
| `explicit_override` | At least one exact user field was preserved. |
| `operator_override` | An active time-bounded override filled an unspecified field. |
| `coordination_baseline` | Coordination uses the configured reliable coordinator tier. |
| `reliability_baseline` | No accepted downgrade evidence applied. |
| `evidence_downgrade` | Representative evidence passed configured thresholds. |
| `provider_capability_fallback` | An inferred target lacked a required capability. |
| `failure_escalation` | One concrete failure triggered the stronger retry. |
| `escalation_exhausted` | The single retry was unavailable or already used. |

Unsupported explicit or inferred capabilities refuse with bounded storage
codes; they do not create a routing decision that claims a selected target.

Every decision records `policy_version`, semantic tier and signals, configured
target key, resolved provider/model/effort, explicit-field flags, override ID
and expiry when used, fallback/escalation parent and count, and the bounded
reason code. Outcome evidence records success, corrections, latency, and cost
when available; missing optional cost is not fabricated.

### Time-bounded Sol-xhigh operator policy

The current OpenAI model documentation identifies `gpt-5.6-sol` as the
flagship-capability target and lists `xhigh` as a supported reasoning effort
([GPT-5.6 Sol model](https://developers.openai.com/api/docs/models/gpt-5.6-sol),
[GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/latest-model)).
Represent today's policy in provider configuration, not lifecycle code:
[`league-model-routing.example.json`](../../config/league-model-routing.example.json)
is the repository-local example; the inherited watcher schema-2 configuration
remains byte-preserved for compatibility.

| Field | Value |
| --- | --- |
| Override ID | `sol-xhigh-2026-08-28` |
| Active interval | `2026-08-28T00:00:00-07:00` inclusive to `2026-08-29T00:00:00-07:00` exclusive |
| Provider/model/effort | `openai` / `gpt-5.6-sol` / `xhigh` |
| Precedence | Fills only unspecified fields after exact user overrides |
| Safety | Still subject to required-capability validation; ignored automatically after expiry |

This is an operator policy for 2026-08-28, not a permanent claim that Sol-xhigh
is universally strongest or optimal.

## Table-driven decision corpus

Keep owner routing, local execution, progress propagation, and model routing as
separate expected fields even when one scenario exercises several.

### Owner and execution routing

| Case | Input distinction | Expected decision |
| --- | --- | --- |
| Explicit Squad override | User supplies one valid exact Squad | `squad_route` with `explicit_squad`; offer that Squad and await exact acknowledgement. |
| Explicit route unavailable | Exact target is closed, stale, or lacks required capability | Refuse the exact route; do not infer another Squad. |
| Durable continuation | Follow-up names an existing Squad-owned request after Shotcaller rollover | `squad_route` with `continuation_squad`; address the Squad's current accepting Shotcaller. |
| Interview Prep sole match | Registered project/issue history gives one strong eligible domain match | `squad_route` with `unique_strong_squad`; ownership changes only after acknowledgement. |
| Advisory-only match | A project merely lists a primary suggestion without strong request evidence | Keep current owner; classify local execution. |
| Multiple Squads | Two eligible strong candidates remain | Keep current owner; never use suggestion order as a tiebreaker. |
| Stale or unavailable owner | The sole evidence match has no live accepting current Shotcaller | Keep current owner; no automatic fallback to another Squad. |
| Capability mismatch | Strong candidate lacks one required capability | Filter it; route only if exactly one other strong eligible candidate remains. |
| Cross-project work | Evidence is mixed-domain or cross-cutting | Keep current owner; no cross-domain guess. |
| Squad rollover before receipt | Offered Squad changes current Shotcaller before acknowledgement | Leave ownership unchanged; supersede and re-offer against the new fence. |
| All direct bounds | Current owner sees every `direct-tiny` predicate pass | `local_direct` with `direct_tiny`. |
| One failed bound | Any artifact, mutation, reproduction, test, benchmark, browser/computer use, project implementation, time, or call bound fails | `local_champion` with `worker_required` and the exact visible receipt. |
| Explicit Champion | Valid exact Champion route | `local_champion` with `explicit_champion`; receipt remains mandatory. |
| Hidden advice | Worker-required task also has hidden advisory output | `local_champion`; advice never substitutes for visible ownership. |
| Hidden-safe scientist | Bounded read-only support has exact identity/model/effort/budgets | Record `hidden`; reuse assignment prepare/launch/activate and deliver terminal-only. |
| Hidden scope expansion | Scientist reaches mutation, test, benchmark, reproduction, browser, project, or substantive work | Stop, create a linked visible Champion, and finish as `promotion_required`; never change role in place. |
| Hidden stale runtime | Exact scientist runtime is closed, failed, missing, or unverified | Reconcile to cleanup-pending with no progress event; deliver only the cleanup-gated terminal result. |
| Expanded direct work | A direct predicate becomes false at runtime | Stop at the safe boundary and obtain a visible Champion receipt before continuing. |
| Routed owner execution | A Squad acknowledges an ownership offer | The target owner independently decides `local_direct` or `local_champion`; the source does not preselect it. |

### Parent-request progress

| Case | Input distinction | Expected request-level effect |
| --- | --- | --- |
| Child transition | Champion records material task progress | Deliver immediately to coordinating Shotcaller only; no automatic requester event. |
| Parent-critical blocker | A blocker threatens the parent request | Atomically commit one immediate public-safe request event and requester outbox. |
| Recovered child blocker | Child reroutes locally and the parent is not threatened | Keep it in the routine aggregate. |
| Awaiting-user question | Work cannot continue without requester input | Emit immediately with bounded question summary; no raw child details. |
| Scope or plan change | Accepted scope or material plan changes | Emit immediately once per progress generation and recipient. |
| Acceptance risk | Evidence threatens an acceptance criterion | Emit immediately with the affected criterion summarized safely. |
| Final result | Owner reaches its final result | Emit immediately; child completion alone is not the owner result. |
| Routine changed progress | New aggregate status/counts arrive inside the minimum interval | Coalesce, then emit the newest changed aggregate when due. |
| Duplicate delivery | The same generation is retried | Reuse the event identity; unique key prevents another requester event/outbox. |
| Unchanged periodic check | Status, summary, and counts fingerprint is unchanged | Emit nothing; no heartbeat. |
| Offline requester | Required event commits while delivery endpoint is unavailable | Keep one pending outbox/obligation until exact recipient receipt. |
| Overdue owner | Changed buffered progress passes 15 minutes or a promised checkpoint expires | Create one `request_progress_due`; after five minutes emit one stalled notice, without inventing progress. |
| Public projection | Internal event includes runtime/session/worktree/path details | Omit those fields; expose only the bounded structured aggregate. |

### Model routing

| Case | Input distinction | Expected decision |
| --- | --- | --- |
| Full explicit target | User specifies provider/model/effort | Preserve all exactly; unsupported capability refuses. |
| Partial explicit target | User specifies only one or two target fields | Preserve those; fill only missing fields from active override, then versioned policy. |
| No downgrade evidence | Bounded/checkable work but gate is missing, stale, or below threshold | Strongest compatible reliability baseline. |
| Accepted downgrade | Representative evidence meets quality/success and correction thresholds | Faster/cheaper compatible tier with `evidence_downgrade`. |
| Ambiguous/high-impact/weak verification | Any strong-risk signal is present | Strongest compatible reliability baseline. |
| Unsupported inferred capability | Preferred inferred target lacks a required capability | Strongest compatible configured fallback, or refuse if none. |
| First concrete failure | Eligible weaker decision later has an accepted failure class | One safe-boundary stronger retry. |
| Second concrete failure | An escalation child fails again | Block with count still one. |
| Active operator override | Decision time is inside the 2026-08-28 interval | Fill unspecified fields with Sol/xhigh and record override evidence. |
| Expired operator override | Decision time is at or after expiry | Ignore override and use ordinary policy. |

## Primary sources

All web sources below were accessed 2026-08-28.

- [OpenAI Agents SDK: Agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/), [Handoffs](https://openai.github.io/openai-agents-python/handoffs/), and [Running agents](https://openai.github.io/openai-agents-python/running_agents/), official framework documentation.
- [OpenAI Agents SDK: Models](https://openai.github.io/openai-agents-python/models/), official framework/provider documentation.
- [LangChain: Multi-agent overview](https://docs.langchain.com/oss/python/langchain/multi-agent), [Subagents](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents), and [Handoffs](https://docs.langchain.com/oss/python/langchain/multi-agent/handoffs), official framework documentation.
- [Microsoft AutoGen: Teams](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/teams.html), official framework documentation.
- [A2A Protocol specification v1.0.1](https://github.com/a2aproject/A2A/blob/v1.0.1/docs/specification.md), official protocol specification released 2026-05-26.
- [Temporal: Task Queues](https://docs.temporal.io/task-queue), [Workflow definition](https://docs.temporal.io/workflow-definition), [Workflow message passing](https://docs.temporal.io/encyclopedia/workflow-message-passing), and [Workflow and Run IDs](https://docs.temporal.io/workflow-execution/workflowid-runid), official durable-execution documentation.
- [Kubernetes: Services](https://kubernetes.io/docs/concepts/services-networking/service/), [Readiness probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/), and [Labels and selectors](https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/), official framework documentation.
- [AWS Prescriptive Guidance: Transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html), official provider architecture guidance.
- [CloudEvents 1.0.2](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md), CNCF event-format specification released 2022-02-05.
- [Prometheus Alertmanager](https://prometheus.io/docs/alerting/latest/alertmanager/) and [configuration](https://prometheus.io/docs/alerting/latest/configuration/), official framework documentation.
- [etcd v3.7 API](https://etcd.io/docs/v3.7/learning/api/), official framework documentation last modified 2026-05-05.
- [DMN 1.5](https://www.omg.org/spec/DMN/1.5/PDF), Object Management Group decision-table specification, August 2024.
- [OpenTelemetry: Handling sensitive data](https://opentelemetry.io/docs/security/handling-sensitive-data/), official framework guidance last modified 2026-01-14.
- [OpenAI API: GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/latest-model) and [GPT-5.6 Sol model](https://developers.openai.com/api/docs/models/gpt-5.6-sol), official provider documentation.
- [OpenAI API: Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices), official provider documentation.
- [Anthropic: Effort](https://platform.claude.com/docs/en/build-with-claude/effort) and [Model IDs and versioning](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions), official provider documentation.
- [Google: Gemini thinking](https://ai.google.dev/gemini-api/docs/thinking), official provider documentation.
- [RFC 3339: Date and Time on the Internet](https://www.rfc-editor.org/rfc/rfc3339.html), IETF Standards Track specification, July 2002.
- [Ong et al., “RouteLLM: Learning to Route LLMs with Preference Data”](https://openreview.net/forum?id=8sSqNntaMr), ICLR 2025 primary paper.
- [Li et al., “LLMRouterBench: A Massive Benchmark and Unified Framework for LLM Routing”](https://aclanthology.org/2026.findings-acl.1881/), Findings of ACL 2026 primary paper.

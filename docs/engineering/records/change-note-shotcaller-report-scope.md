---
schemaVersion: 1
id: "change-note-shotcaller-report-scope"
revision: 1
type: "change-note"
status: "draft"
title: "Report Shotcaller-scoped callsign history"
repository: "league-of-orchestrator"
capabilityIds: ["activity-reporting"]
createdAt: "2026-09-08T23:15:00Z"
reconstructed: false
confidence: "verified"
unknowns: ["Installed release acceptance remains separate from the focused regression."]
modules: ["reporting"]
interfaces: ["league-report"]
seams: []
adapters: ["sqlite"]
relatedRecords: []
decisions: []
incidents: []
features: []
capabilities: []
amends: []
supersedes: []
learningRefs: []
diagrams: []
sources: [{"label":"Issue #22","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/22","kind":"issue"}]
verification: {"state":"verified","evidenceRefs":["tests/test_reporting.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 22
pr: null
release: null
run: null
---
# Shotcaller callsign history

Callsign identity reconciliation supports the `shotcaller` scope, whose scope
identifier is an agent ID. Reporting handled only squad, task and worker scopes.
Encountering a valid Shotcaller record therefore raised `KeyError` before the
report could describe outstanding work.

The reporting lookup now maps Shotcaller scope IDs through the existing public
actor-ID encoder. No schema, stored records, lifecycle transitions, scope filters,
or privacy rules change. A synthetic fixture reproduces the original failure and
checks reservation, activation and release events in owner-scoped local and
public-safe reports. This is a reporting repair, not product E2E acceptance.

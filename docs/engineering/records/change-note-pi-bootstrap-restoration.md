---
schemaVersion: 1
id: "change-note-pi-bootstrap-restoration"
revision: 1
type: "change-note"
status: "draft"
title: "Restore in-place Pi Shotcallers from bootstrap identity"
repository: "league-of-orchestrator"
capabilityIds: ["persistent-supervision"]
createdAt: "2026-09-23T00:25:00Z"
reconstructed: false
confidence: "verified"
unknowns: []
modules: ["runtime"]
interfaces: ["restored-agent"]
seams: []
adapters: ["pi", "herdr"]
relatedRecords: []
decisions: []
incidents: []
features: []
capabilities: []
amends: []
supersedes: []
learningRefs: []
diagrams: []
sources: [{"label":"Issue #23","url":"https://github.com/Vinosaamaa/league-of-orchestrator/issues/23","kind":"issue"}]
verification: {"state":"verified","evidenceRefs":["tests/test_multiplexer_metadata.py"]}
visibility: "public-safe"
publicationEligibility: "eligible"
issue: 23
pr: 248
release: null
run: null
---
# Restore in-place Pi Shotcallers from bootstrap identity

In-place Shotcaller bootstrap does not create a provider launch descriptor.
Pi restoration incorrectly required one. Resolve the verified active callsign
assignment and canonical bootstrap publication instead, sharing the existing
native publication checks. Preserve the full session path as routing identity
and use its bounded digest only for display metadata.

Descriptor-backed Champion restoration is unchanged. Both provider variants,
long paths, no process creation, and changed-session rejection are covered.

Native Pi also replaces its process title, hiding the original argv. Accept
only the exact single Node/Pi shape with matching OS start, shell parent,
foreground group and native session source. Foreign or incomplete shapes
remain rejected and have focused regression coverage.

PYTHON ?= python3
BASELINE_TESTS := \
	tests/test_agent_watcher.py \
	tests/test_record_contract.py \
	tests/test_delivery.py \
	tests/test_reconciliation.py \
	tests/test_lifecycle.py \
	tests/test_schema_examples.py

STORAGE_TESTS := \
	tests/test_sqlite_store_prototype.py \
	tests/test_sqlite_storage_migrations.py \
	tests/test_sqlite_storage_import_export.py \
	tests/test_sqlite_storage_commands.py \
	tests/test_sqlite_storage_concurrency.py \
	tests/test_project_catalog_roster.py \
	tests/test_autonomous_delivery.py \
	tests/test_issue_selection.py

REQUEST_LIFECYCLE_TESTS := \
	tests/test_request_lifecycle.py \
	tests/test_request_turn_batch.py \
	tests/test_assignment_dispatch.py \
	tests/test_request_concurrency.py \
	tests/test_transition_delivery.py \
	tests/test_cursor_steering.py \
	tests/test_pi_provider_launch.py \
	tests/test_shotcaller_stop.py \
	tests/test_canonical_watcher.py \
	tests/test_persistent_supervisor.py \
	tests/test_supervisor_delivery.py \
	tests/test_calm_supervision.py \
	tests/test_request_reconciliation.py \
	tests/test_request_lifecycle_cli.py

BENCHMARK_TESTS := \
	tests/test_request_turn_benchmark.py \
	tests/test_semantic_triage_benchmark.py \
	tests/test_semantic_triage_benchmark_integration.py \
	tests/test_inline_triage_prompt_shapes.py

RUNTIME_LIFECYCLE_TESTS := \
	tests/test_runtime_adapters.py \
	tests/test_multiplexer_metadata.py \
	tests/test_runtime_replacement.py \
	tests/test_cleanup_lifecycle.py \
	tests/test_production_cleanup.py \
	tests/test_repository_artifacts.py \
	tests/test_real_cleanup.py \
	tests/test_issue_continuation.py \
	tests/test_model_routing.py \
	tests/test_visible_champion_launch.py

ROUTING_POLICY_TESTS := \
	tests/test_routing_policy.py \
	tests/test_squad_registration.py \
	tests/test_request_progress_policy.py \
	tests/test_hidden_scientist_assignment.py

SKILL_CONTRACT_TESTS := \
	tests/test_skill_contracts.py

HANDOFF_CALLSIGN_TESTS := \
	tests/test_callsign_queue.py \
	tests/test_shotcaller_rollover.py \
	tests/test_shotcaller_bootstrap.py \
	tests/test_placement_policy.py

ACCEPTANCE_TESTS := \
	tests/test_acceptance_harness.py \
	tests/test_pre_cutover.py \
	tests/test_live_cutover.py

REPORTING_PRIVACY_TESTS := \
	tests/test_privacy.py \
	tests/test_guidance_installer.py \
	tests/test_public_safety.py \
	tests/test_reporting.py \
	tests/test_reporting_performance.py

.PHONY: test test-baseline test-storage test-project-roster test-acceptance test-request-lifecycle test-turn-benchmark test-runtime-lifecycle test-routing-policy test-skill-contracts test-handoff-callsigns test-reporting-privacy test-public-safety test-affected test-all
test:
	@$(MAKE) --no-print-directory test-baseline

test-baseline:
	@set -eu; for test in $(BASELINE_TESTS); do \
		LEAGUE_WRITER_POINTER="$(CURDIR)/tests/fixtures/absent-writer-pointer.json" PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
	done

test-storage:
	@set -eu; for test in $(STORAGE_TESTS); do \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
	done

test-project-roster:
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) tests/test_project_catalog_roster.py

test-request-lifecycle:
	@set -eu; for test in $(REQUEST_LIFECYCLE_TESTS); do \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
	done

test-turn-benchmark:
	@set -eu; for test in $(BENCHMARK_TESTS); do \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
	done

test-runtime-lifecycle:
	@set -eu; for test in $(RUNTIME_LIFECYCLE_TESTS); do \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
	done

test-routing-policy:
	@set -eu; for test in $(ROUTING_POLICY_TESTS); do \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
	done

test-skill-contracts:
	@set -eu; for test in $(SKILL_CONTRACT_TESTS); do \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
	done

test-handoff-callsigns:
	@set -eu; for test in $(HANDOFF_CALLSIGN_TESTS); do \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
	done

test-acceptance:
	@set -eu; for test in $(ACCEPTANCE_TESTS); do \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
	done

test-reporting-privacy:
	@set -eu; for test in $(REPORTING_PRIVACY_TESTS); do \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
	done

test-public-safety:
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/public_safety.py --base origin/main --head HEAD

test-affected: test-storage test-acceptance test-request-lifecycle test-runtime-lifecycle test-routing-policy test-skill-contracts test-handoff-callsigns test-reporting-privacy test-public-safety

test-all: test-baseline test-affected

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
	tests/test_project_catalog_roster.py

REQUEST_LIFECYCLE_TESTS := \
	tests/test_request_lifecycle.py \
	tests/test_assignment_dispatch.py \
	tests/test_request_concurrency.py \
	tests/test_transition_delivery.py \
	tests/test_shotcaller_stop.py \
	tests/test_request_lifecycle_cli.py

RUNTIME_LIFECYCLE_TESTS := \
	tests/test_runtime_adapters.py \
	tests/test_cleanup_lifecycle.py \
	tests/test_model_routing.py

SKILL_CONTRACT_TESTS := \
	tests/test_skill_contracts.py

HANDOFF_CALLSIGN_TESTS := \
	tests/test_callsign_queue.py \
	tests/test_shotcaller_rollover.py

ACCEPTANCE_TESTS := \
	tests/test_acceptance_harness.py

.PHONY: test test-baseline test-storage test-project-roster test-acceptance test-request-lifecycle test-runtime-lifecycle test-skill-contracts test-handoff-callsigns test-affected test-all
test:
	@$(MAKE) --no-print-directory test-baseline

test-baseline:
	@set -eu; for test in $(BASELINE_TESTS); do \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
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

test-runtime-lifecycle:
	@set -eu; for test in $(RUNTIME_LIFECYCLE_TESTS); do \
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

test-affected: test-storage test-acceptance test-request-lifecycle test-runtime-lifecycle test-skill-contracts test-handoff-callsigns

test-all: test-baseline test-affected

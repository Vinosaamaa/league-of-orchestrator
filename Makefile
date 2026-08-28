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
	tests/test_sqlite_storage_concurrency.py

REQUEST_LIFECYCLE_TESTS := \
	tests/test_request_lifecycle.py \
	tests/test_assignment_dispatch.py \
	tests/test_request_concurrency.py \
	tests/test_transition_delivery.py \
	tests/test_shotcaller_stop.py \
	tests/test_request_lifecycle_cli.py

.PHONY: test test-baseline test-storage test-request-lifecycle test-all
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

test-request-lifecycle:
	@set -eu; for test in $(REQUEST_LIFECYCLE_TESTS); do \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
	done

test-all: test-baseline test-storage test-request-lifecycle

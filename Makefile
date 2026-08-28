PYTHON ?= python3
TESTS := \
	tests/test_agent_watcher.py \
	tests/test_record_contract.py \
	tests/test_delivery.py \
	tests/test_reconciliation.py \
	tests/test_lifecycle.py \
	tests/test_schema_examples.py \
	tests/test_sqlite_store_prototype.py

STORAGE_TESTS := tests/test_sqlite_store_prototype.py

.PHONY: test test-storage
test:
	@set -eu; for test in $(TESTS); do \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
	done

test-storage:
	@set -eu; for test in $(STORAGE_TESTS); do \
		PYTHONDONTWRITEBYTECODE=1 $(PYTHON) $$test; \
	done

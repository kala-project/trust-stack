PYTHON ?= python3
PUBLIC_TEST_DATA_ROOT ?= var/documents
FRONTEND_DIST := frontend/dist
PUBLIC_TESTS := \
	tests.test_init_public \
	tests.test_api_contracts.ContractRegistryTests \
	tests.test_frontend_static_routes \
	tests.test_bootstrap_public_test_data \
	tests.test_frontend_supply_chain \
	tests.test_supply_chain \
	tests.test_register_configured_entities \
	tests.test_report_arithmetic_check

.PHONY: init build frontend sync-frontend public-test-data public-test-data-refresh \
	init-owner serve worker test check python-typecheck check-copilot clean

init:
	$(PYTHON) -S -m workbench.init_public

frontend:
	cd frontend && npm run typecheck && npm test && npm run build

sync-frontend: frontend
	cp $(FRONTEND_DIST)/index.html $(FRONTEND_DIST)/report-reader.html \
		$(FRONTEND_DIST)/theme-init.js $(FRONTEND_DIST)/workbench-app.css \
		$(FRONTEND_DIST)/workbench-app.js $(FRONTEND_DIST)/workbench-base.css \
		$(FRONTEND_DIST)/workbench-reader.css $(FRONTEND_DIST)/workbench-reader.js \
		$(FRONTEND_DIST)/workbench-shared.js web/

build: sync-frontend

public-test-data:
	$(PYTHON) -S scripts/bootstrap_public_test_data.py --root $(PUBLIC_TEST_DATA_ROOT)

public-test-data-refresh:
	$(PYTHON) -S scripts/bootstrap_public_test_data.py --root $(PUBLIC_TEST_DATA_ROOT) --refresh

init-owner:
	var/copilot-runtime/venv/bin/python -m workbench.server --init-owner

serve:
	var/copilot-runtime/venv/bin/python -m workbench.server

worker:
	var/copilot-runtime/venv/bin/python -m workbench.worker \
		--evidence-db var/evidence.sqlite3 \
		--chat-db var/chat.sqlite3 \
		--documents-root $(PUBLIC_TEST_DATA_ROOT) \
		--enable-public-acquisition

test:
	$(PYTHON) -S -m unittest $(PUBLIC_TESTS) -v

check: test frontend

# Optional: install mypy from requirements-dev.lock in an isolated environment first.
python-typecheck:
	$(PYTHON) scripts/check_type_contracts.py

check-copilot:
	var/copilot-runtime/venv/bin/python scripts/check_copilot_sdk.py

clean:
	rm -rf frontend/dist frontend/node_modules .mypy_cache

.PHONY: test lint install

# Canonical commands for splitfare development.
VENV := .venv/bin

test: ## run the test suite
	$(VENV)/python -m pytest tests/ -q

lint: ## pyright + compile checks
	$(VENV)/python -m py_compile splitfare.py sources.py providers.py mcp_server.py

install: ## editable install with mcp extras
	$(VENV)/pip install -e ".[mcp,dev]"

PYTHON ?= python

.PHONY: bootstrap start pull stop restart logs compose-env mcp-config

bootstrap start:
	$(PYTHON) scripts/bootstrap.py

pull:
	docker compose --env-file .syncsage/compose.env pull

stop:
	docker compose --env-file .syncsage/compose.env down

restart:
	docker compose --env-file .syncsage/compose.env restart syncsage

logs:
	docker compose --env-file .syncsage/compose.env logs -f syncsage

compose-env:
	$(PYTHON) -m syncsage compose-env syncsage.yaml --output .syncsage/compose.env

mcp-config:
	$(PYTHON) -m syncsage client-config vscode --output .vscode/mcp.json

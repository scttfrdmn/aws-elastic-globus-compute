# Pinned test tools live in .tools/ (gitignored).
SUBSTRATE_VERSION ?= v0.126.0

.PHONY: tools test lint docs

tools: .tools/substrate

.tools/substrate:
	GOBIN=$(CURDIR)/.tools go install github.com/scttfrdmn/substrate/cmd/substrate@$(SUBSTRATE_VERSION)

lint:
	ruff check . && ruff format --check . && mypy

test: tools
	pytest

docs:
	mkdocs build --strict

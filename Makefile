# Pinned test tools live in .tools/ (gitignored).
SUBSTRATE_VERSION ?= v0.128.0

.PHONY: tools test lint docs

# A stamp per version, so bumping SUBSTRATE_VERSION reinstalls the binary.
tools: .tools/substrate-$(SUBSTRATE_VERSION).stamp

.tools/substrate-$(SUBSTRATE_VERSION).stamp:
	GOBIN=$(CURDIR)/.tools go install github.com/scttfrdmn/substrate/cmd/substrate@$(SUBSTRATE_VERSION)
	rm -f $(CURDIR)/.tools/substrate-*.stamp && touch $@

lint:
	ruff check . && ruff format --check . && mypy

test: tools
	pytest

docs:
	mkdocs build --strict

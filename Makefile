PYTHON ?= python3
VENV   ?= .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python
GEN    := liquidmetal_at/flintlock/gen

.PHONY: venv
venv:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -U pip
	$(PIP) install -e '.[dev]'

# Generate gRPC stubs from the vendored (stripped) flintlock protos.
.PHONY: proto
proto:
	mkdir -p $(GEN)
	$(PY) -m grpc_tools.protoc \
		-Iproto \
		--python_out=$(GEN) \
		--grpc_python_out=$(GEN) \
		flapi/microvms.proto fltypes/microvm.proto
	touch $(GEN)/__init__.py $(GEN)/flapi/__init__.py $(GEN)/fltypes/__init__.py
	@echo "Generated stubs in $(GEN)"

# Re-fetch the upstream flintlock protos, re-strip the REST-gateway options, revendor.
.PHONY: refresh-proto
refresh-proto:
	$(PY) scripts/refresh_protos.py

.PHONY: test
test: proto
	$(VENV)/bin/pytest

.PHONY: lint
lint:
	$(VENV)/bin/ruff check liquidmetal_at tests

# Delete any DigitalOcean resources left tagged from a crashed run (at-*).
.PHONY: clean-tags
clean-tags:
	$(PY) -m liquidmetal_at.infra.reaper

.PHONY: clean
clean:
	rm -rf $(GEN)/flapi $(GEN)/fltypes $(GEN)/__init__.py
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

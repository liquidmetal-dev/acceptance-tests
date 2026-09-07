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

# Generate gRPC stubs from the vendored (stripped) flintlock + battery protos. Both
# compile into one shared $(GEN) root: battery's poolmgr/v1alpha1/types_pb2.py imports
# fltypes.microvm_pb2 as a sibling package, which only resolves if they share a root.
.PHONY: proto
proto:
	mkdir -p $(GEN)
	$(PY) -m grpc_tools.protoc \
		-Iproto \
		--python_out=$(GEN) \
		--grpc_python_out=$(GEN) \
		flapi/microvms.proto fltypes/microvm.proto \
		poolmgr/v1alpha1/pooladmin.proto poolmgr/v1alpha1/lease.proto \
		poolmgr/v1alpha1/events.proto poolmgr/v1alpha1/types.proto
	touch $(GEN)/__init__.py $(GEN)/flapi/__init__.py $(GEN)/fltypes/__init__.py \
		$(GEN)/poolmgr/__init__.py $(GEN)/poolmgr/v1alpha1/__init__.py
	@echo "Generated stubs in $(GEN)"

# Re-fetch the upstream flintlock protos, re-strip the REST-gateway options, revendor.
.PHONY: refresh-proto
refresh-proto:
	$(PY) scripts/refresh_protos.py

# Re-fetch battery's own protos, revendor.
.PHONY: refresh-battery-proto
refresh-battery-proto:
	$(PY) scripts/refresh_battery_protos.py

.PHONY: test
test: proto
	$(VENV)/bin/pytest tests --ignore=tests/battery

.PHONY: test-battery
test-battery: proto
	$(VENV)/bin/pytest tests/battery

.PHONY: lint
lint:
	$(VENV)/bin/ruff check liquidmetal_at tests

# Delete any DigitalOcean resources left tagged from a crashed run (at-*).
.PHONY: clean-tags
clean-tags:
	$(PY) -m liquidmetal_at.infra.reaper

.PHONY: clean
clean:
	rm -rf $(GEN)/flapi $(GEN)/fltypes $(GEN)/poolmgr $(GEN)/__init__.py
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

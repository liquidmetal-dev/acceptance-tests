# Vendored flintlock protos

These are **stripped, gRPC-only** copies of the flintlock API protos:

- `flapi/microvms.proto` — the `microvm.services.api.v1alpha1.MicroVM` service
  (Create/Get/List/Delete). brigade implements this exact service on port 9091.
- `fltypes/microvm.proto` — `flintlock.types` messages (`MicroVMSpec`, `MicroVM`, ...).

The upstream files import grpc-gateway/openapiv2 + `google.api` HTTP annotations used
only by the REST gateway. `scripts/refresh_protos.py` removes those imports and option
blocks so the stubs generate against the well-known types bundled with `grpcio-tools`
— no googleapis/grpc-gateway vendoring needed.

The upstream import `types/microvm.proto` is re-pointed to `fltypes/microvm.proto` to
avoid the generated Python module clashing with the stdlib `types` module.

The proto **package names are unchanged** (`microvm.services.api.v1alpha1`,
`flintlock.types`) so the wire-level service name matches what flintlock/brigade serve.

Regenerate:  `make refresh-proto && make proto`

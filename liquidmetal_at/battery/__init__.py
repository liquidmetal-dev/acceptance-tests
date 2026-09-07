"""gRPC client to battery's pool manager (poolmgrd): PoolAdmin, Lease, Events.

The generated stubs for ``poolmgr.v1alpha1`` live alongside flintlock's own generated
stubs under ``liquidmetal_at/flintlock/gen`` (one shared root — see the Makefile's
``proto`` target) because ``poolmgr/v1alpha1/types_pb2.py`` imports ``fltypes.microvm_pb2``
as a sibling package. Importing ``..flintlock`` puts that root on ``sys.path``.
"""
from __future__ import annotations

from .. import flintlock as _flintlock  # noqa: F401 - side effect: gen/ on sys.path

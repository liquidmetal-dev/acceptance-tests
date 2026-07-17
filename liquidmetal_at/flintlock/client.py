"""Thin wrapper around the flintlock MicroVM gRPC stub, pointed at brigade:9091.

Because brigade implements the identical proto, the same client drives both the
orchestrator (north edge, :9091) and, for cross-checks, a host's local flintlockd
(:9090). Exposes create/get/list/delete plus waiters for state transitions.
"""
from __future__ import annotations

import logging

import grpc
from flapi import microvms_pb2, microvms_pb2_grpc  # noqa: E402
from fltypes import microvm_pb2  # noqa: E402

from ..waiter import retry_call, wait_until  # noqa: E402

# side-effect import puts gen/ on sys.path
from . import _GEN  # noqa: F401

log = logging.getLogger("flintlock")

State = microvm_pb2.MicroVMStatus.MicroVMState

# gRPC codes brigade returns transiently while its scheduler partition is mid-change
# (a node joining/leaving briefly drops quorum and halves visible capacity). A create
# in that window is safe to retry — the cluster re-settles within seconds.
_TRANSIENT_CODES = frozenset(
    {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.RESOURCE_EXHAUSTED}
)


class MicroVMNotFound(RuntimeError):
    pass


class _TransientSchedulerError(RuntimeError):
    """A retryable brigade create failure (momentarily no quorum / no capacity)."""


class FlintlockClient:
    def __init__(self, host: str, port: int, *, timeout: float = 30):
        self.target = f"{host}:{port}"
        self._channel = grpc.insecure_channel(self.target)
        self._stub = microvms_pb2_grpc.MicroVMStub(self._channel)
        self._timeout = timeout

    def close(self) -> None:
        self._channel.close()

    def __enter__(self) -> FlintlockClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- CRUD ---

    def create(
        self, request: microvms_pb2.CreateMicroVMRequest, *, retry_timeout: float = 120
    ) -> microvm_pb2.MicroVM:
        """Create a microVM via brigade, retrying transient scheduler churn.

        brigade briefly returns UNAVAILABLE / RESOURCE_EXHAUSTED when its cluster
        partition is re-forming (a node join/leave drops quorum + cuts capacity for a
        few seconds). Retry those; surface everything else (bad spec, etc.) immediately.
        """

        def _once() -> microvm_pb2.MicroVM:
            try:
                return self._stub.CreateMicroVM(request, timeout=self._timeout).microvm
            except grpc.RpcError as e:
                if e.code() in _TRANSIENT_CODES:
                    raise _TransientSchedulerError(f"{e.code()}: {e.details()}") from e
                raise

        return retry_call(
            _once,
            timeout=retry_timeout,
            exceptions=(_TransientSchedulerError,),
            description="CreateMicroVM (transient scheduler churn)",
        )

    def get(self, uid: str) -> microvm_pb2.MicroVM:
        try:
            resp = self._stub.GetMicroVM(
                microvms_pb2.GetMicroVMRequest(uid=uid), timeout=self._timeout
            )
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                raise MicroVMNotFound(uid) from e
            raise
        return resp.microvm

    def list(self, namespace: str, name: str | None = None) -> list[microvm_pb2.MicroVM]:
        req = microvms_pb2.ListMicroVMsRequest(namespace=namespace)
        if name is not None:
            req.name = name
        resp = self._stub.ListMicroVMs(req, timeout=self._timeout)
        return list(resp.microvm)

    def delete(self, uid: str) -> None:
        self._stub.DeleteMicroVM(
            microvms_pb2.DeleteMicroVMRequest(uid=uid), timeout=self._timeout
        )

    def exists(self, uid: str) -> bool:
        try:
            self.get(uid)
            return True
        except MicroVMNotFound:
            return False

    # --- waiters ---

    def wait_state(
        self, uid: str, target: State.ValueType, *, timeout: float
    ) -> microvm_pb2.MicroVM:
        def _reached() -> microvm_pb2.MicroVM | None:
            vm = self.get(uid)
            state = vm.status.state
            if state == State.FAILED and target != State.FAILED:
                raise RuntimeError(f"microvm {uid} entered FAILED state")
            return vm if state == target else None

        return wait_until(
            _reached,
            timeout=timeout,
            interval=5,
            description=f"microvm {uid} -> {State.Name(target)}",
        )

    def wait_deleted(self, uid: str, *, timeout: float) -> None:
        wait_until(
            lambda: not self.exists(uid),
            timeout=timeout,
            interval=5,
            description=f"microvm {uid} deleted",
        )


def uid_of(vm: microvm_pb2.MicroVM) -> str:
    return vm.spec.uid

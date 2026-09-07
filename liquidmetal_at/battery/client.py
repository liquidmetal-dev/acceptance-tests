"""Thin wrapper around battery's PoolAdmin/Lease/Events gRPC stubs, pointed at poolmgrd.

Mirrors the shape of :mod:`liquidmetal_at.flintlock.client`: thin CRUD/claim wrappers
plus ``wait_until``-based waiters for the async pool-provisioning/replenishment flow.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

import grpc
from poolmgr.v1alpha1 import (  # noqa: E402
    events_pb2,
    events_pb2_grpc,
    lease_pb2,
    lease_pb2_grpc,
    pooladmin_pb2,
    pooladmin_pb2_grpc,
    types_pb2,
)

from ..waiter import wait_until  # noqa: E402
from . import _flintlock  # noqa: F401

log = logging.getLogger("battery")


class PoolNotFound(RuntimeError):
    pass


class NoVMAvailable(RuntimeError):
    """ClaimVM failed with RESOURCE_EXHAUSTED - no AVAILABLE VM in the pool yet."""


class PoolManagerClient:
    def __init__(self, host: str, port: int, *, timeout: float = 30):
        self.target = f"{host}:{port}"
        self._channel = grpc.insecure_channel(self.target)
        self._admin = pooladmin_pb2_grpc.PoolAdminStub(self._channel)
        self._lease = lease_pb2_grpc.LeaseStub(self._channel)
        self._events = events_pb2_grpc.EventsStub(self._channel)
        self._timeout = timeout

    def close(self) -> None:
        self._channel.close()

    def __enter__(self) -> PoolManagerClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- PoolAdmin ---

    def create_pool(self, spec: types_pb2.PoolSpec) -> types_pb2.Pool:
        return self._admin.CreatePool(
            pooladmin_pb2.CreatePoolRequest(spec=spec), timeout=self._timeout
        )

    def get_pool(self, name: str, namespace: str) -> types_pb2.Pool:
        try:
            return self._admin.GetPool(
                pooladmin_pb2.GetPoolRequest(ref=types_pb2.PoolRef(name=name, namespace=namespace)),
                timeout=self._timeout,
            )
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                raise PoolNotFound(f"{namespace}/{name}") from e
            raise

    def list_pools(self, namespace: str | None = None) -> list[types_pb2.Pool]:
        req = pooladmin_pb2.ListPoolsRequest()
        if namespace is not None:
            req.namespace = namespace
        resp = self._admin.ListPools(req, timeout=self._timeout)
        return list(resp.pools)

    def delete_pool(self, name: str, namespace: str) -> None:
        self._admin.DeletePool(
            pooladmin_pb2.DeletePoolRequest(ref=types_pb2.PoolRef(name=name, namespace=namespace)),
            timeout=self._timeout,
        )

    def pool_exists(self, name: str, namespace: str) -> bool:
        try:
            self.get_pool(name, namespace)
            return True
        except PoolNotFound:
            return False

    # --- Lease ---

    def claim_vm(self, pool_name: str, pool_namespace: str) -> lease_pb2.ClaimVMResponse:
        try:
            return self._lease.ClaimVM(
                lease_pb2.ClaimVMRequest(
                    pool=types_pb2.PoolRef(name=pool_name, namespace=pool_namespace)
                ),
                timeout=self._timeout,
            )
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                raise NoVMAvailable(f"{pool_namespace}/{pool_name}") from e
            raise

    def heartbeat(self, lease_id: str) -> lease_pb2.HeartbeatResponse:
        return self._lease.Heartbeat(
            lease_pb2.HeartbeatRequest(lease_id=lease_id), timeout=self._timeout
        )

    def release_vm(self, lease_id: str) -> None:
        self._lease.ReleaseVM(lease_pb2.ReleaseVMRequest(lease_id=lease_id), timeout=self._timeout)

    # --- Events ---

    def subscribe(self, *, timeout: float | None = None) -> Iterator[events_pb2.Event]:
        """Stream events. The caller is responsible for consuming it from a background
        thread if it needs to keep driving other calls concurrently - a streaming RPC
        blocks the calling thread between messages."""
        return self._events.Subscribe(events_pb2.SubscribeRequest(), timeout=timeout)

    # --- waiters ---

    def wait_available(
        self, name: str, namespace: str, count: int, *, timeout: float
    ) -> types_pb2.Pool:
        def _reached() -> types_pb2.Pool | None:
            pool = self.get_pool(name, namespace)
            return pool if pool.status.available_count >= count else None

        return wait_until(
            _reached,
            timeout=timeout,
            interval=5,
            description=f"pool {namespace}/{name} available >= {count}",
        )

    def wait_claimable(
        self, pool_name: str, pool_namespace: str, *, timeout: float
    ) -> lease_pb2.ClaimVMResponse:
        def _claimed() -> lease_pb2.ClaimVMResponse | None:
            try:
                return self.claim_vm(pool_name, pool_namespace)
            except NoVMAvailable:
                return None

        return wait_until(
            _claimed,
            timeout=timeout,
            interval=5,
            description=f"claimable VM in pool {pool_namespace}/{pool_name}",
        )

    def wait_deleted(self, name: str, namespace: str, *, timeout: float) -> None:
        wait_until(
            lambda: not self.pool_exists(name, namespace),
            timeout=timeout,
            interval=5,
            description=f"pool {namespace}/{name} deleted",
        )

"""Build a battery PoolSpec for the acceptance tests.

Reuses :func:`liquidmetal_at.flintlock.spec.build_spec` for ``microvm_template`` - the
same real, provisionable flintlock spec (kernel/rootfs/interface/static IP) already used
for brigade's VMs, built with ``guest_agent=True``.

CAUTION - verified directly from upstream (``internal/reconciler/provision.go``): ``Provision``
unconditionally calls ``flintlockclient.WaitReady`` (a no-op ``true`` exec via flintlockd's
native ``MicroVMExec`` service) before running any ``create_commands`` - it does this even when
the pool's hook lists are empty, not only when they're non-empty. So every VM needs the
in-guest guest-agent actually installed and running (``build_spec(..., guest_agent=True)``,
not just ``allow_guest_agent`` on the wire - that only attaches the vsock device, it doesn't
install the agent software) and flintlockd needs ``--enable-exec-api``
(``bootstrap/host.py::bootstrap_host_battery`` passes ``enable_exec_api=True``). Without both,
every VM in every pool times out in ``CREATE_HOOK_RUNNING`` and never reaches ``AVAILABLE``.

CAUTION - verified directly from upstream (``internal/reconciler/provision.go``, v0.3.2): the
reconciler clones ``pool.microvm_template`` for every VM in the pool and only overrides
``allow_guest_agent``, ``namespace`` (when empty) and, since v0.3.1, ``id`` - which it sets to
``<pool-name>-<8 hex>`` per VM, so the template's own ``id`` is ignored. The static IP and the
interface's ``guest_mac`` are still cloned verbatim, so a pool with ``size > 1`` built from a
template that carries a static address (as ``build_spec`` does) would hand every VM the same
address. See docs/battery-known-gaps.md. Keep ``size=1`` for every pool this suite creates
until upstream supports per-VM address variation.

NOTE - since flintlock v0.15.2 the per-VM sockets live at ``/run/flintlock/<uid>/``, so
battery's generated ``id`` (and hence pool name/namespace length) no longer affects the
guest-agent vsock socket path (flintlock#1226, battery#94).
"""
from __future__ import annotations

from poolmgr.v1alpha1 import types_pb2  # noqa: E402

from ..config import Config
from ..flintlock.spec import build_spec
from . import _flintlock  # noqa: F401


def build_pool_spec(
    cfg: Config,
    name: str,
    *,
    index: int,
    size: int,
    flintlock_hosts: list[str],
    replenishment_strategy: types_pb2.ReplenishmentStrategyType.ValueType = (
        types_pb2.MIN_SIZE_THRESHOLD
    ),
    min_size: int | None = None,
    heartbeat_interval_s: int = 30,
    heartbeat_expiry_threshold_s: int = 300,
) -> types_pb2.PoolSpec:
    strategy = types_pb2.ReplenishmentStrategy(type=replenishment_strategy)
    if replenishment_strategy == types_pb2.MIN_SIZE_THRESHOLD:
        strategy.min_size = min_size if min_size is not None else size

    spec = types_pb2.PoolSpec(
        name=name,
        namespace=cfg.microvm_namespace,
        microvm_template=build_spec(cfg, index, guest_agent=True),
        size=size,
        flintlock_hosts=flintlock_hosts,
        replenishment_strategy=strategy,
        hook_failure_policy=types_pb2.DELETE_AND_REPLACE,
    )
    spec.heartbeat_interval.FromSeconds(heartbeat_interval_s)
    spec.heartbeat_expiry_threshold.FromSeconds(heartbeat_expiry_threshold_s)
    return spec

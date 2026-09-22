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

CAUTION - that generated ``id`` becomes a directory in the guest-agent's vsock socket path
(``/var/lib/flintlock/vm/<namespace>/<id>/<uid>/guest-agent.vsock``), a Unix socket bound by
Linux's 108-byte ``sun_path`` (107 usable). Upstream doesn't length-check pool names, so an
over-long ``namespace`` + ``name`` is accepted by ``CreatePool`` and then fails every VM late
with ``connect: invalid argument`` (https://github.com/liquidmetal-dev/battery/issues/94).
:func:`build_pool_spec` rejects such names up front; keep pool names short and don't prefix
them with ``run_id`` - the namespace (``run_id`` by default) already isolates each run.
"""
from __future__ import annotations

from poolmgr.v1alpha1 import types_pb2  # noqa: E402

from ..config import Config
from ..flintlock.spec import build_spec
from . import _flintlock  # noqa: F401

# Mirrors flintlock's on-disk layout (pkg/defaults StateRootDir + GuestAgentVsockName) and
# battery's generated id suffix (internal/reconciler/provision.go, v0.3.1+).
_VSOCK_PATH_TEMPLATE = "/var/lib/flintlock/vm/{namespace}/{name}-xxxxxxxx/{uid}/guest-agent.vsock"
_FLINTLOCK_UID_LEN = 26  # ULID
_SUN_PATH_MAX = 107  # sizeof(sockaddr_un.sun_path) - 1 for the NUL terminator


def guest_agent_vsock_path_len(name: str, namespace: str) -> int:
    """Length in bytes of the vsock socket path flintlock will bind for a VM in this pool.

    Measured as UTF-8: ``sun_path`` is a byte limit, and a non-ASCII namespace/name takes more
    bytes than characters.
    """
    path = _VSOCK_PATH_TEMPLATE.format(
        namespace=namespace, name=name, uid="x" * _FLINTLOCK_UID_LEN
    )
    return len(path.encode("utf-8"))


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
    path_len = guest_agent_vsock_path_len(name, cfg.microvm_namespace)
    if path_len > _SUN_PATH_MAX:
        raise ValueError(
            f"pool {cfg.microvm_namespace}/{name}: guest-agent vsock path would be {path_len} "
            f"bytes (> {_SUN_PATH_MAX}), so every VM would fail with 'connect: invalid "
            "argument' - shorten the pool name or MICROVM_NAMESPACE "
            "(see https://github.com/liquidmetal-dev/battery/issues/94)"
        )

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

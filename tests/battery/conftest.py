"""Session-scoped pytest fixtures wiring the battery acceptance run's phases.

Chain:  config -> infra (provision) -> hosts (bootstrap flintlock on every node,
poolmgrd on node 0) -> battery_client (gRPC to poolmgrd). Self-contained and
deliberately separate from the root ``tests/conftest.py``: battery is a single-instance
pool manager dialing N flintlock hosts, not a peer mesh replacing flintlock's own API like
brigade, so the bootstrap step diverges enough that sharing one conftest would make both
harder to read. Teardown/failure-log-collection behavior mirrors the root conftest exactly.
"""
from __future__ import annotations

import logging

import pytest

from liquidmetal_at import config as config_mod
from liquidmetal_at.battery.client import PoolManagerClient
from liquidmetal_at.bootstrap import cloudinit
from liquidmetal_at.bootstrap.host import bootstrap_all_battery
from liquidmetal_at.infra import backend

log = logging.getLogger("battery.conftest")

# battery v0.3.1+ names each VM <pool-name>-<8 hex>; flintlock before v0.15.2 put that into the
# guest-agent socket path, overflowing sun_path so every VM fails late with "connect: invalid
# argument" (flintlock#1226). Fail the session up front instead of timing out every test.
MIN_FLINTLOCK_REF = "v0.15.2"


@pytest.fixture(scope="session")
def config() -> config_mod.Config:
    cfg = config_mod.load()
    if config_mod.flintlock_ref_below(cfg.flintlock_ref, MIN_FLINTLOCK_REF):
        pytest.exit(
            f"tests/battery/ needs FLINTLOCK_REF >= {MIN_FLINTLOCK_REF} (got {cfg.flintlock_ref}): "
            "older flintlock overflows the guest-agent socket path for battery's VM ids - "
            "see https://github.com/liquidmetal-dev/flintlock/issues/1226",
            returncode=2,
        )
    return cfg


@pytest.fixture(scope="session")
def infra(request, config):
    """Provision infra; always tear down (unless KEEP_INFRA_ON_FAILURE + failures)."""
    with backend.provisioned_infra(
        config,
        lambda i, name: cloudinit.user_data(config, i, name),
        failed=lambda: request.session.testsfailed > 0,
    ) as provisioned:
        yield provisioned


@pytest.fixture(scope="session")
def hosts(config, infra):
    """Bootstrap flintlock on every node + poolmgrd on node 0."""
    bootstrap_all_battery(config, infra)
    return infra


@pytest.fixture(scope="session")
def poolmgrd_node(hosts):
    """The node running poolmgrd (battery is single-instance, not a mesh)."""
    return hosts.nodes[0]


@pytest.fixture(scope="session")
def battery_client(request, config, poolmgrd_node):
    """gRPC client to poolmgrd; best-effort pool cleanup on teardown."""
    client = PoolManagerClient(poolmgrd_node.public_ip, config.battery_api_port)
    yield client
    if not request.session.testsfailed:
        try:
            for pool in client.list_pools(config.microvm_namespace):
                try:
                    client.delete_pool(pool.spec.name, pool.spec.namespace)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    client.close()

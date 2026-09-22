"""Session-scoped pytest fixtures wiring the battery acceptance run's phases.

Chain:  config -> infra (provision) -> hosts (bootstrap flintlock on every droplet,
poolmgrd on droplet 0) -> battery_client (gRPC to poolmgrd). Self-contained and
deliberately separate from the root ``tests/conftest.py``: battery is a single-instance
pool manager dialing N flintlock hosts, not a peer mesh replacing flintlock's own API like
brigade, so the bootstrap step diverges enough that sharing one conftest would make both
harder to read. Teardown/failure-log-collection behavior mirrors the root conftest exactly.
"""
from __future__ import annotations

import logging

import pytest

from liquidmetal_at import config as config_mod
from liquidmetal_at import logs
from liquidmetal_at.battery.client import PoolManagerClient
from liquidmetal_at.bootstrap import cloudinit
from liquidmetal_at.bootstrap.host import bootstrap_all_battery
from liquidmetal_at.infra import do

log = logging.getLogger("battery.conftest")

# Blocked on upstream flintlock exec-API bugs that make guest-agent readiness
# unreliable, so no VM in any pool reliably reaches AVAILABLE. See
# docs/battery-known-gaps.md for the full history:
#   - https://github.com/liquidmetal-dev/flintlock/issues/1200 (original hang; the fix in
#     v0.14.1/v0.15.0 resolved the clear-cut hang case but a re-test on v0.15.1 turned up an
#     ambiguous recurrence for one VM - not confirmed either way)
#   - https://github.com/liquidmetal-dev/flintlock/issues/1205 (handshake EOF; fixed in v0.15.1,
#     confirmed)
# Two further causes of "never reaches AVAILABLE" have since been fixed and not yet re-run on
# real infra: event-driven pools were never seeded before battery v0.3.2, and before flintlock
# v0.15.2 the guest-agent vsock socket path embedded namespace + VM id and overflowed sun_path
# (battery#94, fixed flintlock-side by flintlock#1227). Remove this once tests/battery/ passes
# cleanly on BATTERY_REF=v0.3.2 + FLINTLOCK_REF=v0.15.2 (or newer).
XFAIL_REASON = (
    "pending a clean e2e run on battery v0.3.2 + flintlock v0.15.2 - see "
    "docs/battery-known-gaps.md, flintlock#1200/#1205 and battery#94"
)

# battery v0.3.1+ names each VM <pool-name>-<8 hex>; flintlock before v0.15.2 put that into the
# guest-agent socket path, overflowing sun_path so every VM fails late with "connect: invalid
# argument" (flintlock#1226). Fail the session up front instead of timing out every test.
MIN_FLINTLOCK_REF = "v0.15.2"


def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.xfail(reason=XFAIL_REASON, strict=False))


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
    """Provision DO infra; always tear down (unless KEEP_INFRA_ON_FAILURE + failures)."""
    provisioned = do.provision(
        config, user_data_for=lambda i, name: cloudinit.user_data(config, i, name)
    )
    yield provisioned

    failed = request.session.testsfailed > 0
    if failed:
        try:
            logs.collect(config, provisioned)
        except Exception as exc:  # noqa: BLE001
            log.warning("log collection failed: %s", exc)
    if failed and config.keep_infra_on_failure:
        log.warning(
            "KEEP_INFRA_ON_FAILURE set and tests failed - leaving infra tag=%s up. "
            "Reap later with: make clean-tags",
            config.tag,
        )
        return
    do.destroy_by_tag(config, provisioned.client)


@pytest.fixture(scope="session")
def hosts(config, infra):
    """Bootstrap flintlock on every droplet + poolmgrd on droplet 0."""
    bootstrap_all_battery(config, infra)
    return infra


@pytest.fixture(scope="session")
def poolmgrd_node(hosts):
    """The droplet running poolmgrd (battery is single-instance, not a mesh)."""
    return hosts.droplets[0]


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

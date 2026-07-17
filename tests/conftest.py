"""Session-scoped pytest fixtures wiring the acceptance run's phases.

Chain:  config -> infra (provision) -> hosts (bootstrap) -> cluster (mesh gate) ->
fl_client (gRPC to brigade). Teardown is registered on the infra fixture so a failure
in any later phase still reaps DigitalOcean resources. Set KEEP_INFRA_ON_FAILURE=true
to leave infra up for debugging when a test fails.
"""
from __future__ import annotations

import itertools
import logging
import os

import pytest

from liquidmetal_at import config as config_mod
from liquidmetal_at import logs
from liquidmetal_at.bootstrap import cloudinit
from liquidmetal_at.bootstrap.brigade_cluster import wait_for_cluster
from liquidmetal_at.bootstrap.host import bootstrap_all
from liquidmetal_at.flintlock.client import FlintlockClient
from liquidmetal_at.infra import do

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("conftest")

# pydo runs on the Azure SDK, whose default HttpLoggingPolicy logs every DO API
# request/response at INFO. Quiet the azure.* tree by default; re-enable with the
# --log-http flag or LOG_HTTP=true (see pytest_configure).
logging.getLogger("azure").setLevel(logging.WARNING)


def pytest_addoption(parser):
    parser.addoption(
        "--log-http",
        action="store_true",
        default=False,
        help="Emit DigitalOcean API HTTP request/response logs (Azure/pydo pipeline).",
    )


def pytest_configure(config):
    enabled = config.getoption("--log-http") or config_mod._bool(
        os.environ.get("LOG_HTTP", "false")
    )
    logging.getLogger("azure").setLevel(logging.INFO if enabled else logging.WARNING)


@pytest.fixture(scope="session")
def config() -> config_mod.Config:
    return config_mod.load()


@pytest.fixture(scope="session")
def infra(request, config):
    """Provision DO infra; always tear down (unless KEEP_INFRA_ON_FAILURE + failures)."""
    provisioned = do.provision(
        config, user_data_for=lambda i, name: cloudinit.user_data(config, i, name)
    )
    yield provisioned

    failed = request.session.testsfailed > 0
    if failed:
        # Capture host logs + mesh diagnostics before any keep/destroy decision, so a
        # failed run is analyzable from artifacts/ even when infra is left up. Never let a
        # collection error leak infra by skipping the teardown below.
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
    """Bootstrap flintlock + brigade on both droplets."""
    bootstrap_all(config, infra)
    return infra


@pytest.fixture(scope="session")
def cluster(config, hosts):
    """Block until the 2-node brigade Erlang mesh has formed."""
    wait_for_cluster(config, hosts)
    return hosts


@pytest.fixture(scope="session")
def brigade_node(cluster):
    """The droplet whose brigade north edge the client dials."""
    return cluster.droplets[0]


@pytest.fixture(scope="session")
def fl_client(request, config, cluster, brigade_node):
    """gRPC client to brigade's north edge; best-effort namespace cleanup on teardown."""
    client = FlintlockClient(brigade_node.public_ip, config.brigade_grpc_port)
    yield client
    # On failure, leave the microVMs in place: deleting removes their
    # /var/lib/flintlock/vm/<uid>/firecracker.log, which logs.collect (run later in the
    # infra teardown) needs to explain why a guest never reached CREATED. The droplets are
    # destroyed wholesale afterwards anyway, so this per-VM tidy only matters on success.
    if not request.session.testsfailed:
        try:
            for vm in client.list(config.microvm_namespace):
                try:
                    client.delete(vm.spec.uid)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    client.close()


@pytest.fixture(scope="session")
def vm_index():
    """Hand out unique microVM indices (→ unique ids + static IPs) across all tests."""
    counter = itertools.count(0)
    return lambda: next(counter)

"""Gate on brigade Erlang-mesh formation before running assertions.

Polls every node's status endpoint until it reports at least ``min_cluster_size``
members. A stuck size-1 cluster means firewall/cookie/topology trouble — we fail
loud with a diagnostic rather than letting placement tests fail cryptically later.
"""
from __future__ import annotations

import logging

from .. import brigade_status
from ..config import Config
from ..infra.do import Infra
from ..waiter import wait_until

log = logging.getLogger("cluster")


def wait_for_cluster(cfg: Config, infra: Infra) -> None:
    target = cfg.brigade_min_cluster_size
    for d in infra.droplets:
        def _formed(ip=d.public_ip) -> bool:
            size = brigade_status.cluster_size(ip, cfg.brigade_status_port)
            log.info("node %s reports cluster size %d (want >= %d)", ip, size, target)
            return size >= target

        wait_until(
            _formed,
            timeout=cfg.timeout_cluster,
            interval=5,
            description=f"brigade cluster size>={target} on {d.public_ip}",
        )
    log.info("brigade cluster formed (size>=%d) across %d nodes", target, len(infra.droplets))

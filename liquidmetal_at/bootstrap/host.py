"""Drive per-host bootstrap over SSH: flintlock stack, then brigade node.

Runs after droplets are active. Each host is provisioned in parallel; the brigade
config is peer-aware (all peer private IPs) so the nodes form an Erlang mesh.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from ..config import Config
from ..infra.do import ERLANG_DIST_HIGH, ERLANG_DIST_LOW, Droplet, Infra
from ..remote.ssh import SSH
from .render import render

log = logging.getLogger("bootstrap")

# The DO block volume attaches by name at a stable /dev/disk/by-id path; the raw
# device is /dev/sda for the first attached volume on DO droplets.
THINPOOL_DISK = "/dev/sda"
THINPOOL_NAME = "flintlock-thinpool"
PARENT_IFACE = "eth1"  # DO private-network interface inside the VPC
BRIDGE_NAME = "flintlock0"  # host bridge flintlock attaches guest TAP devices to


def _erlang_hosts(private_ips: list[str]) -> str:
    """Render the libcluster Epmd hosts list, e.g. :"brigade@10.0.0.2", :"brigade@10.0.0.3"."""
    return ", ".join(f':"brigade@{ip}"' for ip in private_ips)


def _capacity(cfg: Config) -> tuple[int, int]:
    """Per-host capacity sized so a single host cannot hold all N microVMs.

    Guarantees the placement test spreads: each host tops out at (N-1) VMs.
    """
    n = max(cfg.microvm_count, 2)
    reserve_vcpu, reserve_mem = 1, 1024
    vcpu = reserve_vcpu + (n - 1) * cfg.microvm_vcpu
    mem = reserve_mem + (n - 1) * cfg.microvm_mem_mb
    return vcpu, mem


def _provision_flintlock(cfg: Config, ssh: SSH) -> None:
    ssh.run("cloud-init status --wait || true", timeout=1200)
    script = render(
        "provision_host.sh.j2",
        thinpool=THINPOOL_NAME,
        disk=THINPOOL_DISK,
        parent_iface=PARENT_IFACE,
        bridge_name=BRIDGE_NAME,
        bridge_addr=cfg.microvm_gateway_cidr,
        guest_subnet=cfg.microvm_subnet_cidr,
        flintlock_grpc_port=cfg.flintlock_grpc_port,
    )
    ssh.put(script, "/tmp/provision_host.sh")
    ssh.sudo("bash /tmp/provision_host.sh", timeout=1800)


def _provision_brigade(
    cfg: Config, ssh: SSH, node_index: int, private_ip: str, all_private_ips: list[str]
) -> None:
    cap_vcpu, cap_mem = _capacity(cfg)
    config_exs = render(
        "brigade_config.exs.j2",
        grpc_port=cfg.brigade_grpc_port,
        status_port=cfg.brigade_status_port,
        flintlock_grpc_port=cfg.flintlock_grpc_port,
        private_ip=private_ip,
        min_cluster_size=cfg.brigade_min_cluster_size,
        node_index=node_index,
        capacity_vcpu=cap_vcpu,
        capacity_mem_mb=cap_mem,
        erlang_hosts=_erlang_hosts(all_private_ips),
    )
    ssh.sudo("mkdir -p /opt/brigade/config")
    ssh.put(config_exs, "/tmp/brigade_config.exs")
    ssh.sudo("cp /tmp/brigade_config.exs /opt/brigade/config/config.exs")

    script = render(
        "provision_brigade.sh.j2",
        private_ip=private_ip,
        cookie=cfg.brigade_cookie,
        grpc_port=cfg.brigade_grpc_port,
        status_port=cfg.brigade_status_port,
        dist_min=ERLANG_DIST_LOW,
        dist_max=ERLANG_DIST_HIGH,
    )
    ssh.put(script, "/tmp/provision_brigade.sh")
    ssh.sudo("bash /tmp/provision_brigade.sh", timeout=1800)


def bootstrap_host(
    cfg: Config, droplet: Droplet, node_index: int, all_private_ips: list[str]
) -> None:
    log.info("bootstrapping host%d (%s)", node_index, droplet.public_ip)
    ssh = SSH(host=droplet.public_ip, user="root", key_path=cfg.ssh_private_key_path)
    ssh.connect(timeout=cfg.timeout_ssh)
    try:
        _provision_flintlock(cfg, ssh)
        _provision_brigade(cfg, ssh, node_index, droplet.private_ip, all_private_ips)
    finally:
        ssh.close()
    log.info("host%d bootstrapped", node_index)


def bootstrap_all(cfg: Config, infra: Infra) -> None:
    """Bootstrap all hosts in parallel."""
    private_ips = [d.private_ip for d in infra.droplets]
    with ThreadPoolExecutor(max_workers=len(infra.droplets)) as pool:
        futures = [
            pool.submit(bootstrap_host, cfg, d, i, private_ips)
            for i, d in enumerate(infra.droplets)
        ]
        for f in futures:
            f.result()  # re-raise any bootstrap failure

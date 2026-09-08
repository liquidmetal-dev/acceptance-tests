"""DigitalOcean provisioning + tag-scoped teardown via the pydo SDK.

Provisions, for one acceptance run: an SSH key (idempotent by public-key match), a
VPC, one raw block volume + one droplet per flintlock host, and a firewall. Every
resource is tagged ``cfg.tag`` so ``destroy_by_tag`` can reap the whole run even if
the process crashed before returning an :class:`Infra` handle.

pydo returns plain dicts (JSON bodies from the DO API); we read them defensively.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pydo import Client

from ..config import Config
from ..waiter import retry_call, wait_until

log = logging.getLogger("infra")

# Erlang distribution port range opened between droplets for the brigade mesh.
ERLANG_DIST_LOW = 9100
ERLANG_DIST_HIGH = 9200
EPMD_PORT = 4369
GOSSIP_UDP_PORT = 45892


@dataclass
class Droplet:
    id: int
    name: str
    public_ip: str
    private_ip: str


@dataclass
class Infra:
    cfg: Config
    client: Client
    ssh_key_id: int
    vpc_id: str
    vpc_cidr: str
    firewall_id: str
    volume_ids: list[str] = field(default_factory=list)
    droplets: list[Droplet] = field(default_factory=list)


def client(cfg: Config) -> Client:
    return Client(token=cfg.do_token)


def _ensure_tag(c: Client, tag: str) -> None:
    """Create the run's tag up front so every resource reliably attaches to it.

    DO only dependably materializes a tag when a *droplet* is created with it; a VPC
    (name-matched), firewall (tags = match selector), or volume won't. Creating it here
    guarantees the tag exists before any resource references it — and before teardown
    tries to destroy droplets by it. Idempotent: DO returns 422 if it already exists.
    """
    try:
        c.tags.create(body={"name": tag})
    except Exception as exc:  # noqa: BLE001 - already-exists is fine
        log.debug("tag %s create: %s", tag, exc)


# --- SSH key ---------------------------------------------------------------


def _ensure_ssh_key(c: Client, cfg: Config) -> int:
    pub = cfg.ssh_public_key
    existing = c.ssh_keys.list()
    for key in existing.get("ssh_keys", []):
        if key.get("public_key", "").strip() == pub:
            log.info("reusing existing DO ssh key id=%s", key["id"])
            return key["id"]
    created = c.ssh_keys.create(body={"name": f"{cfg.tag}-key", "public_key": pub})
    return created["ssh_key"]["id"]


# --- VPC -------------------------------------------------------------------


def _create_vpc(c: Client, cfg: Config) -> tuple[str, str]:
    body = {"name": cfg.tag, "region": cfg.do_region, "description": "acceptance run"}
    created = c.vpcs.create(body=body)["vpc"]
    return created["id"], created.get("ip_range", "")


# --- volumes + droplets ----------------------------------------------------


def _create_volume(c: Client, cfg: Config, name: str) -> str:
    body = {
        "name": name,
        "region": cfg.do_region,
        "size_gigabytes": cfg.do_volume_gb,
        "filesystem_type": "",  # raw, no filesystem — flintlock builds a thinpool on it
        "tags": [cfg.tag],
    }
    return c.volumes.create(body=body)["volume"]["id"]


def _create_droplet(
    c: Client, cfg: Config, name: str, ssh_key_id: int, vpc_id: str, volume_id: str, user_data: str
) -> int:
    body = {
        "name": name,
        "region": cfg.do_region,
        "size": cfg.do_size,
        "image": cfg.do_image,
        "ssh_keys": [ssh_key_id],
        "vpc_uuid": vpc_id,
        "volumes": [volume_id],
        "tags": [cfg.tag],
        "user_data": user_data,
        "ipv6": False,
    }
    return c.droplets.create(body=body)["droplet"]["id"]


def _droplet_ready(c: Client, droplet_id: int) -> Droplet | None:
    d = c.droplets.get(droplet_id=droplet_id)["droplet"]
    if d.get("status") != "active":
        return None
    pub = priv = ""
    for net in d.get("networks", {}).get("v4", []):
        if net.get("type") == "public":
            pub = net["ip_address"]
        elif net.get("type") == "private":
            priv = net["ip_address"]
    if pub and priv:
        return Droplet(id=droplet_id, name=d["name"], public_ip=pub, private_ip=priv)
    return None


# --- firewall --------------------------------------------------------------


def _create_firewall(c: Client, cfg: Config) -> str:
    """Firewall scoping SSH + brigade north edge to the world, mesh ports to peers."""
    anywhere = {"addresses": ["0.0.0.0/0", "::/0"]}
    peers = {"tags": [cfg.tag]}  # only the droplets in this run

    def tcp(port: str, sources: dict) -> dict:
        return {"protocol": "tcp", "ports": port, "sources": sources}

    def udp(port: str, sources: dict) -> dict:
        return {"protocol": "udp", "ports": port, "sources": sources}

    inbound = [
        tcp("22", anywhere),
        tcp(str(cfg.brigade_grpc_port), anywhere),
        tcp(str(cfg.brigade_status_port), anywhere),
        # battery's poolmgrd API + metrics (runs on droplet 0 only, but the firewall is
        # one shared rule set for the whole tagged run)
        tcp(str(cfg.battery_api_port), anywhere),
        tcp(str(cfg.battery_metrics_port), anywhere),
        # mesh + south edge only between the run's droplets
        tcp(str(cfg.flintlock_grpc_port), peers),
        tcp(str(EPMD_PORT), peers),
        tcp(f"{ERLANG_DIST_LOW}-{ERLANG_DIST_HIGH}", peers),
        udp(str(GOSSIP_UDP_PORT), peers),
    ]
    outbound = [
        {"protocol": "tcp", "ports": "1-65535", "destinations": anywhere},
        {"protocol": "udp", "ports": "1-65535", "destinations": anywhere},
        {"protocol": "icmp", "destinations": anywhere},
    ]
    body = {
        "name": cfg.tag,
        "inbound_rules": inbound,
        "outbound_rules": outbound,
        "tags": [cfg.tag],
    }
    return c.firewalls.create(body=body)["firewall"]["id"]


# --- public API ------------------------------------------------------------


def provision(cfg: Config, user_data_for: callable) -> Infra:
    """Provision all infra for a run. ``user_data_for(index, name)`` -> cloud-init str."""
    c = client(cfg)
    log.info("provisioning DO infra tag=%s region=%s", cfg.tag, cfg.do_region)

    _ensure_tag(c, cfg.tag)
    ssh_key_id = _ensure_ssh_key(c, cfg)
    vpc_id, vpc_cidr = _create_vpc(c, cfg)
    firewall_id = _create_firewall(c, cfg)

    infra = Infra(
        cfg=cfg,
        client=c,
        ssh_key_id=ssh_key_id,
        vpc_id=vpc_id,
        vpc_cidr=vpc_cidr,
        firewall_id=firewall_id,
    )

    droplet_ids: list[tuple[int, str]] = []
    for i in range(cfg.droplet_count):
        name = f"{cfg.tag}-host{i}"
        vol_id = _create_volume(c, cfg, f"{name}-pool")
        infra.volume_ids.append(vol_id)
        did = _create_droplet(
            c, cfg, name, ssh_key_id, vpc_id, vol_id, user_data_for(i, name)
        )
        droplet_ids.append((did, name))
        log.info("created droplet %s id=%s", name, did)

    for did, name in droplet_ids:
        droplet = wait_until(
            lambda did=did: _droplet_ready(c, did),
            timeout=cfg.timeout_provision,
            interval=5,
            description=f"droplet {name} active + IPs",
        )
        infra.droplets.append(droplet)
        log.info("droplet %s active pub=%s priv=%s", name, droplet.public_ip, droplet.private_ip)

    return infra


def destroy_by_tag(cfg: Config, c: Client | None = None) -> None:
    """Idempotently delete every resource tagged for this run."""
    destroy_tag(c or client(cfg), cfg.tag)


def destroy_tag(c: Client, tag: str) -> None:
    """Idempotently delete every DO resource tagged/named ``tag``.

    Order matters on DO: firewall + droplets before the VPC (a VPC with members
    won't delete), volumes after their droplets detach. Safe to call twice. Shared
    by the pytest teardown fixtures (via :func:`destroy_by_tag`) and the orphan
    sweep in ``reaper.py``, so both paths get the same retry/backoff protection.
    """
    log.info("tearing down DO infra tag=%s", tag)

    # firewalls (matched by name == tag)
    for fw in c.firewalls.list().get("firewalls", []):
        if fw.get("name") == tag:
            retry_call(
                lambda fw=fw: c.firewalls.delete(firewall_id=fw["id"]),
                timeout=60,
                description="delete firewall",
            )

    # droplets by tag. A missing tag (provisioning died before the first droplet was
    # created) means "nothing to destroy" — 404 is success, not a retryable error, so
    # swallow it and let teardown continue to volumes + VPC below.
    def _destroy_droplets() -> None:
        try:
            c.droplets.destroy_by_tag(tag_name=tag)
        except Exception as exc:  # noqa: BLE001
            if "does not exist" in str(exc).lower():
                log.info("no tag %s (nothing to destroy by tag)", tag)
                return
            raise

    retry_call(_destroy_droplets, timeout=120, description="destroy droplets by tag")

    # volumes by name prefix (must be detached — droplets are gone above)
    def _delete_volumes() -> None:
        for vol in c.volumes.list().get("volumes", []):
            if tag in vol.get("name", "") or tag in (vol.get("tags") or []):
                c.volumes.delete(volume_id=vol["id"])

    retry_call(_delete_volumes, timeout=180, description="delete volumes")

    # VPC last (eventual consistency after droplet deletion). DO auto-promotes a
    # region's only VPC to that region's "default" once nothing else claims the
    # slot, and refuses to delete a default VPC — a terminal, non-retryable state
    # for an account/region with no other VPC to fall back to. The VPC itself is
    # free, so leaving it behind costs nothing; just stop retrying and move on.
    def _delete_vpc() -> None:
        for vpc in c.vpcs.list().get("vpcs", []):
            if vpc.get("name") == tag:
                try:
                    c.vpcs.delete(vpc_id=vpc["id"])
                except Exception as exc:  # noqa: BLE001
                    if "default vpc" in str(exc).lower():
                        log.warning(
                            "vpc %s is now the region's default VPC and cannot be "
                            "deleted via the API; leaving it in place (no cost)",
                            vpc["id"],
                        )
                        return
                    raise

    retry_call(_delete_vpc, timeout=180, description="delete vpc")
    log.info("teardown complete for tag=%s", tag)

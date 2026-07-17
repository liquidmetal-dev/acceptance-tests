"""Read brigade's HTTP status endpoint (:9600) for cluster + placement observability.

The exact JSON schema of brigade ``/status`` is version-dependent, so parsing is
deliberately tolerant: we probe several plausible key names and fall back gracefully.
The placement map (uid -> host) is used by the spread test; ``cluster_size`` gates
cluster formation and the quorum test.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("brigade_status")


def _get(ip: str, port: int, path: str = "/status", timeout: float = 5) -> dict:
    resp = requests.get(f"http://{ip}:{port}{path}", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def healthy(ip: str, port: int) -> bool:
    try:
        _get(ip, port, "/healthz")
        return True
    except Exception:  # noqa: BLE001
        try:
            _get(ip, port, "/status")
            return True
        except Exception:  # noqa: BLE001
            return False


def cluster_size(ip: str, port: int) -> int:
    """Best-effort count of nodes the given brigade node sees in its partition."""
    data = _get(ip, port)
    # brigade reports membership under `partition` (size / members). Prefer it: the
    # top-level `hosts` list holds only this node's own managed host (len 1), so the
    # fallback loops below would otherwise undercount a healthy cluster.
    part = data.get("partition")
    if isinstance(part, dict):
        if isinstance(part.get("size"), int):
            return part["size"]
        if isinstance(part.get("members"), list):
            return len(part["members"])
    for key in ("cluster_size", "clusterSize", "size", "quorum_size"):
        if isinstance(data.get(key), int):
            return data[key]
    for key in ("nodes", "members", "cluster", "hosts"):
        val = data.get(key)
        if isinstance(val, list):
            return len(val)
        if isinstance(val, dict):
            return len(val)
    raise ValueError(f"cannot determine cluster size from status payload: {list(data)}")


def placement_map(ip: str, port: int) -> dict[str, str]:
    """Return uid -> host mapping from the status payload (best-effort)."""
    data = _get(ip, port)
    out: dict[str, str] = {}
    vms = None
    for key in ("vms", "microvms", "micro_vms", "placements"):
        if isinstance(data.get(key), list):
            vms = data[key]
            break
    if vms is None:
        return out
    for vm in vms:
        uid = vm.get("uid") or vm.get("id") or vm.get("microvm_uid")
        host = vm.get("host") or vm.get("node") or vm.get("host_id") or vm.get("endpoint")
        if uid and host:
            out[str(uid)] = str(host)
    return out

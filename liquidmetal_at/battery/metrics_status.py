"""Tolerant scrape of poolmgrd's Prometheus ``/metrics`` endpoint.

Used as an independent ground-truth cross-check against ``GetPool().status`` - the same
spirit as :mod:`liquidmetal_at.brigade_status`'s tolerant parsing of brigade's own status
surface, scaled down to what battery actually exposes (Prometheus text format, not JSON).
Parsing is line-oriented and ignores anything it doesn't recognize, since the metric set
is an upstream-sensitive spot (labeled gauge names/labels may shift release to release).
"""
from __future__ import annotations

import re

import requests

_GAUGE_RE = re.compile(
    r'^(?P<name>poolmgr_pool_\w+)\{(?P<labels>[^}]*)\}\s+(?P<value>[0-9.eE+-]+)\s*$'
)
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        m = _GAUGE_RE.match(line)
        if not m:
            continue
        labels = dict(_LABEL_RE.findall(m.group("labels")))
        rows.append({"name": m.group("name"), "value": float(m.group("value")), **labels})
    return rows


def fetch(host: str, port: int, *, timeout: float = 10) -> list[dict]:
    resp = requests.get(f"http://{host}:{port}/metrics", timeout=timeout)
    resp.raise_for_status()
    return _parse(resp.text)


def pool_status(host: str, port: int, pool_name: str, pool_namespace: str, **kw) -> dict:
    """Best-effort ``{available, leased, provisioning, quarantined}`` for one pool.

    Missing gauges (e.g. a pool with no VMs yet - poolmgrd only emits a pool's gauges
    once it exists in the store) are simply absent from the returned dict.
    """
    out: dict[str, float] = {}
    suffix_map = {
        "poolmgr_pool_available": "available",
        "poolmgr_pool_leased": "leased",
        "poolmgr_pool_provisioning": "provisioning",
        "poolmgr_pool_quarantined": "quarantined",
    }
    for row in fetch(host, port, **kw):
        if row.get("pool_name") != pool_name or row.get("pool_namespace") != pool_namespace:
            continue
        key = suffix_map.get(row["name"])
        if key:
            out[key] = row["value"]
    return out

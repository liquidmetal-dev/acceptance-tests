#!/usr/bin/env python3
"""Re-fetch battery's own protos and re-vendor them.

Battery's `poolmgr/v1alpha1` protos are already pure gRPC (no REST-gateway options to
strip). The only rewrite needed is the same clash-avoidance re-point
`scripts/refresh_protos.py` applies to flintlock's own protos: `types/microvm.proto` ->
`fltypes/microvm.proto`, so `poolmgr.v1alpha1.PoolSpec.microvm_template` resolves against
the vendored flintlock types.

Usage:  BATTERY_REF=v0.1.0 python scripts/refresh_battery_protos.py
"""
from __future__ import annotations

import os
import urllib.request
from pathlib import Path

REF = os.environ.get("BATTERY_REF", "main")
BASE = f"https://raw.githubusercontent.com/liquidmetal-dev/battery/{REF}/api/proto/poolmgr/v1alpha1/"
ROOT = Path(__file__).resolve().parent.parent / "proto" / "poolmgr" / "v1alpha1"

FILES = ("pooladmin.proto", "lease.proto", "events.proto", "types.proto")


def fetch(rel: str) -> str:
    with urllib.request.urlopen(BASE + rel) as r:
        return r.read().decode()


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        src = fetch(name)
        src = src.replace('import "types/microvm.proto";', 'import "fltypes/microvm.proto";')
        (ROOT / name).write_text(src)
    print(f"Vendored protos from battery@{REF} into {ROOT}")


if __name__ == "__main__":
    main()

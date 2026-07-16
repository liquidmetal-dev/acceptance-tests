#!/usr/bin/env python3
"""Re-fetch the flintlock protos and re-vendor stripped, gRPC-only copies.

The upstream protos import grpc-gateway/openapiv2 + google.api HTTP annotations that
are only needed for the REST gateway. The acceptance suite talks pure gRPC, so we strip
those imports + option blocks. This keeps stub generation hermetic on the well-known
types bundled with grpcio-tools (no googleapis / grpc-gateway vendoring required).

Usage:  FLINTLOCK_REF=main python scripts/refresh_protos.py
"""
from __future__ import annotations

import os
import re
import urllib.request
from pathlib import Path

REF = os.environ.get("FLINTLOCK_REF", "main")
BASE = f"https://raw.githubusercontent.com/liquidmetal-dev/flintlock/{REF}/"
ROOT = Path(__file__).resolve().parent.parent / "proto"


def fetch(rel: str) -> str:
    with urllib.request.urlopen(BASE + rel) as r:
        return r.read().decode()


def strip_gateway(src: str) -> str:
    # Drop REST-gateway / openapiv2 / google.api imports.
    src = "\n".join(
        line
        for line in src.splitlines()
        if not re.search(r'import "(google/api/annotations|protoc-gen-openapiv2)', line)
    )
    # Drop the top-level openapiv2_swagger option block.
    src = re.sub(
        r"option \(grpc\.gateway\.protoc_gen_openapiv2[^\n]*=\s*\{.*?\n\};\n",
        "",
        src,
        flags=re.S,
    )
    # Drop per-rpc google.api.http option blocks.
    src = re.sub(r"\s*option \(google\.api\.http\)\s*=\s*\{.*?\};", "", src, flags=re.S)
    return src


def main() -> None:
    svc = strip_gateway(fetch("api/services/microvm/v1alpha1/microvms.proto"))
    # Re-point the types import at our clash-free vendored dir name.
    svc = svc.replace('import "types/microvm.proto";', 'import "fltypes/microvm.proto";')
    types = strip_gateway(fetch("api/types/microvm.proto"))

    (ROOT / "flapi").mkdir(parents=True, exist_ok=True)
    (ROOT / "fltypes").mkdir(parents=True, exist_ok=True)
    (ROOT / "flapi" / "microvms.proto").write_text(svc)
    (ROOT / "fltypes" / "microvm.proto").write_text(types)
    print(f"Vendored stripped protos from flintlock@{REF} into {ROOT}")


if __name__ == "__main__":
    main()

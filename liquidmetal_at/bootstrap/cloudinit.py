"""Render droplet cloud-init user-data (base packages + warmed toolchain + clones)."""
from __future__ import annotations

from ..config import Config
from .render import render


def user_data(cfg: Config, index: int, name: str) -> str:  # noqa: ARG001 - signature matches provisioner
    return render(
        "cloud_init.yaml.j2",
        flintlock_ref=cfg.flintlock_ref,
        brigade_ref=cfg.brigade_ref,
    )

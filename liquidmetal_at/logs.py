"""Collect host logs to the artifacts dir for post-mortem debugging."""
from __future__ import annotations

import logging
from pathlib import Path

from .config import Config
from .infra.do import Infra
from .remote.ssh import SSH

log = logging.getLogger("logs")


def collect(cfg: Config, infra: Infra) -> None:
    out = Path(cfg.artifacts_dir) / cfg.run_id
    out.mkdir(parents=True, exist_ok=True)
    for i, d in enumerate(infra.droplets):
        try:
            ssh = SSH(host=d.public_ip, user="root", key_path=cfg.ssh_private_key_path)
            ssh.connect(timeout=30)
            for unit in ("flintlockd", "brigade"):
                _, journal, _ = ssh.run(
                    f"journalctl -u {unit} --no-pager -n 500 || true", check=False
                )
                (out / f"host{i}-{unit}.log").write_text(journal)
            ssh.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not collect logs from host%d: %s", i, exc)
    log.info("collected host logs into %s", out)

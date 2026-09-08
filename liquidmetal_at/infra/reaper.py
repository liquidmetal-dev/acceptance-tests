"""Reap orphaned acceptance-test resources left by crashed runs.

Deletes every DigitalOcean resource whose tag/name starts with the suite prefix
(``lm-acceptance-``). Run via ``make clean-tags``. DO_API_TOKEN must be set.
"""
from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv
from pydo import Client

PREFIX = "lm-acceptance-"

log = logging.getLogger("reaper")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(override=False)
    token = os.environ.get("DO_API_TOKEN", "").strip()
    if not token:
        print("DO_API_TOKEN not set", file=sys.stderr)
        return 1
    c = Client(token=token)

    tags = {
        t["name"]
        for t in c.tags.list().get("tags", [])
        if t.get("name", "").startswith(PREFIX)
    }
    if not tags:
        log.info("no orphaned %s* tags found", PREFIX)
        return 0

    for tag in sorted(tags):
        log.info("reaping tag %s", tag)
        for fw in c.firewalls.list().get("firewalls", []):
            if fw.get("name") == tag:
                c.firewalls.delete(firewall_id=fw["id"])
        try:
            c.droplets.destroy_by_tag(tag_name=tag)
        except Exception as exc:  # noqa: BLE001
            log.warning("droplet destroy for %s: %s", tag, exc)
        for vol in c.volumes.list().get("volumes", []):
            if tag in vol.get("name", "") or tag in (vol.get("tags") or []):
                try:
                    c.volumes.delete(volume_id=vol["id"])
                except Exception as exc:  # noqa: BLE001
                    log.warning("volume delete: %s", exc)
        for vpc in c.vpcs.list().get("vpcs", []):
            if vpc.get("name") == tag:
                try:
                    c.vpcs.delete(vpc_id=vpc["id"])
                except Exception as exc:  # noqa: BLE001
                    log.warning("vpc delete: %s", exc)
        try:
            c.tags.delete(tag_id=tag)
        except Exception as exc:  # noqa: BLE001
            log.warning("tag delete: %s", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

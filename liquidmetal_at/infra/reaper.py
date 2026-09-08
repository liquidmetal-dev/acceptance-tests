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

from .do import destroy_tag

PREFIX = "lm-acceptance-"

log = logging.getLogger("reaper")


def reap_tag(c: Client, tag: str) -> bool:
    """Tear down one tag's resources via the shared retry-protected teardown.

    Returns whether the tag itself was deleted. On failure the tag is left in
    place (not deleted) so the next sweep rediscovers it and retries — deleting
    it unconditionally would silently orphan whatever failed to tear down.
    """
    try:
        destroy_tag(c, tag)
    except Exception as exc:  # noqa: BLE001
        log.warning("teardown for %s failed, leaving tag for next sweep: %s", tag, exc)
        return False
    try:
        c.tags.delete(tag_id=tag)
    except Exception as exc:  # noqa: BLE001
        log.warning("tag delete: %s", exc)
        return False
    return True


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
        reap_tag(c, tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

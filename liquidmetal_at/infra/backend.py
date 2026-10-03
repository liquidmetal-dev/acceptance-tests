"""Select the infrastructure backend and run the shared provision/teardown lifecycle.

``INFRA_BACKEND`` picks where the flintlock hosts come from: DigitalOcean droplets
(default) or local libvirt VMs. Both suites' ``infra`` fixtures delegate to
:func:`provisioned_infra` so failure handling is identical everywhere.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from .. import logs
from ..config import Config
from . import do, libvirt
from .types import Infra

log = logging.getLogger("infra.backend")


def provision(cfg: Config, user_data_for: Callable[[int, str], str]) -> Infra:
    if cfg.infra_backend == "libvirt":
        return libvirt.provision(cfg, user_data_for)
    return do.provision(cfg, user_data_for)


def destroy(cfg: Config, infra: Infra) -> None:
    if cfg.infra_backend == "libvirt":
        libvirt.destroy_run(cfg)
    else:
        do.destroy_by_tag(cfg, getattr(infra, "client", None))


def _cleanup_hint(cfg: Config) -> str:
    return "make clean-libvirt" if cfg.infra_backend == "libvirt" else "make clean-tags"


@contextmanager
def provisioned_infra(
    cfg: Config, user_data_for: Callable[[int, str], str], *, failed: Callable[[], bool]
) -> Iterator[Infra]:
    """Provision; on exit collect logs if ``failed()``, then tear down.

    Teardown is skipped only when the run failed *and* KEEP_INFRA_ON_FAILURE is set.
    """
    infra = provision(cfg, user_data_for)
    yield infra

    did_fail = failed()
    if did_fail:
        # Capture host logs + mesh diagnostics before any keep/destroy decision, so a
        # failed run is analyzable from artifacts/ even when infra is left up. Never let a
        # collection error leak infra by skipping the teardown below.
        try:
            logs.collect(cfg, infra)
        except Exception as exc:  # noqa: BLE001
            log.warning("log collection failed: %s", exc)
        if cfg.infra_backend == "libvirt":
            libvirt.collect_consoles(cfg)
    if did_fail and cfg.keep_infra_on_failure:
        log.warning(
            "KEEP_INFRA_ON_FAILURE set and tests failed - leaving infra %s up. "
            "Reap later with: %s",
            cfg.tag,
            _cleanup_hint(cfg),
        )
        return
    destroy(cfg, infra)

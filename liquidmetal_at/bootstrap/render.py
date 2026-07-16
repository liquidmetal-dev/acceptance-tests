"""Jinja2 template rendering for cloud-init + host provisioning scripts."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES)),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


def render(template: str, **ctx) -> str:
    return _env.get_template(template).render(**ctx)

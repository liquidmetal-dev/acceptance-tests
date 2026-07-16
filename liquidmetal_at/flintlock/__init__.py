"""gRPC client to the flintlock MicroVM API (served by brigade on the north edge).

The generated stubs live under ``gen/`` and use root-relative imports
(``from flapi import ...``), so ``gen/`` is placed on ``sys.path`` here.
"""
from __future__ import annotations

import sys
from pathlib import Path

_GEN = Path(__file__).parent / "gen"
if str(_GEN) not in sys.path:
    sys.path.insert(0, str(_GEN))

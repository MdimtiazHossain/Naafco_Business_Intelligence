"""Put ``backend/`` on ``sys.path`` so the scripts run without installation."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Windows consoles default to a legacy code page; force UTF-8 so Bangla text and
# box-drawing characters print instead of raising UnicodeEncodeError.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):  # pragma: no cover - non-reconfigurable stream
            pass

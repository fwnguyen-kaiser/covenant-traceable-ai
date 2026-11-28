"""Pytest bootstrap initializing Django settings against the configured test database."""
from __future__ import annotations

import os
import sys
from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent
AI_SRC = WEB_ROOT.parent / "ai" / "src"
for p in (str(WEB_ROOT), str(AI_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "covenant_web.settings")

import django  # noqa: E402

django.setup()

#!/usr/bin/env python
"""Django administrative entry point for Project Covenant Web."""
import os
import sys
from pathlib import Path


def main() -> None:
    web_root = Path(__file__).resolve().parent
    ai_src = web_root.parent / "ai" / "src"
    for p in (str(web_root), str(ai_src)):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "covenant_web.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()

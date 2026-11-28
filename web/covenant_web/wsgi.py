"""WSGI entrypoint for Project Covenant Web."""
import os
import sys
from pathlib import Path

web_root = Path(__file__).resolve().parent.parent
ai_src = web_root.parent / "ai" / "src"
for p in (str(web_root), str(ai_src)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "covenant_web.settings")
from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()

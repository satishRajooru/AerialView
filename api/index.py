# Vercel entrypoint (vercel.json rewrites every path here). Path restoration lives in
# app.main.VercelRewriteFix so it also works when Vercel loads app/main.py directly.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402,F401

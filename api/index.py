# Vercel entrypoint: every route is rewritten here (see vercel.json).
#
# vercel.json rewrites "/<anything>" to "/api/index?__path=/<anything>". Depending on the
# runtime, the ASGI app may then see the rewritten path ("/api/index") instead of the
# original one, which makes every route 404. This wrapper restores the original path.
import os
import sys
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app as fastapi_app  # noqa: E402


async def app(scope, receive, send):
    if scope["type"] in ("http", "websocket"):
        qs = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        orig = next((v for k, v in qs if k == "__path"), None)
        if orig is not None:
            path = "/" + orig.lstrip("/")
            scope = dict(scope, path=path, raw_path=path.encode(),
                         query_string=urlencode([(k, v) for k, v in qs if k != "__path"]).encode())
        elif scope.get("path", "").startswith("/api/index"):
            # Rewritten path without our marker: treat the remainder as the real path.
            path = scope["path"][len("/api/index"):] or "/"
            scope = dict(scope, path=path, raw_path=path.encode())
        if scope.get("root_path"):
            scope = dict(scope, root_path="")
    await fastapi_app(scope, receive, send)

"""Vercel serverless entrypoint for the FastAPI operator API.

Vercel's Python runtime discovers the ASGI `app` object in api/index.py.
The backend package lives under src/ (api/, db.py, env.py), so both the
project root and src/ are put on sys.path before importing — mirroring
src/api/main.py's own bootstrap for local uvicorn runs.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
for _path in (_SRC, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from api.main import app  # noqa: E402,F401
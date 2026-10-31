"""Vercel serverless function: every /api/* request lands here (see vercel.json and coach/web.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # makes the coach package importable

from coach.web import app  # noqa: E402, F401

"""Vercel Python function using the same API as the local app."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.server import CancelRegistry, handler_for
from app.services import StudioService

_service = StudioService()
_registry = CancelRegistry()


class handler(handler_for(_service, _registry, None)):
    pass

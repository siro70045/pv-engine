"""Shared slowapi `Limiter` instance.

Routers import `limiter` from here (instead of `api.main`) to avoid the
circular import that would otherwise occur — `api.main` itself imports the
routers at startup.
"""

from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"

limiter = Limiter(key_func=get_remote_address, enabled=RATE_LIMIT_ENABLED)

from .libraries import router as libraries_router
from .logs import router as logs_router
from .media import router as media_router
from .scan import router as scan_router
from .settings import router as settings_router

__all__ = [
    "libraries_router",
    "logs_router",
    "media_router",
    "scan_router",
    "settings_router",
]

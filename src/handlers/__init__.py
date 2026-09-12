from src.handlers.start import setup_start_handler
from src.handlers.settings import setup_settings_handlers
from src.handlers.status import setup_status_handlers
from src.handlers.cancel import setup_cancel_handlers
from src.handlers.restart import setup_restart_handler
from src.handlers.workers import setup_worker_handlers
from src.handlers.mi import setup_mediainfo_handlers

__all__ = [
    "setup_start_handler",
    "setup_settings_handlers",
    "setup_status_handlers",
    "setup_cancel_handlers",
    "setup_restart_handler",
    "setup_worker_handlers",
    "setup_mediainfo_handlers",
]

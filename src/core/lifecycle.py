import signal
import sys
from collections.abc import Callable

from .logging import setup_logging

logger = setup_logging("text2sql.lifecycle")

_shutdown_callbacks: list[Callable[[], None]] = []


def register_shutdown(callback: Callable[[], None]) -> None:
    _shutdown_callbacks.append(callback)


def _handle_signal(signum: int, frame: object) -> None:
    name = signal.Signals(signum).name
    logger.info("Received signal %s, initiating graceful shutdown...", name)
    for cb in _shutdown_callbacks:
        try:
            cb()
        except Exception:
            logger.exception("Error during shutdown callback")
    logger.info("Shutdown complete")
    sys.exit(0)


def install_signal_handlers() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass
    logger.info("Signal handlers installed")

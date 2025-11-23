# app/utils/logging.py
import logging
from logging.handlers import RotatingFileHandler
import queue
import threading
from typing import Callable, Optional

DEFAULT_LOGFILE = "ImageToJpgApp.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

# A simple thread-safe queue-based handler for UI consumption
class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.queue = q

    def emit(self, record):
        try:
            msg = self.format(record)
            self.queue.put_nowait(msg)
        except Exception:
            self.handleError(record)

def setup_logger(app_name: str = "ImageToJpgApp", logfile: Optional[str] = None, ui_queue: Optional[queue.Queue] = None) -> logging.Logger:
    """
    Configure and return a logger used by the application.

    - logfile: path to log file (if None, default in cwd used).
    - ui_queue: if provided, a Queue object; logs are forwarded to it for UI display.
    """
    logger = logging.getLogger(app_name)
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        # already configured
        return logger

    # Formatter
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    # Console handler (INFO+)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file handler (DEBUG+)
    lf = logfile or DEFAULT_LOGFILE
    fh = RotatingFileHandler(lf, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Optional UI queue handler (DEBUG+)
    if ui_queue is not None:
        qh = QueueHandler(ui_queue)
        qh.setLevel(logging.DEBUG)
        qh.setFormatter(fmt)
        logger.addHandler(qh)

    # avoid duplicate propagation
    logger.propagate = False
    return logger

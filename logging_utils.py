"""logging_utils.py — safe logging helpers for Fiber EUR."""
from __future__ import annotations

import logging
import os

class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for key in ("OANDA_API_KEY", "TELEGRAM_TOKEN"):
            secret = os.environ.get(key, "")
            if secret and secret in msg:
                msg = msg.replace(secret, "***REDACTED***")
        record.msg = msg
        record.args = ()
        return True


def install_secret_redaction() -> None:
    flt = SecretRedactionFilter()
    root = logging.getLogger()
    root.addFilter(flt)
    for handler in root.handlers:
        handler.addFilter(flt)

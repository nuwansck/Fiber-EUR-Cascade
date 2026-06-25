"""state_utils.py — Fiber EUR Cascade v1.2 State Persistence Utilities
State persistence utilities for Fiber EUR Cascade v1.2.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import pytz

logger = logging.getLogger(__name__)

SG_TZ    = pytz.timezone("Asia/Singapore")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Canonical file paths — single source of truth
TRADE_HISTORY_FILE = DATA_DIR / "trade_history.json"
OPS_STATE_FILE     = DATA_DIR / "ops_state.json"
RUNTIME_STATE_FILE = DATA_DIR / "runtime_state.json"


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(default, dict) and not isinstance(data, dict):
                    return default.copy()
                if isinstance(default, list) and not isinstance(data, list):
                    return default.copy()
                return data
    except Exception as exc:
        logger.warning("Failed to load %s: %s", path, exc)
    return default.copy() if isinstance(default, (dict, list)) else default


def save_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as tmp:
            json.dump(data, tmp, indent=2)
            temp_name = tmp.name
        os.replace(temp_name, path)
    except Exception as exc:
        logger.warning("Failed to save %s: %s", path, exc)


def update_runtime_state(**kwargs) -> None:
    state = load_json(RUNTIME_STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    state.update(kwargs)
    state["updated_at_sgt"] = datetime.now(SG_TZ).strftime("%Y-%m-%d %H:%M:%S")
    save_json(RUNTIME_STATE_FILE, state)


def parse_sgt_timestamp(value: str | None) -> datetime | None:
    """Parse a SGT timestamp string into a timezone-aware datetime.

    Accepts both '%Y-%m-%d %H:%M:%S' and ISO '%Y-%m-%dT%H:%M:%S' formats.
    Returns None if value is falsy or unparseable.
    """
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return SG_TZ.localize(datetime.strptime(value, fmt))
        except Exception:
            pass
    return None

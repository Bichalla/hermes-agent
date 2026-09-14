"""Gateway hook lifecycle; the daemon broker cannot outlive its gateway process."""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import stat
import threading

from .broker import BridgeConfig, Broker
from .pm import PmApprovalService

_broker = None
_lock = threading.Lock()


def load_config(path: Path) -> BridgeConfig:
    mode = path.lstat()
    if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise ValueError("bridge configuration must be a private owner file")
    config = BridgeConfig(**json.loads(path.read_text()))
    if (not Path(config.db_path).is_absolute() or not Path(config.socket_path).is_absolute()
            or not config.owner_id.isdecimal() or not config.notifier_profile
            or not config.worker_profiles or not 0 < config.max_timeout <= 300):
        raise ValueError("invalid bridge configuration")
    return config


def stop() -> None:
    global _broker
    with _lock:
        old, _broker = _broker, None
        if old is not None:
            old.close()


def start(service, config_path: Path) -> None:
    global _broker
    if getattr(service, "api_version", None) != 1 or not callable(getattr(service, "request", None)):
        raise RuntimeError("Hermes Kanban approval API is unavailable; bridge disabled")
    config = load_config(config_path)
    from hermes_constants import get_hermes_home
    if get_hermes_home().name != config.notifier_profile:
        raise RuntimeError("Kanban owner broker is installed in the wrong profile")
    with _lock:
        old, _broker = _broker, None
        if old is not None:
            old.close()
        new = Broker(config, PmApprovalService(service))
        try:
            new.start()
        except Exception:
            new.close()
            raise RuntimeError("Kanban owner broker failed to start") from None
        _broker = new


atexit.register(stop)

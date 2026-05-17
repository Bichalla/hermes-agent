#!/usr/bin/env python3
"""No-secret runtime preflight for Discord voice-channel mode.

Checks Python packages and local system dependencies needed for Hermes to join,
listen in, and play TTS into a Discord voice channel. The output is deliberately
limited to dependency names and local executable/library paths; it never reads or
prints Hermes config, tokens, or environment secret values.
"""
from __future__ import annotations

import ctypes.util
import importlib.util
import json
import os
import shutil
from typing import Any, Callable

REQUIRED_MODULES = ["discord", "nacl", "davey", "edge_tts", "faster_whisper"]
OPTIONAL_MODULES = ["mutagen"]
REQUIRED_BINARIES = ["ffmpeg"]
REQUIRED_LIBRARIES = ["opus"]
MACOS_HOMEBREW_OPUS_PATHS = [
    "/opt/homebrew/lib/libopus.dylib",
    "/usr/local/lib/libopus.dylib",
]


def _has_library(
    name: str,
    find_library: Callable[[str], str | None],
    exists: Callable[[str], bool],
) -> bool:
    if find_library(name):
        return True
    if name == "opus":
        return any(exists(path) for path in MACOS_HOMEBREW_OPUS_PATHS)
    return False


def check_runtime(
    *,
    find_spec: Callable[[str], Any] = importlib.util.find_spec,
    which: Callable[[str], str | None] = shutil.which,
    find_library: Callable[[str], str | None] = ctypes.util.find_library,
    exists: Callable[[str], bool] = os.path.exists,
) -> dict[str, Any]:
    missing: list[str] = []
    optional_missing: list[str] = []

    for name in REQUIRED_MODULES:
        if find_spec(name) is None:
            missing.append(f"python:{name}")
    for name in OPTIONAL_MODULES:
        if find_spec(name) is None:
            optional_missing.append(f"python:{name}")
    for name in REQUIRED_BINARIES:
        if which(name) is None:
            missing.append(name)
    for name in REQUIRED_LIBRARIES:
        if not _has_library(name, find_library, exists):
            missing.append(f"lib:{name}")

    return {
        "ok": not missing,
        "missing": missing,
        "optional_missing": optional_missing,
    }


def main() -> int:
    result = check_runtime()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run focused bridge fixtures with an empty home and no inherited credentials."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", type=Path, default=ROOT / ".work/hermes-source")
parser.add_argument("--pattern", default="test_*.py")
args = parser.parse_args()
with tempfile.TemporaryDirectory(prefix="hermes-bridge-test-") as home:
    python = args.source.resolve() / ".venv/bin/python"
    if not python.exists():
        active_config = Path.home() / ".hermes/ops/runtime-protection/runtime-protection.json"
        if active_config.exists():
            python = Path(json.loads(active_config.read_text())["venv"]) / "bin/python"
    if not python.exists():
        python = Path(sys.executable)
    env = {
        "HOME": home, "HERMES_HOME": home, "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(args.source.resolve())]),
        "PYTHONDONTWRITEBYTECODE": "1", "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "TZ": "UTC", "LANG": "C.UTF-8",
    }
    result = subprocess.run(
        [str(python), "-B", "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
         "-p", args.pattern, "-v"], cwd=home, env=env,
    )
    raise SystemExit(result.returncode)

#!/usr/bin/env python3
"""Run registered-workflow tests from copied sources in a default-deny sandbox."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SOURCE_AGENT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_HOME = Path.home()
VENV_ROOT = SOURCE_AGENT_ROOT / "venv"
PYTHON = VENV_ROOT / "bin" / "python"
PYTHON_RUNTIME_ROOT = PYTHON.resolve().parents[1]


def _scheme(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')


def _copy_source(root: Path) -> Path:
    agent = root / "agent-src"
    shutil.copytree(
        SOURCE_AGENT_ROOT,
        agent,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git",
            "venv",
            "node_modules",
            ".pytest_cache",
            "__pycache__",
            "*.pyc",
            "*.db",
            "*.db-*",
            "*.sqlite",
            "*.sqlite3",
        ),
    )
    return agent


def _reject_source_symlinks(*roots: Path) -> None:
    for source_root in roots:
        for candidate in source_root.rglob("*"):
            if candidate.is_symlink():
                raise RuntimeError("copied source contains a symlink")


def _source_manifest_digest(*roots: Path) -> str:
    digest = hashlib.sha256()
    for source_root in roots:
        label = source_root.name
        for candidate in sorted(path for path in source_root.rglob("*") if path.is_file()):
            relative = candidate.relative_to(source_root).as_posix()
            digest.update(label.encode("utf-8") + b"\0")
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(candidate.read_bytes()).digest())
    return digest.hexdigest()


def _copy_python_runtime(root: Path) -> tuple[Path, Path]:
    site_packages = root / "site-packages"
    shutil.copytree(
        VENV_ROOT / "lib" / "python3.11" / "site-packages",
        site_packages,
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return PYTHON.resolve(), site_packages


def _sandbox_profile(root: Path, python_runtime: Path) -> str:
    readable = [
        root,
        PYTHON_RUNTIME_ROOT,
        Path("/System/Library"),
        Path("/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld"),
        Path("/usr/lib"),
        Path("/usr/share/zoneinfo"),
        Path("/private/var/db/timezone"),
    ]
    rules = [
        "(version 1)",
        "(deny default)",

        '(allow file-read-metadata (literal "/"))',
        # dyld CacheFinder enumerates the root directory itself during startup;
        # literal matching does not authorize reads beneath `/`.
        '(allow file-read-data (literal "/"))',
        '(allow file-read-metadata (literal "/private"))',
        '(allow file-read-metadata (literal "/etc"))',
        '(allow file-read-metadata (literal "/var"))',
        '(allow file-read-metadata (literal "/tmp"))',
        '(allow file-read* (literal "/dev/null"))',
        "(allow process-fork)",
        "(allow process-info*)",
        "(allow sysctl-read)",
        '(allow ipc-posix-shm-read-data (ipc-posix-name "apple.shm.notification_center"))',
        '(allow mach-lookup (global-name "com.apple.dyld.crc32c"))',
        '(allow mach-lookup (global-name "com.apple.system.notification_center"))',
        '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))',
        '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.membership"))',
        '(allow mach-lookup (global-name "com.apple.cfprefsd.agent"))',
        '(allow mach-lookup (global-name "com.apple.cfprefsd.daemon"))',
        '(allow mach-lookup (global-name "com.apple.logd"))',
        '(allow mach-lookup (global-name "com.apple.system.logger"))',
        '(allow mach-lookup (global-name "com.apple.securityd"))',
        '(allow mach-lookup (global-name "com.apple.trustd.agent"))',
        '(allow mach-lookup (global-name "com.apple.lsd.mapdb"))',
    ]
    rules.append(f'(allow process-exec (literal "{_scheme(python_runtime)}"))')
    rules.append(
        f'(allow file-map-executable (literal "{_scheme(python_runtime)}"))'
    )

    for executable_root in (python_runtime.parent,):
        rules.append(
            f'(allow process-exec (subpath "{_scheme(executable_root)}"))'
        )
    rules.append(
        f'(allow file-map-executable (subpath "{_scheme(PYTHON_RUNTIME_ROOT)}"))'
    )
    for path in readable:
        rules.append(f'(allow file-read* (subpath "{_scheme(path)}"))')
        for parent in path.parents:
            rules.append(
                f'(allow file-read-metadata (literal "{_scheme(parent)}"))'
            )
    rules.extend(
        [
            f'(allow file-write* (subpath "{_scheme(root)}"))',
            '(allow file-lock)',
            '(allow file-ioctl)',
            '(allow file-write* (literal "/dev/null"))',
            '(allow file-read-metadata (literal "/"))',
            f'(deny file-read* (subpath "{_scheme(SOURCE_HOME / ".hermes")}"))',
            f'(deny file-read* (subpath "{_scheme(SOURCE_HOME / ".ssh")}"))',
            f'(deny file-read* (subpath "{_scheme(SOURCE_HOME / ".aws")}"))',
            f'(deny file-read* (subpath "{_scheme(SOURCE_HOME / ".config")}"))',
            f'(deny file-read* (subpath "{_scheme(SOURCE_HOME / "Library")}"))',
            '(deny file-read* (subpath "/private/var/root"))',
            '(deny file-read* (subpath "/Volumes"))',
            '(deny file-read* (subpath "/Applications"))',
            '(deny file-read* (subpath "/opt"))',
            '(deny file-read* (subpath "/usr/local"))',
            '(deny file-read* (subpath "/Library/Keychains"))',
            '(deny file-read* (subpath "/Library/Application Support"))',
            '(deny file-read* (subpath "/Library/Preferences"))',
            '(deny file-read* (subpath "/private/etc/ssh"))',
            '(deny file-read* (literal "/private/etc/sudoers"))',
        ]
    )
    return "\n".join(rules) + "\n"


def _run(
    command: list[str], *, cwd: Path, env: dict[str, str], profile: Path
) -> dict[str, Any]:
    completed = subprocess.run(
        ["/usr/bin/sandbox-exec", "-f", str(profile), *command],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        shell=False,
        check=False,
        timeout=300,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout[-16000:],
        "stderr": completed.stderr[-8000:],
    }


def run_temp_smoke() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="registered-workflow-smoke-") as raw:
        root = Path(raw).resolve()
        agent = _copy_source(root)
        _reject_source_symlinks(agent)
        source_manifest_sha256 = _source_manifest_digest(agent)
        python, site_packages = _copy_python_runtime(root)
        home = root / "home"
        hermes_home = home / ".hermes"
        temp_dir = root / "tmp"
        home.mkdir(mode=0o700)
        hermes_home.mkdir(mode=0o700)
        temp_dir.mkdir(mode=0o700)
        profile = root / "no-live.sb"
        profile.write_text(_sandbox_profile(root, python), encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C.UTF-8",
            "HOME": str(home),
            "HERMES_HOME": str(hermes_home),

            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(site_packages),

            "TMPDIR": str(temp_dir),
            "SQLITE_TMPDIR": str(temp_dir),
        }
        live_probes = [
            SOURCE_HOME / ".hermes" / "config.yaml",
            SOURCE_HOME / ".ssh",
            SOURCE_HOME / ".aws",
            SOURCE_HOME / ".config",
            SOURCE_HOME / ".hermes" / "ops" / "state",
            Path("/private/var/root"),
            Path("/Library/Keychains"),
            Path("/private/etc/ssh"),
        ]
        probe = _run(
            [
                str(python),
                "-c",
                (
                    "import pathlib,sys; denied=0\n"
                    "for raw in sys.argv[1:]:\n"
                    " p=pathlib.Path(raw)\n"
                    " try: list(p.iterdir()) if p.is_dir() else p.open('rb').read(1)\n"
                    " except (PermissionError, FileNotFoundError): denied += 1\n"
                    "print(denied); raise SystemExit(0 if denied == len(sys.argv)-1 else 2)"
                ),
                *map(str, live_probes),
            ],
            cwd=agent,
            env=env,
            profile=profile,
        )
        network_probe = _run(
            [
                str(python),
                "-c",
                (
                    "import socket\n"
                    "try: socket.socket().connect(('127.0.0.1',9))\n"
                    "except OSError as e: print(e.errno); raise SystemExit(0 if e.errno in (1,13) else 2)\n"
                    "raise SystemExit(2)"
                ),
            ],
            cwd=agent,
            env=env,
            profile=profile,
        )
        host_lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        denied_shm_name = f"/hrw.{os.getpid()}"
        denied_shm_fd = host_lib.shm_open(
            denied_shm_name.encode(), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600
        )
        if denied_shm_fd < 0:
            raise OSError(ctypes.get_errno(), "failed to create POSIX shm probe")
        os.close(denied_shm_fd)
        try:
            boundary_probe = _run(
                [
                    str(python),
                    "-c",
                    (
                        "import ctypes,os,subprocess,sys\n"
                        "denied=0\n"
                        "try: subprocess.run(['/usr/bin/true'],check=False)\n"
                        "except PermissionError: denied += 1\n"
                        "try: os.kill(os.getppid(),0)\n"
                        "except PermissionError: denied += 1\n"
                        "lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)\n"
                        "bootstrap=ctypes.c_uint.in_dll(lib,'bootstrap_port').value\n"
                        "port=ctypes.c_uint()\n"
                        "result=lib.bootstrap_look_up(bootstrap,b'com.apple.coreservices.launchservicesd',ctypes.byref(port))\n"
                        "denied += int(result != 0)\n"
                        "fd=lib.shm_open(sys.argv[1].encode(),os.O_RDONLY,0)\n"
                        "if fd < 0 and ctypes.get_errno() in (1,13): denied += 1\n"
                        "elif fd >= 0: os.close(fd)\n"
                        "print(denied); raise SystemExit(0 if denied == 4 else 2)"
                    ),
                    denied_shm_name,
                ],
                cwd=agent,
                env=env,
                profile=profile,
            )
        finally:
            host_lib.shm_unlink(denied_shm_name.encode())
        agent_tests = [
            "tests/tools/test_registered_workflow_capability_policy.py",
            "tests/tools/test_workflow_authority.py",
            "tests/tools/test_registered_local_workflow.py",

            "tests/tools/test_terminal_tool.py",
            "tests/tools/test_approval_tool.py",
            "tests/hermes_cli/test_kanban_capability_migrate.py",
            "tests/hermes_cli/test_kanban_intake_migrate.py",
            "tests/integration/test_review_ledger_registered_capability.py",
        ]
        agent_result = _run(
            [
                str(python),
                "-m",
                "pytest",
                *agent_tests,

                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
            ],
            cwd=agent,
            env=env,
            profile=profile,
        )

    live_denied = probe["returncode"] == 0
    network_denied = network_probe["returncode"] == 0
    hostile_boundaries_denied = boundary_probe["returncode"] == 0
    passed = (
        agent_result["returncode"] == 0

        and live_denied
        and network_denied
        and hostile_boundaries_denied
    )
    return {
        "schema": "registered-workflow-no-live-smoke/v2",
        "source_manifest_sha256": source_manifest_sha256,
        "mode": "copied-source-default-deny",
        "passed": passed,
        "network_denied": network_denied,
        "hostile_boundaries_denied": hostile_boundaries_denied,
        "live_home_denied": live_denied,
        "agent_tests": agent_result,

    }


def main() -> int:
    if sys.argv[1:] not in ([], ["--json"]):
        print(json.dumps({"passed": False, "error": "only --json is accepted"}))
        return 2
    result = run_temp_smoke()
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

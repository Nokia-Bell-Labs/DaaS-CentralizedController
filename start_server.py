#!/usr/bin/env python3
#
#                © 2026 Nokia
#                Licensed under the BSD 3-Clause Clear License
#                SPDX-License-Identifier: BSD-3-Clause-Clear
#
"""
Startup script for DaaS-CC API server.

This is the canonical entrypoint for running the controller.
It supports a few optional "troubleshooting" modes that previously lived in
separate scripts (fresh / force-fresh / clean, etc.).

Usage:
    python start_server.py [--config CONFIG_FILE]

Options:
    --config, -c          Path to configuration JSON file (default: poc-10G-config.json)
    --reload/--no-reload  Enable/disable uvicorn auto-reload (default: --no-reload)

Troubleshooting options:
    --fresh-imports       Clear already-imported daas_cc modules before creating the app
    --kill-port PORT      Kill any process LISTENING on PORT (Windows only; best-effort)
    --clear-pycache       Delete __pycache__ directories (under daas_cc/) before importing
    --dont-write-bytecode Set PYTHONDONTWRITEBYTECODE=1 for this run

Environment variable:
    DAAS_CONFIG_FILE      Path to configuration JSON file
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _kill_listeners_on_port_windows(port: int) -> None:
    """Best-effort: kill processes listening on the given port (Windows)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return

        pids_to_kill: set[str] = set()
        for line in result.stdout.splitlines():
            # Typical line: TCP    0.0.0.0:8000   0.0.0.0:0   LISTENING   1234
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    pid = parts[-1]
                    if pid.isdigit():
                        pids_to_kill.add(pid)

        for pid in sorted(pids_to_kill):
            try:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=5)
            except Exception:
                pass

        if pids_to_kill:
            time.sleep(2)
    except Exception:
        # Best-effort cleanup only
        return


def _clear_pycache_under_daas_cc(repo_root: Path) -> int:
    """Delete __pycache__ dirs under daas_cc/. Returns number removed."""
    daas_cc_path = repo_root / "daas_cc"
    removed = 0
    for root, dirs, _files in os.walk(str(daas_cc_path)):
        if "__pycache__" in dirs:
            cache_dir = Path(root) / "__pycache__"
            try:
                shutil.rmtree(cache_dir)
                removed += 1
            except Exception:
                pass
    return removed


def _clear_daas_cc_modules_from_sys_modules() -> int:
    to_remove = [k for k in list(sys.modules.keys()) if k.startswith("daas_cc")]
    for k in to_remove:
        try:
            del sys.modules[k]
        except Exception:
            pass
    return len(to_remove)


# Provide a module-level app for `uvicorn start_server:app --reload`.
# This keeps PyCharm/run-configs working when they expect an import string.
# IMPORTANT: This path uses the default config resolution (env var or default).
try:
    from daas_cc.config import ControllerConfig, get_config_file_path
    from daas_cc.services.listeners import make_app

    _cfg_for_import = ControllerConfig.from_json_file(get_config_file_path())
    app = make_app(_cfg_for_import)
except Exception as _e:  # pragma: no cover
    # If config is missing or invalid, keep the import from crashing so that
    # `python start_server.py ...` can still be used to debug.
    app = None  # type: ignore


def main() -> None:
    parser = argparse.ArgumentParser(description="Start DaaS-CC API server")
    parser.add_argument("--config", "-c", default=None, help="Path to configuration JSON file")

    reload_group = parser.add_mutually_exclusive_group()
    reload_group.add_argument("--reload", dest="reload", action="store_true", help="Restart the server when source files change")
    reload_group.add_argument("--no-reload", dest="reload", action="store_false", help="Start one server process (default)")
    parser.set_defaults(reload=False)

    parser.add_argument("--fresh-imports", action="store_true", help="Clear daas_cc modules from import cache before start")
    parser.add_argument("--kill-port", type=int, default=None, metavar="PORT", help="Kill process listening on PORT (Windows best-effort)")
    parser.add_argument("--clear-pycache", action="store_true", help="Delete __pycache__ dirs under daas_cc before start")
    parser.add_argument("--dont-write-bytecode", action="store_true", help="Set PYTHONDONTWRITEBYTECODE=1 for this run")

    args = parser.parse_args()

    repo_root = _project_root()
    os.chdir(str(repo_root))
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    if args.dont_write_bytecode:
        sys.dont_write_bytecode = True
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

    if args.kill_port is not None and os.name == "nt":
        _kill_listeners_on_port_windows(args.kill_port)

    if args.clear_pycache:
        _clear_pycache_under_daas_cc(repo_root)

    if args.fresh_imports:
        _clear_daas_cc_modules_from_sys_modules()

    from daas_cc.config import ControllerConfig, get_config_file_path
    from daas_cc.services.listeners import make_app

    # Config resolution:
    # - If user provided a path and it exists, use the absolute path directly.
    # - Otherwise fall back to the config helper (env var / default), which may
    #   resolve relative paths under daas_cc/examples/.
    config_file = None
    if args.config:
        candidate = Path(args.config)
        if not candidate.is_absolute():
            candidate = (repo_root / candidate).resolve()
        if candidate.exists():
            config_file = str(candidate)
        else:
            # Let ControllerConfig/load_config_from_json resolve relative paths.
            config_file = args.config
    else:
        config_file = get_config_file_path()

    cfg = ControllerConfig.from_json_file(config_file)

    # The module-level app was already built at import time with the same config.
    # Reuse it directly — do NOT call make_app() again, which would re-initialise
    # the controller and re-route all unrouted flows a second time.
    global app
    if app is None:
        # Module-level build failed (e.g. missing config); try again now.
        app = make_app(cfg)
    app_local = app

    # app is already set above.

    if __name__ == "__main__":
        import uvicorn

        print("=" * 70)
        print("Starting DaaS-CC API server")
        print("=" * 70)
        print(f"Configuration: {config_file or 'default (poc-10G-config.json)'}")
        print(f"Topology: {cfg.topology_file}")
        print(f"Pre-routed flows: {cfg.pre_routed_flows_file}")
        print(f"Pre-unrouted flows: {cfg.pre_unrouted_flows_file}")
        print(f"Scheduling policy: {cfg.scheduling_policy}")
        print(f"Routing policy: {cfg.routing_policy}")
        print(f"PSS link rate: {cfg.pss_link_rate} Mbps")
        print(f"PSS period slots: {cfg.pss_period_slots}")
        print(f"PSS output directory: {cfg.pss_output_dir}")
        print(f"Write PSS on startup: {cfg.push_pss_on_startup}")
        print()
        print("API will be available at: http://localhost:8000")
        print("API documentation at: http://localhost:8000/docs")
        print("=" * 70)
        print()

        if args.reload:
            # When reload is enabled, uvicorn requires an import string.
            uvicorn.run(
                "start_server:app",
                host="0.0.0.0",
                port=8000,
                reload=True,
                log_level="info",
            )
        else:
            uvicorn.run(
                app_local,
                host="0.0.0.0",
                port=8000,
                reload=False,
                log_level="info",
            )


if __name__ == "__main__":
    main()


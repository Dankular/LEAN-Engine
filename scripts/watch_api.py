from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from watchfiles import watch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
LEAN_ROOT = PROJECT_ROOT / "lean"
PROJECT_SOURCE_DIRS = {"src", "lean", "scripts", "tests"}
IGNORED_PARTS = {
    ".git",
    ".lake",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "node_modules",
}
ROOT_PROJECT_FILES = {"pyproject.toml", "features.json"}
LEAN_CONFIG_FILES = {"lakefile.toml", "lake-manifest.json", "lean-toolchain"}


def _relative_project_path(path_text: str) -> Path | None:
    path = Path(path_text).resolve()
    try:
        return path.relative_to(PROJECT_ROOT)
    except ValueError:
        return None


def _is_project_input(path_text: str) -> bool:
    relative_path = _relative_project_path(path_text)
    if relative_path is None or any(part in IGNORED_PARTS for part in relative_path.parts):
        return False
    if len(relative_path.parts) == 1:
        return relative_path.name in ROOT_PROJECT_FILES | PROJECT_SOURCE_DIRS
    return relative_path.parts[0] in PROJECT_SOURCE_DIRS


def _is_lean_input(path_text: str) -> bool:
    relative_path = _relative_project_path(path_text)
    return bool(relative_path and relative_path.parts and relative_path.parts[0] == "lean")


def _find_lake() -> str | None:
    configured = os.environ.get("LAKE_BIN")
    if configured:
        return shutil.which(configured) or (configured if Path(configured).is_file() else None)
    elan_lake = Path.home() / ".elan" / "bin" / ("lake.exe" if os.name == "nt" else "lake")
    if elan_lake.is_file():
        return str(elan_lake)
    return shutil.which("lake")


def _project_watch_paths() -> list[Path]:
    paths = [PROJECT_ROOT]
    for source_dir in PROJECT_SOURCE_DIRS:
        source_root = PROJECT_ROOT / source_dir
        if not source_root.is_dir():
            continue
        for current, subdirectories, _ in os.walk(source_root):
            subdirectories[:] = [name for name in subdirectories if name not in IGNORED_PARTS]
            paths.append(Path(current))
    return paths


def _build_project(changed_paths: list[str]) -> bool:
    python_roots = [SOURCE_ROOT, PROJECT_ROOT / "scripts", PROJECT_ROOT / "tests"]
    python_build = subprocess.run(
        [
            sys.executable,
            "-m",
            "compileall",
            "-q",
            *(str(path) for path in python_roots if path.is_dir()),
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if python_build.returncode != 0:
        print("Python source compilation failed; keeping the current API worker.", flush=True)
        return False

    lean_sources = sorted(
        path
        for path in changed_paths
        if _is_lean_input(path) and Path(path).suffix == ".lean" and Path(path).is_file()
    )
    lean_config_changed = any(
        _is_lean_input(path) and Path(path).name in LEAN_CONFIG_FILES
        for path in changed_paths
    )
    if lean_sources or lean_config_changed:
        lake = _find_lake()
        if lake is None:
            print("Lean files changed, but Lake is unavailable; keeping the current API worker.", flush=True)
            return False
        for lean_source in lean_sources:
            print(f"Type-checking Lean source {lean_source}.", flush=True)
            lean_check = subprocess.run(
                [lake, "env", "lean", str(Path(lean_source).resolve())],
                cwd=LEAN_ROOT,
                check=False,
            )
            if lean_check.returncode != 0:
                print("Lean source check failed; keeping the current API worker.", flush=True)
                return False
        if lean_config_changed:
            print("Building the Lake project configuration.", flush=True)
            lean_build = subprocess.run(
                [lake, "build", "--allow-empty"],
                cwd=LEAN_ROOT,
                check=False,
            )
            if lean_build.returncode != 0:
                print("Lake project build failed; keeping the current API worker.", flush=True)
                return False

    return True


def _start_server(port: int) -> subprocess.Popen[bytes]:
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "leanproof_service.api:app",
        "--app-dir",
        str(SOURCE_ROOT),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    process = subprocess.Popen(command, cwd=PROJECT_ROOT)
    print(f"API worker started (PID {process.pid})", flush=True)
    return process


def _stop_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    print(f"API worker stopped (PID {process.pid})", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the LEAN Engine API with source watching.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    print(f"Watching project sources and build inputs under {PROJECT_ROOT}", flush=True)
    print("Python changes are compiled; Lean files are type-checked before restart.", flush=True)
    process = _start_server(args.port)
    try:
        while True:
            watcher = watch(
                *_project_watch_paths(),
                watch_filter=lambda change, path: _is_project_input(path),
                debounce=500,
                step=100,
                yield_on_timeout=True,
                rust_timeout=500,
                recursive=False,
            )
            try:
                for changes in watcher:
                    if process.poll() is not None:
                        print(
                            f"API worker exited with status {process.returncode}; stopping watcher.",
                            flush=True,
                        )
                        return process.returncode or 1

                    changed_paths = sorted({path for _, path in changes if _is_project_input(path)})
                    if not changed_paths:
                        continue

                    print("Project changes detected; rebuilding before API restart:", flush=True)
                    for changed_path in changed_paths:
                        print(f"  {changed_path}", flush=True)
                    if _build_project(changed_paths):
                        _stop_server(process)
                        process = _start_server(args.port)
                    break
            finally:
                watcher.close()
    except KeyboardInterrupt:
        print("Stopping project watcher.", flush=True)
        return 0
    finally:
        _stop_server(process)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

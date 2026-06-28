from __future__ import annotations

"""
轻量自动编译监视器。

用途：开发时监听 Python 源码变化，每次保存后自动运行 compileall。
实现只依赖标准库，避免为了开发体验再引入额外 watcher 依赖。
"""

import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import time
from typing import Sequence


@dataclass
class Snapshot:
    files: dict[str, tuple[int, int]]


def _take_snapshot(paths: list[Path]) -> Snapshot:
    files: dict[str, tuple[int, int]] = {}
    for root in paths:
        if not root.exists():
            continue
        if root.is_file() and root.suffix == ".py":
            stat = root.stat()
            files[str(root)] = (stat.st_mtime_ns, stat.st_size)
            continue
        for path in root.rglob("*.py"):
            if _should_skip(path):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files[str(path)] = (stat.st_mtime_ns, stat.st_size)
    return Snapshot(files=files)


def _should_skip(path: Path) -> bool:
    return any(part in {"__pycache__", ".git", ".pytest_cache", ".codeclaw"} for part in path.parts)


def _changed(before: Snapshot, after: Snapshot) -> bool:
    return before.files != after.files


def _run_compile(target: str) -> int:
    started = time.time()
    command = [sys.executable, "-m", "compileall", "-q", target]
    print(f"\n[watch] running: {' '.join(command)}", flush=True)
    completed = subprocess.run(command, check=False)
    elapsed = time.time() - started
    if completed.returncode == 0:
        print(f"[watch] compile ok ({elapsed:.2f}s)", flush=True)
    else:
        print(f"[watch] compile failed exit={completed.returncode} ({elapsed:.2f}s)", flush=True)
    return completed.returncode


def watch_compile(
    *,
    watch_paths: list[str],
    compile_target: str,
    interval: float,
    debounce: float,
    run_once_first: bool,
) -> int:
    paths = [Path(path).resolve() for path in watch_paths]
    target = str(Path(compile_target))
    print("[watch] CodeClaw auto compile watcher", flush=True)
    print(f"[watch] watching: {', '.join(str(path) for path in paths)}", flush=True)
    print(f"[watch] target: {target}", flush=True)
    print("[watch] press Ctrl+C to stop", flush=True)

    last_status = 0
    if run_once_first:
        last_status = _run_compile(target)

    snapshot = _take_snapshot(paths)
    try:
        while True:
            time.sleep(interval)
            current = _take_snapshot(paths)
            if not _changed(snapshot, current):
                continue
            snapshot = current
            print("[watch] change detected, waiting for save to settle...", flush=True)
            time.sleep(debounce)
            snapshot = _take_snapshot(paths)
            last_status = _run_compile(target)
    except KeyboardInterrupt:
        print("\n[watch] stopped", flush=True)
    return last_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch Python files and re-run compileall on changes.")
    parser.add_argument(
        "--watch",
        action="append",
        default=None,
        help="Path to watch. Can be repeated. Default: src",
    )
    parser.add_argument("--target", default="src", help="compileall target. Default: src")
    parser.add_argument("--interval", type=float, default=0.8, help="Polling interval in seconds. Default: 0.8")
    parser.add_argument("--debounce", type=float, default=0.4, help="Delay after change before compiling. Default: 0.4")
    parser.add_argument("--no-initial", action="store_true", help="Do not compile once on startup.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return watch_compile(
        watch_paths=args.watch or ["src"],
        compile_target=args.target,
        interval=max(0.1, args.interval),
        debounce=max(0.0, args.debounce),
        run_once_first=not bool(args.no_initial),
    )


if __name__ == "__main__":
    raise SystemExit(main())

"""
Top-level launcher: runs AURA's services as separate processes, so you
don't need one terminal each.

Two modes, and the difference between them is the Body — which comes down
to the camera. On Windows OpenCV takes exclusive access to the webcam, so
the two Bodies cannot both own it: the desktop mode's perception service
holds it, the web mode's web Body does. Memory and Mind are byte-for-byte
the same services in both modes. That is the entire point of the split.

  default    perception, episodic memory writer, mind, desktop body
  --web      episodic memory writer, mind, web body

Desktop mode (default) — the Body owns the console's main thread:

    The Body is deliberately **not** one of the child processes. It is
    the only component that talks to the human, so it must own the
    console's stdin — and a multiprocessing child on Windows does not
    reliably get a usable one. `input()` there raises `EOFError` on the
    very first prompt, which killed the conversation loop the moment AURA
    started (observed 2026-10-06). The Body therefore runs on this
    process's main thread, and supervision of the other three runs on a
    daemon thread.

Web mode (--web) — the Body owns a port:

    The web Body talks to a browser, not the console, so it needs no
    stdin and runs as an ordinary child process. The main thread keeps the
    supervision loop, which is where Ctrl+C has to land.

Requires: Redis reachable at localhost:6379, Postgres reachable per the
DSN in memory/episodic/store.py, and GEMINI_API_KEY set in the
environment. Preflight checks below fail fast with a clear message
instead of letting each service crash separately with a raw stack trace.

Run with:   uv run python run_aura.py            (desktop body)
            uv run python run_aura.py --web      (browser at 127.0.0.1:8000)
Stop with:  Ctrl+C (shuts everything down cleanly)
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

WEB_URL = "http://127.0.0.1:8000"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_aura.py",
        description="Start AURA. Without --web the desktop Body talks to "
        "the console; with --web a browser interface is served instead.",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help=(
            "run the web Body and serve the interface on "
            f"{WEB_URL}. Replaces the desktop Body and the perception "
            "service, because only one process can hold the camera."
        ),
    )
    return parser.parse_args(argv)


def _preflight_checks() -> list[str]:
    """Returns human-readable problems found. Empty list means all clear."""
    problems: list[str] = []

    if not os.environ.get("GEMINI_API_KEY"):
        problems.append(
            "GEMINI_API_KEY is not set. Get a free key at "
            "https://aistudio.google.com/apikey and export it — "
            "the Mind can't reason or speak without it."
        )

    try:
        import redis as redis_lib

        redis_lib.Redis(host="localhost", port=6379, socket_connect_timeout=2).ping()
    except Exception as exc:
        problems.append(
            f"Redis is not reachable at localhost:6379 ({exc}). "
            "On Windows, start Memurai: net start Memurai "
            "(run PowerShell as Administrator). "
            "On Linux: sudo systemctl start redis"
        )

    try:
        import psycopg

        # _CONNECT_TIMEOUT is imported deliberately, despite the leading
        # underscore. This check and the store used to carry the same
        # timeout as two independent literals (both 2), and that is exactly
        # how they came to be too tight together: session 6's preflight
        # reported "Postgres is not reachable" while the server was up and
        # answering in 1.34 s, because this check gave up first. Sharing
        # one constant means the preflight can never disagree with the
        # connection the services actually make.
        from memory.episodic.store import _CONNECT_TIMEOUT, DEFAULT_DSN

        with psycopg.connect(DEFAULT_DSN, connect_timeout=_CONNECT_TIMEOUT):
            pass
    except Exception as exc:
        problems.append(
            f"Postgres is not reachable ({exc}). "
            "On Windows, start the service: net start postgresql-x64-18 "
            "(run PowerShell as Administrator; check the exact service name "
            "with: Get-Service *postgres*). "
            "On Linux: sudo systemctl start postgresql. "
            "Also confirm AURA_PG_PASSWORD is set in this terminal."
        )

    return problems


def _run_perception() -> None:
    from perception.service import main as perception_main

    perception_main()


def _run_memory_writer() -> None:
    from memory.episodic.writer import main as writer_main

    writer_main()


def _run_mind() -> None:
    from mind.reasoning_loop import main as mind_main

    mind_main()


def _run_body() -> None:
    from bodies.desktop.run import main as body_main

    body_main()


def _run_web_body() -> None:
    from bodies.web.app import main as web_body_main

    web_body_main()


def _warn_for_dead_workers(
    processes: dict[str, mp.Process],
    already_warned: set[str],
) -> list[str]:
    """
    Print one degraded-mode warning per service that has stopped.

    Returns the names newly warned about on this call, and records them in
    `already_warned` so a service that stays down is reported once rather
    than on every pass of the supervisor loop.

    Extracted from `main()` so the warn-once behaviour can be tested
    without starting real processes (step 6's verification).
    """
    newly_warned: list[str] = []

    for name, proc in processes.items():
        if proc.is_alive() or name in already_warned:
            continue

        # Deliberately explicit about the two things that made G-5
        # invisible: the service is gone for good, and this warning
        # appears exactly once. A generic "stopped unexpectedly" reads
        # like something that might recover on its own. It will not.
        still_running = [n for n, p in processes.items() if p.is_alive()]
        print(
            f"\nWARNING: AURA is running DEGRADED. The '{name}' service has "
            f"stopped (exit code {proc.exitcode}) and will NOT be restarted "
            "— nothing here can recover it without a full restart. Check "
            "its output above for the actual error."
        )
        print(
            f"Still running: {', '.join(still_running) or 'nothing'}. "
            f"Press Ctrl+C and start AURA again to bring '{name}' back."
        )
        already_warned.add(name)
        newly_warned.append(name)

    return newly_warned


def _supervise(
    processes: dict[str, mp.Process],
    already_warned: set[str],
) -> None:
    """
    Watches the worker processes and reports any that die.

    Runs on a daemon thread in desktop mode, because the main thread
    there belongs to the interactive Body — which blocks reading stdin for
    the whole session. In web mode the main thread runs this loop inline.
    """
    while True:
        _warn_for_dead_workers(processes, already_warned)
        time.sleep(2)


def _start_workers(targets: dict) -> dict[str, mp.Process]:
    processes: dict[str, mp.Process] = {}
    for name, target in targets.items():
        proc = mp.Process(target=target, name=name, daemon=True)
        proc.start()
        processes[name] = proc
        print(f"Started {name} (pid {proc.pid})")
        time.sleep(1)  # stagger startup so services don't race on init
    return processes


def _stop_workers(processes: dict[str, mp.Process]) -> None:
    print("\nShutting down...")
    for proc in processes.values():
        proc.terminate()
    for proc in processes.values():
        proc.join(timeout=5)
    print("Stopped.")


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    problems = _preflight_checks()
    if problems:
        print("Cannot start AURA — fix the following first:\n")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)

    already_warned: set[str] = set()

    if args.web:
        # No perception service: the web Body owns the camera, and two
        # processes cannot both hold it. Everything else is unchanged.
        targets = {
            "memory-writer": _run_memory_writer,
            "mind": _run_mind,
            "web-body": _run_web_body,
        }
        processes = _start_workers(targets)

        print(f"\nAURA is running. Open {WEB_URL} in your browser.\n")
        try:
            _supervise(processes, already_warned)
        except KeyboardInterrupt:
            pass
        finally:
            _stop_workers(processes)
        return

    # The Body is deliberately absent from this dict: it needs the console's
    # stdin, so it runs on the main thread below rather than as a spawn
    # child. See the module docstring.
    targets = {
        "perception": _run_perception,
        "memory-writer": _run_memory_writer,
        "mind": _run_mind,
    }
    processes = _start_workers(targets)

    print("\nAURA is running. Ctrl+C to stop everything.\n")

    threading.Thread(
        target=_supervise,
        args=(processes, already_warned),
        name="supervisor",
        daemon=True,
    ).start()

    try:
        # Blocks for the life of the session: this is the press-Enter-to-
        # talk loop plus ambient narration, on the real console.
        _run_body()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_workers(processes)


if __name__ == "__main__":
    main()

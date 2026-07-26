#!/usr/bin/env python
"""Queue consumer entrypoint.

Runs as its own process, separate from uvicorn (TECH_SPEC 8.2):

    python worker.py

The separation is deliberate - request handling stays decoupled from
long-running task execution.
"""

from __future__ import annotations

import asyncio

from app.queue.job_queue import run_worker

if __name__ == "__main__":
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        # Windows' proactor loop has no add_signal_handler, so Ctrl-C arrives
        # here instead of through the SIGINT handler.
        pass

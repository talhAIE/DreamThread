"""Run with python -m app.workers.main [--once]."""

import argparse
import asyncio
import logging
import signal

from app.config import Settings
from app.db import Database
from app.loop import create_loop
from app.workers.queue import Queue
from app.workers.runner import Worker


async def main(once):
    settings = Settings()
    db, queue = Database(settings), Queue(settings)
    try:
        if await db.probe() != "ok":
            raise RuntimeError("Runtime database is not ready")
        await queue.check_role()
        worker = Worker(settings, db, queue)
        if once:
            await worker.maintain_once()
            print("Job processed:", await worker.run_once())
            return
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
        await worker.serve(stop)
    finally:
        await queue.close()
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        with asyncio.Runner(loop_factory=create_loop) as runner:
            runner.run(main(args.once))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        logging.error("Worker startup/execution failed type=%s", type(error).__name__)
        raise SystemExit(1) from None

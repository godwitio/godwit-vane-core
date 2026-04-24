"""Daemon-thread entry point for the seeder.

Wraps `Seeder.run()` in a top-level try/except so that an uncaught
exception in the bootstrap seeder never kills the live pipeline's
process.
"""
from log import Logger
from services.seeder.seeder import Seeder


def run_seeder_safely(seeder: Seeder, logger: Logger) -> None:
    try:
        seeder.run()
    except Exception as e:
        logger(f"[seed] aborted: {e}")

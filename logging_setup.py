"""Structured logging setup for DarkForest Hunter.

One configuration entry point used by ``run.py``. Diagnostic messages
(network errors, rate-limit waits, scanner warnings) flow through the stdlib
``logging`` module under the ``darkforest`` logger — independent of the
user-facing ``log_callback`` narrative the pipeline/TUI uses for progress.

Design:
- Console handler: WARNING by default (only surface problems), DEBUG if verbose.
- Optional file handler: always DEBUG (full diagnostic trail).
- Idempotent: safe to call multiple times.
"""
from __future__ import annotations

import logging
from pathlib import Path

LOGGER_NAME = "darkforest"
_CONFIGURED = False

_FMT = logging.Formatter(
    "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S"
)


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    """Return a child logger under the darkforest namespace."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name != LOGGER_NAME else LOGGER_NAME)


def configure_logging(verbose: bool = False, log_file: str | None = None,
                      force: bool = False) -> logging.Logger:
    """Configure the ``darkforest`` logger (idempotent unless ``force``).

    Args:
        verbose: console shows DEBUG; else WARNING.
        log_file: if set, also write DEBUG-level diagnostics to this file.
        force: re-run setup even if already configured (used by tests).
    """
    global _CONFIGURED
    logger = logging.getLogger(LOGGER_NAME)
    if _CONFIGURED and not force:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    # Clear any prior handlers (force or first call with leftover handlers).
    for h in list(logger.handlers):
        logger.removeHandler(h)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(_FMT)
    logger.addHandler(console)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_FMT)
        logger.addHandler(fh)

    _CONFIGURED = True
    return logger

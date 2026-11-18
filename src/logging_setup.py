"""
logging_setup.py — Shared logger factory for all HessGrp scripts.

Usage:
    from logging_setup import make_logger

    log = make_logger("price_refresh", DATA_DIR / "price_refresh.log")
    log = make_logger("moat_score",    DATA_DIR / "moat_score.log", stream_level=None)
    log = make_logger("av_fcff_2",     log_path, stream_level=logging.WARNING, fmt=LONG_FMT)

    LONG_FMT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
"""

import logging
import sys
from pathlib import Path

SHORT_FMT = "%(asctime)s  %(levelname)-7s  %(message)s"
LONG_FMT  = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
DATEFMT   = "%Y-%m-%d %H:%M:%S"


def make_logger(
    name: str,
    log_file: "str | Path | None" = None,
    *,
    file_level: int = logging.DEBUG,
    stream_level: "int | None" = logging.INFO,
    fmt: str = SHORT_FMT,
    datefmt: str = DATEFMT,
) -> logging.Logger:
    """
    Build and return a configured Logger.

    Args:
        name         Logger name (typically the script name or __name__).
        log_file     Path to the log file. Omit for stream-only logging.
        file_level   Level for the file handler (default DEBUG).
        stream_level Level for the stdout handler. Pass None to suppress stdout.
        fmt          Log format string (default SHORT_FMT).
        datefmt      Date format string.

    The returned logger has propagate=False so records do not bubble up to the
    root logger. Call this once at module level and reuse the returned object.
    """
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    log.propagate = False

    formatter = logging.Formatter(fmt, datefmt=datefmt)

    if log_file is not None:
        # Self-heal the parent directory the same way hg_dcflib.py's own
        # logger setup already does (os.makedirs(..., exist_ok=True) before
        # opening its FileHandler) -- this factory previously assumed the
        # directory already existed, which is true on a real dev checkout
        # (~/HessGrp/data has always been there) but not in a fresh
        # container: DATA_DIR resolves via Path.home(), and the container's
        # HOME (/home/hessgrp) has no HessGrp subdirectory at all. Found
        # live 2026-08-26: `import moat_score` alone crashes with
        # FileNotFoundError at module load time inside hessgrp-unified's
        # container -- see docs/known_errors.md.
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(log_file), encoding="utf-8")
        fh.setLevel(file_level)
        fh.setFormatter(formatter)
        log.addHandler(fh)

    if stream_level is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(stream_level)
        sh.setFormatter(formatter)
        log.addHandler(sh)

    return log

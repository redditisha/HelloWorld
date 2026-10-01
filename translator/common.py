"""Minimal stand-in for the PC app's local/common.py: settings come from
environment variables, logs go to stdout."""

import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stdout)


def setting(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

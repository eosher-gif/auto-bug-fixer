"""Smoke tests for the structlog configuration module."""
from __future__ import annotations

import logging

import pytest

from auto_bug_fixer.logging_setup import configure_logging, get_logger


@pytest.mark.parametrize("fmt", ["json", "console"])
def test_configure_logging_does_not_raise(fmt: str) -> None:
    configure_logging(level="DEBUG", fmt=fmt)
    log = get_logger("test")
    log.info("hello", x=1)
    log.warning("warn")


def test_unknown_level_falls_back_without_raising() -> None:
    configure_logging(level="NOT_A_LEVEL", fmt="json")
    log = get_logger("test")
    log.info("ok")
    log.warning("warn")
    assert isinstance(logging.getLogger().level, int)

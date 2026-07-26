"""Expose destructive database fixtures to the sibling performance suite."""

from tests.integration.conftest import database_harness

__all__ = ("database_harness",)

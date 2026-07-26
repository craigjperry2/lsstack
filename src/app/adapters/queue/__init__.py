"""SAQ outbox relay and worker integration."""

from app.adapters.queue.worker import build_saq_plugin

__all__ = ["build_saq_plugin"]

"""Phase 19.3: production wiring for ``on_content_changed``.

Importing this module registers a listener that enqueues a ``posting.tag``
Celery task every time :func:`src.jobs.enrich.enrich_posting` inserts a
new ``JobSnapshot``. The wiring lives in its own module so:

* unit tests of :mod:`src.jobs.enrich` keep the dependency-injection-style
  ``reset_listeners()`` API and don't get the Celery import as a side
  effect;
* the worker and the web app import this once (via :mod:`src.tasks.tasks`)
  to opt into the production behaviour.

The listener swallows enqueue failures (broker hiccup) and logs them;
under no circumstances should a content-change event fail to register.
The snapshot's ``tags_status='pending'`` default already guarantees the
fast-path falls back to slow scoring until a successful retag lands, so
losing one enqueue is an availability dip, not a correctness bug.
"""

from __future__ import annotations

import logging

from src.jobs.enrich import ContentChangedEvent, on_content_changed

logger = logging.getLogger("autoapply.jobs.listeners")

_installed = False


def install() -> None:
    """Idempotently register the production content-changed listener.

    Called from :mod:`src.tasks.tasks` import time so the worker boot
    and the web app pick it up. ``reset_listeners`` (test helper) does
    NOT clear ``_installed`` so a second ``install()`` after a test
    that called ``reset_listeners`` correctly re-registers.
    """
    global _installed
    if _installed:
        return
    on_content_changed(_enqueue_posting_tag)
    _installed = True


def reinstall_for_tests() -> None:
    """Test-only helper: reset the install latch so a unit test that
    calls :func:`src.jobs.enrich.reset_listeners` can re-arm production
    wiring afterwards."""
    global _installed
    _installed = False


def _enqueue_posting_tag(event: ContentChangedEvent) -> None:
    try:
        from src.tasks.app import celery_app  # noqa: PLC0415 -- defer until first event
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "posting.tag listener: celery app unavailable; skipping enqueue: %s", exc
        )
        return

    try:
        celery_app.send_task(
            "posting.tag",
            kwargs={"snapshot_id": str(event.snapshot_id)},
            queue="search",
        )
    except Exception as exc:  # noqa: BLE001 -- listener is best-effort
        logger.warning(
            "posting.tag listener: enqueue failed for snapshot %s: %s",
            event.snapshot_id,
            exc,
        )


__all__ = ["install", "reinstall_for_tests"]

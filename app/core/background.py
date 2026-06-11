import asyncio
import logging
from typing import Coroutine, Any

logger = logging.getLogger(__name__)

# Strong references so tasks aren't GC'd before they finish.
_live: set[asyncio.Task] = set()


def enqueue(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Schedule a coroutine as a fire-and-forget background task.

    Keeps a strong reference until the task completes and logs any
    unhandled exception so failures aren't silently swallowed.
    """
    task = asyncio.create_task(coro)
    _live.add(task)
    task.add_done_callback(_on_done)
    return task


def _on_done(task: asyncio.Task) -> None:
    _live.discard(task)
    if not task.cancelled() and (exc := task.exception()):
        logger.error("Background task %r failed: %s", task.get_name(), exc, exc_info=exc)

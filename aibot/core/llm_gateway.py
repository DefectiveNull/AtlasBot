"""Concurrency-aware gateway in front of the OpenAI-compatible client.

The inference backend (e.g. W&B Inference) caps the number of *concurrent*
requests. With effectively unlimited tokens but a hard concurrency ceiling, the
binding constraint shifts from cost to slots — so every LLM call in the bot is
funneled through a single :class:`LLMGateway` that:

1. Bounds global concurrency with a :class:`PrioritySemaphore` so the backend's
   concurrent-request limit is never exceeded.
2. Prioritizes interactive (user-facing) traffic over background work, so a live
   ``@mention`` reply never waits behind summary/profile regeneration or a
   ``/research`` fan-out.

The gateway is a drop-in replacement for ``AsyncOpenAI``: callers keep using
``client.chat.completions.create(...)``. The only addition is an optional
keyword-only ``priority`` argument, which the gateway consumes (it is never
forwarded to the underlying client). Calls that omit it default to
``Priority.INTERACTIVE`` so existing call sites need no changes.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
from enum import IntEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


async def create_json_completion(client: Any, **kwargs: Any) -> Any:
    """``chat.completions.create`` with ``response_format=json_object``,
    retried without it when the provider rejects the parameter.

    Support for JSON mode varies across OpenAI-compatible backends (vLLM,
    LiteLLM, Ollama, ...), so a rejection must degrade to a plain call rather
    than fail the request. Callers' parsers also tolerate prose-wrapped JSON,
    making this belt-and-braces, not a hard dependency.
    """
    try:
        return await client.chat.completions.create(
            response_format={"type": "json_object"}, **kwargs
        )
    except Exception as e:
        # Degrade to a plain call only when the provider rejected the *request*
        # (HTTP 400 / BadRequest) — that is how OpenAI-compatible backends
        # signal an unsupported `response_format`. Auth (401), rate-limit (429),
        # and network/timeout errors must propagate instead of being silently
        # retried (which would double the slot cost and mask the real error).
        #
        # Gating on the 400 status (not a substring of the message) is what
        # makes this robust: a legitimate "structured outputs not supported"
        # rejection whose wording doesn't happen to contain "json"/"unsupported"
        # still degrades, instead of escaping and killing the whole call.
        status = getattr(e, "status_code", None)
        is_request_rejected = status == 400 or type(e).__name__ in (
            "BadRequestError",
            "UnprocessableEntityError",
        )
        if is_request_rejected:
            logger.warning(
                f"JSON response_format not accepted ({type(e).__name__}: {e}); retrying without it"
            )
            return await client.chat.completions.create(**kwargs)
        raise


class Priority(IntEnum):
    """Request priority. Lower value = served first when slots are scarce."""

    INTERACTIVE = 0  # live @mention / prefix replies (default)
    RESEARCH = 1  # /research fan-out (user is waiting, but it's a burst)
    LURK = 2  # passive lurk evaluations
    BACKGROUND = 3  # summaries, profiles, status rotation


class PrioritySemaphore:
    """An asyncio semaphore that grants permits in priority order.

    Behaves like :class:`asyncio.Semaphore` except that, when permits are
    exhausted, waiters are released in ascending ``(priority, arrival)`` order
    rather than strict FIFO. Within a single priority, ordering is FIFO (ties
    broken by a monotonic arrival counter).

    Cancellation-safe: a waiter cancelled while pending is reaped lazily; a
    waiter cancelled *after* being granted a permit hands that permit to the
    next waiter instead of leaking it.
    """

    def __init__(self, value: int) -> None:
        if value < 1:
            raise ValueError("PrioritySemaphore value must be >= 1")
        self._value = value
        # Min-heap of (priority, seq, future). seq keeps the heap total-ordered
        # so futures are never compared against each other.
        self._waiters: list[tuple[int, int, asyncio.Future]] = []
        self._counter = itertools.count()

    @property
    def available(self) -> int:
        """Number of free permits right now."""
        return self._value

    @property
    def waiting(self) -> int:
        """Number of live (not yet granted, not cancelled) waiters."""
        return sum(1 for _, _, fut in self._waiters if not fut.done())

    async def acquire(self, priority: int = Priority.INTERACTIVE) -> None:
        """Acquire a permit, blocking in priority order until one is free.

        A free permit (``self._value > 0``) implies there are no waiters,
        because :meth:`release` hands permits directly to waiters and only
        increments ``_value`` when the heap is empty. So taking a permit on the
        fast path can never jump ahead of a queued waiter.
        """
        if self._value > 0:
            self._value -= 1
            return

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        heapq.heappush(self._waiters, (int(priority), next(self._counter), fut))

        try:
            await fut
        except asyncio.CancelledError:
            if not fut.cancelled():
                # We were granted a permit between release() and our resumption,
                # but got cancelled before we could use it. Pass it on.
                self._wake_next_or_release()
            raise

    def release(self) -> None:
        """Release a permit, handing it to the highest-priority live waiter."""
        self._wake_next_or_release()

    def _wake_next_or_release(self) -> None:
        """Give one permit to the next live waiter, else return it to the pool."""
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                # Permit transfers to this waiter; _value stays unchanged.
                fut.set_result(None)
                return
            # Waiter was cancelled while pending — skip it (lazy reap).
        self._value += 1


class _Completions:
    """Wraps ``client.chat.completions`` to gate ``create`` on the semaphore."""

    def __init__(self, gateway: LLMGateway) -> None:
        self._gateway = gateway

    async def create(self, *, priority: Priority = Priority.INTERACTIVE, **kwargs: Any) -> Any:
        """Acquire a slot at ``priority``, then delegate to the real client.

        ``priority`` is consumed here and never forwarded downstream.
        """
        sem = self._gateway._semaphore
        acquired = False
        try:
            await sem.acquire(int(priority))
            acquired = True
            return await self._gateway._client.chat.completions.create(**kwargs)
        finally:
            if acquired:
                sem.release()


class _Chat:
    def __init__(self, gateway: LLMGateway) -> None:
        self.completions = _Completions(gateway)


class LLMGateway:
    """Concurrency- and priority-aware front for an ``AsyncOpenAI`` client.

    Usage is identical to the wrapped client::

        gateway.chat.completions.create(model=..., messages=...)              # interactive
        gateway.chat.completions.create(..., priority=Priority.BACKGROUND)    # deprioritized

    Any attribute other than ``chat`` (e.g. ``models``, a future ``embeddings``)
    falls through to the underlying client unchanged — those are not gated.
    """

    def __init__(self, client: AsyncOpenAI, max_concurrency: int = 8) -> None:
        self._client = client
        self.max_concurrency = max_concurrency
        self._semaphore = PrioritySemaphore(max_concurrency)
        self.chat = _Chat(self)

    def stats(self) -> dict[str, int]:
        """Snapshot of pool state for logging / introspection."""
        return {
            "max": self.max_concurrency,
            "available": self._semaphore.available,
            "waiting": self._semaphore.waiting,
        }

    def __getattr__(self, name: str) -> Any:
        # Only invoked for attributes not set on the instance (e.g. .models).
        # Use __dict__ to avoid recursing through __getattr__ before _client set.
        client = self.__dict__.get("_client")
        if client is None:
            raise AttributeError(name)
        return getattr(client, name)

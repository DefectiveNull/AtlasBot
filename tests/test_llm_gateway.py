"""Tests for the concurrency-aware LLM gateway."""

from __future__ import annotations

import asyncio

import pytest

from aibot.core.llm_gateway import LLMGateway, Priority, PrioritySemaphore


async def _yield_until(predicate, max_ticks: int = 2000) -> None:
    """Spin the event loop until predicate() is true (bounded, to avoid hangs)."""
    for _ in range(max_ticks):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was never met")


# --- Fake OpenAI-compatible client -----------------------------------------


class _FakeCompletions:
    def __init__(self, create_fn):
        self.create = create_fn


class _FakeChat:
    def __init__(self, create_fn):
        self.completions = _FakeCompletions(create_fn)


class FakeClient:
    """Minimal stand-in exposing chat.completions.create plus a passthrough attr."""

    def __init__(self, create_fn=None):
        self.chat = _FakeChat(create_fn)
        self.models = "MODELS_API"


# --- PrioritySemaphore ------------------------------------------------------


class TestPrioritySemaphore:
    def test_rejects_non_positive_value(self):
        with pytest.raises(ValueError):
            PrioritySemaphore(0)

    async def test_fast_path_decrements_available(self):
        sem = PrioritySemaphore(2)
        assert sem.available == 2
        await sem.acquire()
        assert sem.available == 1
        await sem.acquire()
        assert sem.available == 0
        sem.release()
        assert sem.available == 1

    async def test_grants_in_priority_then_fifo_order(self):
        sem = PrioritySemaphore(1)
        await sem.acquire()  # hold the only permit; subsequent acquires queue

        order: list[str] = []

        async def waiter(priority: Priority, label: str) -> None:
            await sem.acquire(priority)
            order.append(label)

        # Arrival order deliberately not priority order.
        t_bg = asyncio.create_task(waiter(Priority.BACKGROUND, "bg"))
        t_i1 = asyncio.create_task(waiter(Priority.INTERACTIVE, "i1"))
        t_i2 = asyncio.create_task(waiter(Priority.INTERACTIVE, "i2"))
        t_lurk = asyncio.create_task(waiter(Priority.LURK, "lurk"))

        await _yield_until(lambda: sem.waiting == 4)

        # Release four permits; each should go to the best live waiter.
        for _ in range(4):
            sem.release()
        await asyncio.gather(t_bg, t_i1, t_i2, t_lurk)

        # Interactive first (FIFO within tier: i1 before i2), then lurk, then bg.
        assert order == ["i1", "i2", "lurk", "bg"]

    async def test_permit_restored_when_pending_waiter_cancelled(self):
        sem = PrioritySemaphore(1)
        await sem.acquire()  # available == 0

        t = asyncio.create_task(sem.acquire())
        await _yield_until(lambda: sem.waiting == 1)

        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

        # The dead waiter must not consume the released permit.
        sem.release()
        assert sem.available == 1

    async def test_granted_then_cancelled_does_not_leak_permit(self):
        sem = PrioritySemaphore(1)
        await sem.acquire()  # available == 0

        t = asyncio.create_task(sem.acquire())
        await _yield_until(lambda: sem.waiting == 1)

        # Grant the permit to t's future, then cancel before t can resume.
        sem.release()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

        # The granted-but-unused permit should be handed back, not lost.
        assert sem.available == 1

    async def test_multislot_grants_in_priority_then_fifo_order(self):
        """value >= 2: saturate both slots, enqueue mixed-priority waiters, then
        free slots one at a time and assert priority-then-FIFO grant order.

        The single-permit test can't catch a regression that only manifests when
        the heap holds waiters while *multiple* permits are in flight; this one
        keeps two permits held throughout so every grant comes off the heap.
        """
        sem = PrioritySemaphore(2)
        await sem.acquire()  # hold slot 1
        await sem.acquire()  # hold slot 2 — available == 0, all further acquires queue
        assert sem.available == 0

        order: list[str] = []

        async def waiter(priority: Priority, label: str) -> None:
            await sem.acquire(priority)
            order.append(label)

        # Arrival order is deliberately scrambled vs. priority order.
        tasks = [
            asyncio.create_task(waiter(Priority.LURK, "lurk")),  # arrives 1st
            asyncio.create_task(waiter(Priority.BACKGROUND, "bg")),  # arrives 2nd
            asyncio.create_task(waiter(Priority.INTERACTIVE, "i1")),  # arrives 3rd
            asyncio.create_task(waiter(Priority.RESEARCH, "research")),  # arrives 4th
            asyncio.create_task(waiter(Priority.INTERACTIVE, "i2")),  # arrives 5th
        ]
        await _yield_until(lambda: sem.waiting == 5)

        # Release the two held permits plus the three that free as waiters finish.
        # Each release hands the permit to the best remaining live waiter.
        for _ in range(5):
            sem.release()
        await asyncio.gather(*tasks)

        # Strict priority asc; FIFO within INTERACTIVE (i1 before i2).
        assert order == ["i1", "i2", "research", "lurk", "bg"]

    async def test_multislot_higher_priority_preempts_queued_lower(self):
        """With 2 slots saturated, a *later-arriving* high-priority waiter is
        granted ahead of an *earlier* low-priority waiter once a slot frees.

        Drives release one permit at a time so ordering is observed, not assumed.
        """
        sem = PrioritySemaphore(2)
        await sem.acquire()
        await sem.acquire()

        order: list[str] = []

        async def waiter(priority: Priority, label: str) -> None:
            await sem.acquire(priority)
            order.append(label)

        t_bg = asyncio.create_task(waiter(Priority.BACKGROUND, "bg"))
        await _yield_until(lambda: sem.waiting == 1)
        # bg is already queued; now a live reply shows up behind it.
        t_int = asyncio.create_task(waiter(Priority.INTERACTIVE, "int"))
        await _yield_until(lambda: sem.waiting == 2)

        # Free one slot: the interactive waiter must win despite arriving later.
        sem.release()
        await _yield_until(lambda: order == ["int"])
        assert not t_bg.done()  # bg still starved while higher priority pending

        # Free the second slot: bg finally gets it.
        sem.release()
        await asyncio.gather(t_bg, t_int)
        assert order == ["int", "bg"]

    async def test_low_priority_waiter_starves_under_sustained_high_demand(self):
        """No-aging contract: a BACKGROUND waiter is NOT granted while a steady
        stream of INTERACTIVE acquirers keeps demanding the single slot.

        This documents intended behavior — the semaphore has no anti-starvation
        aging. A live-traffic-saturated channel will indefinitely deprioritize
        background work, which is the deliberate design (interactive latency wins).
        Ordering is driven by manual release, not wall-clock, so it's deterministic.

        Each INTERACTIVE acquirer models a real request: it acquires a slot, runs,
        then releases it (mimicking a completed LLM call). Crucially, a *fresh*
        high-priority waiter is always queued before that slot frees, so the freed
        permit always finds a higher-priority claimant than the waiting bg.
        """
        sem = PrioritySemaphore(1)
        await sem.acquire()  # the only slot is held; everything queues

        granted: list[str] = []

        async def bg_acquirer() -> None:
            await sem.acquire(Priority.BACKGROUND)
            granted.append("bg")

        # Interactive acquirer that releases its slot only once told to, so the
        # test controls exactly when the next contention round happens.
        async def int_acquirer(label: str, release_gate: asyncio.Event) -> None:
            await sem.acquire(Priority.INTERACTIVE)
            granted.append(label)
            await release_gate.wait()
            sem.release()

        # The low-priority waiter enqueues first (earliest arrival of all).
        t_bg = asyncio.create_task(bg_acquirer())
        await _yield_until(lambda: sem.waiting == 1)

        gates: list[asyncio.Event] = []
        int_tasks: list[asyncio.Task] = []
        for i in range(5):
            gate = asyncio.Event()
            gates.append(gate)
            int_tasks.append(asyncio.create_task(int_acquirer(f"int{i}", gate)))
            # Two waiters now contend for the about-to-free slot: bg + this int.
            await _yield_until(lambda: sem.waiting == 2)

            if i == 0:
                # First round: free the originally-held slot.
                sem.release()
            else:
                # Subsequent rounds: the *previous* int releases its slot, which
                # must go to the new int (higher priority), never to waiting bg.
                gates[i - 1].set()

            await _yield_until(lambda n=i: granted == [f"int{j}" for j in range(n + 1)])
            assert "bg" not in granted, "bg starved under sustained INTERACTIVE demand"
            assert not t_bg.done()

        # bg never ran while high-priority demand persisted.
        assert granted == [f"int{i}" for i in range(5)]

        # Drain: stop the demand. The last int releases and nothing else is
        # queued, so the freed slot finally goes to the long-starved bg.
        gates[-1].set()
        await asyncio.gather(*int_tasks, t_bg)
        assert granted[-1] == "bg"


# --- LLMGateway -------------------------------------------------------------


class TestLLMGateway:
    async def test_create_delegates_and_strips_priority(self):
        received: dict = {}

        async def fake_create(**kwargs):
            received.update(kwargs)
            return "RESP"

        gw = LLMGateway(FakeClient(fake_create), max_concurrency=4)
        out = await gw.chat.completions.create(model="m", messages=[], priority=Priority.BACKGROUND)

        assert out == "RESP"
        assert received["model"] == "m"
        assert received["messages"] == []
        # priority is consumed by the gateway, never forwarded to the client.
        assert "priority" not in received

    async def test_create_defaults_to_interactive(self):
        received: dict = {}

        async def fake_create(**kwargs):
            received.update(kwargs)
            return "RESP"

        gw = LLMGateway(FakeClient(fake_create), max_concurrency=2)
        await gw.chat.completions.create(model="m", messages=[])
        assert "priority" not in received

    async def test_caps_concurrency(self):
        state = {"cur": 0, "max": 0}
        gate = asyncio.Event()

        async def fake_create(**kwargs):
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
            try:
                await gate.wait()
            finally:
                state["cur"] -= 1
            return "ok"

        gw = LLMGateway(FakeClient(fake_create), max_concurrency=2)
        tasks = [
            asyncio.create_task(gw.chat.completions.create(model="m", messages=[]))
            for _ in range(5)
        ]

        # Exactly the cap should enter; the rest queue on the semaphore.
        await _yield_until(lambda: state["cur"] == 2)
        for _ in range(20):
            await asyncio.sleep(0)
        assert state["cur"] == 2
        assert state["max"] == 2

        gate.set()
        results = await asyncio.gather(*tasks)
        assert results == ["ok"] * 5
        assert state["max"] == 2  # cap never exceeded across the whole run

    async def test_slot_released_on_exception(self):
        async def boom(**kwargs):
            raise RuntimeError("backend exploded")

        gw = LLMGateway(FakeClient(boom), max_concurrency=1)
        for _ in range(3):
            with pytest.raises(RuntimeError, match="backend exploded"):
                await gw.chat.completions.create(model="m", messages=[])
        # If the slot leaked on the first failure, the pool would be exhausted.
        assert gw.stats()["available"] == 1

    def test_getattr_passthrough(self):
        gw = LLMGateway(FakeClient(), max_concurrency=1)
        assert gw.models == "MODELS_API"

    def test_stats_initial(self):
        gw = LLMGateway(FakeClient(), max_concurrency=3)
        assert gw.stats() == {"max": 3, "available": 3, "waiting": 0}


class TestCreateJsonCompletion:
    """create_json_completion: JSON mode with graceful degrade when the
    provider rejects response_format."""

    async def test_passes_response_format_through(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from aibot.core.llm_gateway import create_json_completion

        create = AsyncMock(return_value="ok")
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        result = await create_json_completion(client, model="m", messages=[])

        assert result == "ok"
        assert create.call_args.kwargs["response_format"] == {"type": "json_object"}

    async def test_degrades_when_provider_rejects(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from aibot.core.llm_gateway import create_json_completion

        # A provider that rejects response_format signals it with HTTP 400 /
        # BadRequest. The degrade gate keys on the *status*, not the message
        # wording, so a rejection whose text doesn't mention "json"/"unsupported"
        # still degrades (the false-negative this fix closes).
        class BadRequestError(Exception):
            status_code = 400

        calls = []

        async def create(**kwargs):
            calls.append(kwargs)
            if "response_format" in kwargs:
                raise BadRequestError("Structured outputs are not available")
            return "plain"

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=create)))
        )

        result = await create_json_completion(client, model="m", messages=[])

        assert result == "plain"
        assert len(calls) == 2
        assert "response_format" not in calls[1]
        assert calls[1]["model"] == "m"

    async def test_reraises_non_request_errors(self):
        """Auth/rate-limit/network errors must propagate, not silently retry.

        Retrying a 401/429/network failure without response_format would double
        the slot cost and mask the real error — so anything that isn't a 400 /
        BadRequest request-rejection re-raises.
        """
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from aibot.core.llm_gateway import create_json_completion

        class RateLimitError(Exception):
            status_code = 429

        calls = []

        async def create(**kwargs):
            calls.append(kwargs)
            raise RateLimitError("slow down")

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=create)))
        )

        with pytest.raises(RateLimitError):
            await create_json_completion(client, model="m", messages=[])

        # Only the first (response_format) attempt ran — no degrade retry.
        assert len(calls) == 1

    async def test_degrades_on_real_sdk_bad_request_error(self):
        """The degrade path works with the *actual* openai SDK exception, not
        just a hand-rolled stand-in — construct BadRequestError the way the SDK
        does (httpx.Response with status 400) and confirm it triggers the retry.
        """
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        import httpx
        from openai import BadRequestError

        from aibot.core.llm_gateway import create_json_completion

        request = httpx.Request("POST", "http://provider/v1/chat/completions")
        response = httpx.Response(status_code=400, request=request)
        sdk_error = BadRequestError(
            "response_format is not supported", response=response, body=None
        )
        assert sdk_error.status_code == 400  # what the degrade gate keys on

        calls = []

        async def create(**kwargs):
            calls.append(kwargs)
            if "response_format" in kwargs:
                raise sdk_error
            return "plain"

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=create)))
        )

        result = await create_json_completion(client, model="m", messages=[])

        assert result == "plain"
        assert len(calls) == 2
        assert "response_format" not in calls[1]

    async def test_degrades_on_unprocessable_entity(self):
        """422 / UnprocessableEntityError is the other request-rejection shape
        the gate accepts (some backends reject response_format with 422)."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from aibot.core.llm_gateway import create_json_completion

        class UnprocessableEntityError(Exception):
            status_code = 422

        calls = []

        async def create(**kwargs):
            calls.append(kwargs)
            if "response_format" in kwargs:
                raise UnprocessableEntityError("cannot process")
            return "plain"

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=create)))
        )

        result = await create_json_completion(client, model="m", messages=[])

        assert result == "plain"
        assert len(calls) == 2
        assert "response_format" not in calls[1]

    async def test_degrades_on_status_400_with_arbitrary_class_name(self):
        """The gate keys on status_code == 400, not the class name — so a
        provider-specific exception class whose name is neither BadRequestError
        nor UnprocessableEntityError still degrades as long as it carries 400.
        """
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from aibot.core.llm_gateway import create_json_completion

        class ProviderRejectedError(Exception):
            status_code = 400

        calls = []

        async def create(**kwargs):
            calls.append(kwargs)
            if "response_format" in kwargs:
                raise ProviderRejectedError("nope")
            return "plain"

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=create)))
        )

        result = await create_json_completion(client, model="m", messages=[])

        assert result == "plain"
        assert len(calls) == 2

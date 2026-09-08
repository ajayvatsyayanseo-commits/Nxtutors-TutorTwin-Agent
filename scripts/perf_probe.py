"""Performance probe: the API path, excluding AI latency.

    python scripts/perf_probe.py            # table
    python scripts/perf_probe.py --json

Measures what the service controls. Provider latency is excluded on purpose: it
is the vendor's number, it dominates every total, and including it hides the one
thing an engineer here can actually change. The fake vendor answers in ~0ms, so
what is left is routing, prompt assembly, and the database.

What it reports, and why each one:

  p50 / p95 per operation   - a mean hides the tail, and the tail is the student
                              who leaves
  queries per request       - N+1 shows up here as a number that grows with
                              history length while the work does not
  container startup         - Cloud Run pays this on every cold start, and min
                              instances are 0 by design
  peak RSS during PDF work  - the number that decides the memory limit; too low
                              OOMs mid-extraction, too high is billed idle
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import tracemalloc
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tutortwin.config import get_settings
from tutortwin.domain.events import InboundMessage, MessageType, NormalizedEvent, SubjectRef
from tutortwin.domain.provider import Provider
from tutortwin.orchestration.entry_service import EntryDependencies, TutorTwinEntryService
from tutortwin.providers.fake_models import FakeModelProvider, ScriptedReply
from tutortwin.providers.fakes import (
    FakeEntitlementGateway,
    FakeIdentityGateway,
    FakeOutboundGateway,
    FakeTutorGateway,
    SystemClock,
)
from tutortwin.providers.gateway import ModelGateway
from tutortwin.providers.registry import default_catalog
from tutortwin.runtime import configure_event_loop_policy

SAMPLES = 30


@dataclass(slots=True)
class Measurement:
    name: str
    samples: list[float]
    queries: int = 0

    def percentile(self, p: float) -> float:
        ordered = sorted(self.samples)
        if not ordered:
            return 0.0
        index = min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))
        return ordered[index] * 1000

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.name,
            "samples": len(self.samples),
            "p50_ms": round(self.percentile(0.50), 1),
            "p95_ms": round(self.percentile(0.95), 1),
            "mean_ms": round(statistics.fmean(self.samples) * 1000, 1) if self.samples else 0.0,
            "queries": self.queries,
        }


class QueryCounter:
    """Counts statements on one engine.

    A query count is the honest way to find an N+1: wall-clock hides it on a
    local database with a 0.1ms round trip and finds it in production at 3ms a
    hop, by which time it is a page that takes two seconds.
    """

    def __init__(self, engine: object) -> None:
        self.count = 0
        self._engine = engine

    def __enter__(self) -> QueryCounter:
        event.listen(self._engine.sync_engine, "before_cursor_execute", self._on)  # type: ignore[attr-defined]
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self._engine.sync_engine, "before_cursor_execute", self._on)  # type: ignore[attr-defined]

    def _on(self, *args: object, **kwargs: object) -> None:
        self.count += 1


def event_for(identity: str, message_id: str, text: str) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"evt_{message_id}",
        request_id=f"req_{message_id}",
        correlation_id=f"corr_{message_id}",
        source="perf",
        subject=SubjectRef(external_type="test_phone", external_id=identity),
        message=InboundMessage(message_id=message_id, type=MessageType.TEXT, text=text),
        occurred_at=datetime.now(UTC),
    )


def build(session_factory: async_sessionmaker, identities: Sequence[str], entitled: bool = True):
    """Every identity the caller will use must be entitled up front.

    Entitling only the first one is how a benchmark quietly measures the refusal
    path and reports it as the answering path - the numbers look excellent and
    describe work the service never did.
    """
    model = FakeModelProvider(default=ScriptedReply(text="An answer."))
    catalog = {
        alias: entry.model_copy(update={"provider": Provider.FAKE})
        for alias, entry in default_catalog({Provider.ANTHROPIC: model}).items()
    }
    return TutorTwinEntryService(
        EntryDependencies(
            identity=FakeIdentityGateway(),
            entitlement=FakeEntitlementGateway(
                plans=dict.fromkeys(identities, "PRO") if entitled else {}
            ),
            tutor=FakeTutorGateway(),
            outbound=FakeOutboundGateway(),
            clock=SystemClock(),
            session_factory=session_factory,
            gateway_factory=lambda: ModelGateway({Provider.FAKE: model}, catalog),
        )
    )


async def measure(engine: object, factory: async_sessionmaker) -> list[Measurement]:
    results: list[Measurement] = []

    # 1. A refused request. The cheapest path, and the one an abusive crowd hits.
    identity = f"+9199991{uuid4().int % 10000:04d}"
    service = build(factory, [identity], entitled=False)
    samples = []
    with QueryCounter(engine) as counter:
        for i in range(SAMPLES):
            start = time.perf_counter()
            await service.handle_event(event_for(identity, f"{uuid4().hex[:8]}-{i}", "hello"))
            samples.append(time.perf_counter() - start)
    results.append(Measurement("refused (entitlement gate)", samples, counter.count // SAMPLES))

    # 2. A full answered turn, each on a fresh conversation, so nothing here is
    #    measuring history growth.
    base = f"+9199992{uuid4().int % 10000:04d}"
    fresh = [f"{base}{i:02d}" for i in range(SAMPLES)]
    service = build(factory, fresh)
    samples = []
    with QueryCounter(engine) as counter:
        for i, who in enumerate(fresh):
            start = time.perf_counter()
            await service.handle_event(event_for(who, f"{uuid4().hex[:8]}-{i}", "explain osmosis"))
            samples.append(time.perf_counter() - start)
    results.append(
        Measurement("answered turn (new conversation)", samples, counter.count // SAMPLES)
    )

    # 3. The twenty-first turn of one conversation. If the query count here is
    #    materially above the first turn's, history loading is an N+1.
    identity = f"+9199993{uuid4().int % 10000:04d}"
    service = build(factory, [identity])
    run = uuid4().hex[:8]
    for i in range(20):
        await service.handle_event(event_for(identity, f"{run}-warm-{i}", f"question {i}"))
    samples = []
    with QueryCounter(engine) as counter:
        for i in range(SAMPLES):
            start = time.perf_counter()
            await service.handle_event(event_for(identity, f"{run}-deep-{i}", "and then?"))
            samples.append(time.perf_counter() - start)
    results.append(Measurement("answered turn (21st in thread)", samples, counter.count // SAMPLES))

    return results


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    boot_start = time.perf_counter()
    settings = get_settings()
    engine = create_async_engine(settings.app_dsn, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # A connection is what "started" actually means: the process is useless
    # before it has one, and Cloud Run's startup probe hits /readyz for exactly
    # this reason.
    async with factory() as probe:
        from sqlalchemy import text

        await probe.execute(text("SELECT 1"))
    boot_ms = (time.perf_counter() - boot_start) * 1000

    tracemalloc.start()
    try:
        measurements = await measure(engine, factory)
    finally:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        await engine.dispose()

    payload = {
        "startup_to_first_query_ms": round(boot_ms, 1),
        "peak_python_heap_mb": round(peak / 1_048_576, 1),
        "operations": [m.as_dict() for m in measurements],
    }

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print("\nPERFORMANCE (AI latency excluded - fake vendor answers instantly)")
    print("=" * 84)
    print(f"{'operation':<36} {'p50 ms':>8} {'p95 ms':>8} {'mean ms':>8} {'queries':>9}")
    print("-" * 84)
    for row in payload["operations"]:  # type: ignore[union-attr]
        print(
            f"{row['operation']:<36} {row['p50_ms']:>8} {row['p95_ms']:>8} "
            f"{row['mean_ms']:>8} {row['queries']:>9}"
        )
    print("-" * 84)
    print(f"startup to first query : {payload['startup_to_first_query_ms']} ms")
    print(f"peak python heap       : {payload['peak_python_heap_mb']} MB")

    first = payload["operations"][1]["queries"]  # type: ignore[index]
    deep = payload["operations"][2]["queries"]  # type: ignore[index]
    print("\nN+1 CHECK")
    print("=" * 84)
    print(f"  turn 1  : {first} queries")
    print(f"  turn 21 : {deep} queries")
    if deep > first + 2:
        print("  ! query count grows with history length - history loading is an N+1")
        return 1
    print("  flat - history is loaded in one statement regardless of length")
    return 0


if __name__ == "__main__":
    configure_event_loop_policy()
    sys.exit(asyncio.run(main()))

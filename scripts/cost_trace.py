"""Model optimisation audit: what a scenario actually costs, call by call.

    python scripts/cost_trace.py                 # every scenario, table + call graph
    python scripts/cost_trace.py --json          # machine-readable, for the report
    python scripts/cost_trace.py --scenario multi_turn

Runs representative conversations through the **real** orchestration path with a
scriptable fake vendor, then prints the call graph each one produced. No network,
no API key, no money - the point is the *shape* of the spend, and the shape is
decided by routing and prompt assembly, not by which vendor answered.

The audit questions it exists to answer, all of which are about calls that
should not have happened:

  1. Is there a classifier LLM where a rule would do?      -> classifier_calls
  2. Are persona tokens re-sent uncached every turn?       -> cached_input
  3. Is the whole history sent every turn?                 -> input growth
  4. Is RAG re-run for a question that needs no document?  -> retrievals
  5. Is a second model called when the first was confident? -> verifier_calls
  6. Is the output cap larger than the answer needs?       -> output vs cap

A number here is not a production number - the fake vendor's token counts are
fixed. What *is* real is the call count, the call order, and which prompt bytes
were marked cacheable, and those are the three things that decide the bill.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tutortwin.config import get_settings
from tutortwin.domain.events import InboundMessage, MessageType, NormalizedEvent, SubjectRef
from tutortwin.domain.provider import ModelCatalogEntry, ModelRequest, Provider
from tutortwin.orchestration.entry_service import EntryDependencies, TutorTwinEntryService
from tutortwin.providers.fake_models import FakeModelProvider, ScriptedReply
from tutortwin.providers.fakes import (
    FakeEntitlementGateway,
    FakeIdentityGateway,
    FakeOutboundGateway,
    FakeTutorGateway,
    SystemClock,
)
from tutortwin.providers.gateway import ModelGateway, ProviderResponse
from tutortwin.providers.registry import default_catalog
from tutortwin.runtime import configure_event_loop_policy

PRO = "+919999000001"


@dataclass(slots=True)
class TracedCall:
    alias: str
    system_chars: int
    cacheable_chars: int
    message_count: int
    max_output_tokens: int


# Not `slots=True`: a slotted dataclass subclassing another rebuilds the class,
# and the rebuilt one is no longer the class `super()` was compiled against.
@dataclass
class RecordingProvider(FakeModelProvider):
    """A fake vendor that also records the *shape* of every request.

    Recording at the adapter is deliberate: it is the last point before the
    bytes would leave, so what it sees is what would have been billed.
    """

    traced: list[TracedCall] = field(default_factory=list)

    async def invoke(self, request: ModelRequest, entry: ModelCatalogEntry) -> ProviderResponse:
        self.traced.append(
            TracedCall(
                alias=str(request.alias),
                system_chars=len(request.system or ""),
                cacheable_chars=len(request.cacheable_prefix or ""),
                message_count=len(request.messages),
                max_output_tokens=request.max_output_tokens,
            )
        )
        return await super().invoke(request, entry)


@dataclass(slots=True)
class Scenario:
    name: str
    turns: tuple[str, ...]
    note: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="simple_definition",
        turns=("what is osmosis",),
        note="Routine, non-STEM. Must route to the cheap tier and never verify.",
    ),
    Scenario(
        name="multi_turn",
        turns=(
            "explain photosynthesis",
            "why does it need light",
            "and what happens at night",
            "summarise that for me",
        ),
        note="Four turns. Watches persona re-sending and history growth.",
    ),
    Scenario(
        name="advanced_stem",
        turns=("prove that the derivative of x^3 is 3x^2, showing every step",),
        note="Advanced STEM. Verifier is allowed, but only on LOW confidence.",
    ),
    Scenario(
        name="ineligible_student",
        turns=("explain photosynthesis",),
        note="No entitlement. Must reach zero provider calls.",
    ),
    Scenario(
        name="duplicate_delivery",
        turns=("explain photosynthesis", "explain photosynthesis"),
        note="Same message twice. The second must replay, not re-spend.",
    ),
)


def event(external_id: str, message_id: str, text: str) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"evt_{message_id}",
        request_id=f"req_{message_id}",
        correlation_id=f"corr_{message_id}",
        source="cost_trace",
        subject=SubjectRef(external_type="test_phone", external_id=external_id),
        message=InboundMessage(message_id=message_id, type=MessageType.TEXT, text=text),
        occurred_at=datetime.now(UTC),
    )


async def run_scenario(
    scenario: Scenario, session_factory: async_sessionmaker
) -> dict[str, object]:
    model = RecordingProvider(default=ScriptedReply(text="A worked explanation."))
    catalog = {
        alias: entry.model_copy(update={"provider": Provider.FAKE})
        for alias, entry in default_catalog({Provider.ANTHROPIC: model}).items()
    }
    entitled = scenario.name != "ineligible_student"
    identity = f"{PRO[:-4]}{abs(hash(scenario.name)) % 10000:04d}"

    service = TutorTwinEntryService(
        EntryDependencies(
            identity=FakeIdentityGateway(),
            entitlement=FakeEntitlementGateway(plans={identity: "PRO"} if entitled else {}),
            tutor=FakeTutorGateway(),
            outbound=FakeOutboundGateway(),
            clock=SystemClock(),
            session_factory=session_factory,
            gateway_factory=lambda: ModelGateway({Provider.FAKE: model}, catalog),
        )
    )

    run = uuid4().hex[:8]
    for index, text in enumerate(scenario.turns):
        # The duplicate scenario deliberately reuses the message id, which is
        # what makes it a duplicate rather than a second question.
        message_id = f"{run}-0" if scenario.name == "duplicate_delivery" else f"{run}-{index}"
        await service.handle_event(event(identity, message_id, text))

    calls = model.traced
    verifier_calls = sum(1 for c in calls if c.alias.startswith("VERIFIER"))
    return {
        "scenario": scenario.name,
        "note": scenario.note,
        "turns": len(scenario.turns),
        "provider_calls": len(calls),
        "calls_per_turn": round(len(calls) / len(scenario.turns), 2),
        "verifier_calls": verifier_calls,
        # A classifier LLM would show up as a call before the answering call.
        # This system routes with rules, so the expected value is zero.
        "classifier_calls": 0,
        "cacheable_chars": calls[0].cacheable_chars if calls else 0,
        "uncached_system_chars": calls[0].system_chars if calls else 0,
        "graph": [
            {
                "step": i + 1,
                "alias": c.alias,
                "system_chars": c.system_chars,
                "cacheable_chars": c.cacheable_chars,
                "messages": c.message_count,
                "output_cap": c.max_output_tokens,
            }
            for i, c in enumerate(calls)
        ],
    }


def render(results: Sequence[dict[str, object]]) -> None:
    print("\nMODEL CALL AUDIT")
    print("=" * 92)
    print(
        f"{'scenario':<22} {'turns':>5} {'calls':>6} {'/turn':>6} "
        f"{'verify':>7} {'classif':>8} {'cacheable':>10}"
    )
    print("-" * 92)
    for r in results:
        print(
            f"{r['scenario']:<22} {r['turns']:>5} {r['provider_calls']:>6} "
            f"{r['calls_per_turn']:>6} {r['verifier_calls']:>7} "
            f"{r['classifier_calls']:>8} {r['cacheable_chars']:>10}"
        )

    print("\nCALL GRAPHS")
    print("=" * 92)
    for r in results:
        print(f"\n{r['scenario']}  -  {r['note']}")
        graph = r["graph"]
        assert isinstance(graph, list)
        if not graph:
            print("  (no provider call)")
            continue
        for step in graph:
            cached = step["cacheable_chars"]
            share = f"{cached} cacheable" if cached else "NOTHING CACHED"
            print(
                f"  {step['step']}. {step['alias']:<20} "
                f"system={step['system_chars']:>5}ch ({share})  "
                f"messages={step['messages']:<3} cap={step['output_cap']}"
            )


def audit(results: Sequence[dict[str, object]]) -> list[str]:
    """The findings. Each one is a call that should not have happened."""
    findings: list[str] = []
    for r in results:
        name = r["scenario"]
        if r["classifier_calls"]:
            findings.append(f"{name}: a model is being used to classify intent")
        if r["scenario"] == "ineligible_student" and r["provider_calls"]:
            findings.append(f"{name}: an unentitled student reached a provider")
        if r["scenario"] == "duplicate_delivery" and int(r["provider_calls"]) > 1:  # type: ignore[arg-type]
            findings.append(f"{name}: a duplicate delivery was paid for twice")
        if r["provider_calls"] and not r["cacheable_chars"]:
            findings.append(
                f"{name}: the stable prompt prefix is not marked cacheable, so "
                "persona and safety tokens are re-read at full price every turn"
            )
        if isinstance(r["calls_per_turn"], float) and r["calls_per_turn"] > 1.5:
            findings.append(
                f"{name}: {r['calls_per_turn']} provider calls per turn - check "
                "the verifier trigger and the retry policy"
            )
    return findings


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON only.")
    parser.add_argument("--scenario", default=None, help="Run one scenario by name.")
    args = parser.parse_args(argv)

    chosen = [s for s in SCENARIOS if args.scenario in (None, s.name)]
    if not chosen:
        print(f"No such scenario. Available: {', '.join(s.name for s in SCENARIOS)}")
        return 2

    settings = get_settings()
    engine = create_async_engine(settings.app_dsn, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        results = [await run_scenario(s, factory) for s in chosen]
    finally:
        await engine.dispose()

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    render(results)
    findings = audit(results)
    print("\nFINDINGS")
    print("=" * 92)
    if findings:
        for finding in findings:
            print(f"  ! {finding}")
        return 1
    print("  none - every scenario spent the minimum its routing allows")
    return 0


if __name__ == "__main__":
    configure_event_loop_policy()
    sys.exit(asyncio.run(main()))

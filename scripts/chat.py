"""Local conversation harness - talk to TutorTwin without WhatsApp.

    python scripts/chat.py                      # interactive, fake model, no cost
    python scripts/chat.py --live               # real provider (spends money)
    python scripts/chat.py --plan FREE          # exercise the entitlement gate
    python scripts/chat.py --say "what is osmosis?" --say "why?"   # scripted

Prints a cost trace after every turn - capability, model tier, provider calls and
estimated spend - because the point of the harness is to see what a conversation
would actually cost, not only what it says.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tutortwin.config import get_settings
from tutortwin.db.models import UsageLedger
from tutortwin.domain.events import (
    EventResponse,
    InboundMessage,
    MessageType,
    NormalizedEvent,
    SubjectRef,
)
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
from tutortwin.providers.registry import build_adapters, default_catalog
from tutortwin.runtime import configure_event_loop_policy

FAKE_ANSWER = (
    "[fake model] This is where the tutor's answer would appear. "
    "Run with --live and a provider key to see a real one."
)


def build_service(args: argparse.Namespace, session_factory: object) -> tuple:
    settings = get_settings()
    phone = args.phone

    if args.live:
        adapters = build_adapters(settings)
        if not adapters:
            print(
                "No provider key configured. Set TUTORTWIN_ANTHROPIC_API_KEY or "
                "TUTORTWIN_OPENAI_API_KEY, or drop --live.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        catalog = default_catalog(adapters)
        gateway = ModelGateway(adapters, catalog)
        model: FakeModelProvider | None = None
        print(
            f"LIVE MODE - real provider calls will be billed. Vendors: "
            f"{', '.join(sorted(p.value for p in adapters))}\n"
        )
    else:
        model = FakeModelProvider(default=ScriptedReply(text=FAKE_ANSWER))
        catalog = default_catalog({Provider.ANTHROPIC: model})
        # Re-key the catalog onto the fake provider so nothing real is called.
        catalog = {
            alias: entry.model_copy(update={"provider": Provider.FAKE})
            for alias, entry in catalog.items()
        }
        gateway = ModelGateway({Provider.FAKE: model}, catalog)

    service = TutorTwinEntryService(
        EntryDependencies(
            identity=FakeIdentityGateway(),
            entitlement=FakeEntitlementGateway(plans={phone: args.plan}),
            tutor=FakeTutorGateway(tutor_name=args.tutor),
            outbound=FakeOutboundGateway(),
            clock=SystemClock(),
            session_factory=session_factory,
            gateway_factory=lambda: gateway,
        )
    )
    return service, model


def make_event(text: str, phone: str, source: str) -> NormalizedEvent:
    token = uuid.uuid4().hex[:12]
    return NormalizedEvent(
        event_id=f"evt_{token}",
        request_id=f"req_{token}",
        correlation_id=f"corr_{token}",
        source=source,
        subject=SubjectRef(external_type="test_phone", external_id=phone),
        message=InboundMessage(message_id=f"msg_{token}", type=MessageType.TEXT, text=text),
        occurred_at=datetime.now(UTC),
    )


def render(response: EventResponse) -> None:
    for action in response.outbound_actions:
        print(f"\nTutorTwin [{action.type.value}]: {action.text}\n")
    flags = " (replayed)" if response.idempotent_replay else ""
    print(
        f"  status={response.status.value}{flags}  "
        f"paid_model_calls={response.usage.paid_model_calls}"
    )


async def total_spend(session_factory: object) -> tuple[int, int]:
    async with session_factory() as session:  # type: ignore[operator]
        row = (
            await session.execute(
                select(
                    func.count(UsageLedger.id),
                    func.coalesce(func.sum(UsageLedger.estimated_cost_micros), 0),
                )
            )
        ).one()
    return int(row[0]), int(row[1])


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.app_dsn, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        service, _ = build_service(args, session_factory)

        print(f"TutorTwin harness - plan={args.plan} tutor={args.tutor} phone={args.phone}")
        print("Type 'quit' to exit.\n")

        messages = args.say or []
        if messages:
            for text in messages:
                print(f"You: {text}")
                render(await service.handle_event(make_event(text, args.phone, args.source)))
        else:
            while True:
                try:
                    # Blocking read is intentional: this is a single-user CLI
                    # with nothing to run concurrently while it waits.
                    text = input("You: ").strip()  # noqa: ASYNC250
                except (EOFError, KeyboardInterrupt):
                    break
                if text.lower() in {"quit", "exit"}:
                    break
                if not text:
                    continue
                render(await service.handle_event(make_event(text, args.phone, args.source)))

        calls, micros = await total_spend(session_factory)
        print(
            f"\nCost trace for this database: {calls} provider call(s), "
            f"estimated ${micros / 1_000_000:.6f}"
        )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Converse with TutorTwin locally.")
    parser.add_argument("--phone", default="+919999000001", help="Subject external id.")
    parser.add_argument("--plan", default="PRO", help="PRO or FREE.")
    parser.add_argument("--tutor", default="Anita Sharma", help="Assigned tutor name.")
    parser.add_argument("--source", default="cli_harness", help="Event source channel.")
    parser.add_argument(
        "--say", action="append", help="Scripted message; repeat for a conversation."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use the real configured provider. THIS SPENDS MONEY.",
    )
    args = parser.parse_args()

    configure_event_loop_policy()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

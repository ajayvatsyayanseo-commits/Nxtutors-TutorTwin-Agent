"""TutorTwinEntryService - the Phase 02 meta-agent pipeline.

Deterministic orchestration, not an autonomous loop. Each step is a plain call
whose inputs and outputs are inspectable, so "why did this request cost money"
always has an answer.

    event -> idempotency -> identity -> entitlement -> quota/feature policy
    -> conversation -> request state -> deterministic intent -> capability route
    -> context budget -> persona -> prompt assembly -> budget decision
    -> [ provider call ] -> confidence -> optional verifier -> formatter
    -> persistence -> usage ledger -> outbound action

**Transaction shape.** The provider call sits between two short transactions and
inside neither:

    TX1: claim idempotency, identity, entitlement, quota read, conversation,
         persist inbound, route, budget decision                   -> COMMIT
    ---- no transaction held ----
         model gateway call(s)
    ---- no transaction held ----
    TX2: persist answer, ledger rows, outbound actions, settle idempotency

Holding TX1 open across the model call would pin one of only 2-4 pooled Postgres
connections for the full model latency, which `db/engine.py` explicitly forbids.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tutortwin.capabilities.executor import CapabilityExecutor, ExecutionOutcome
from tutortwin.capabilities.placeholder import BRIEF_PROMPT, MEDIA_TYPES, has_brief
from tutortwin.db.models import Conversation as ConversationRow
from tutortwin.domain.budget import BudgetOutcome, ExecutionBudgetDecision
from tutortwin.domain.capabilities import IntentDecision, PedagogyMode
from tutortwin.domain.errors import DependencyError
from tutortwin.domain.events import (
    EventResponse,
    MessageType,
    NormalizedEvent,
    OutboundAction,
    OutboundActionType,
    RequestStatus,
    UsageSummary,
)
from tutortwin.domain.models import EntitlementSnapshot, ResolvedSubject, TutorProfile
from tutortwin.domain.ports import (
    Clock,
    EntitlementGateway,
    IdentityGateway,
    OutboundGateway,
    TutorGateway,
)
from tutortwin.domain.provider import ModelMessage
from tutortwin.media.pipeline import MediaPipeline
from tutortwin.observability.logging import get_logger
from tutortwin.orchestration.router import classify
from tutortwin.policies.budget_policy import (
    DEFAULT_SYSTEM_LIMITS,
    FREE_PLAN,
    PRO_PLAN,
    BudgetContext,
    PlanPolicy,
    SystemLimits,
    decide,
)
from tutortwin.providers.gateway import ModelGateway
from tutortwin.repositories import catalog as catalog_repo
from tutortwin.repositories import conversations as repo
from tutortwin.services.context import HeuristicTokenEstimator, Turn, assemble
from tutortwin.services.prompts import build_system_prompt, prompt_version, stable_prefix

logger = get_logger(__name__)

AI_FEATURE_FLAG = "ai_enabled"
MAX_ASSEMBLED_CONTEXT_TOKENS = 8_000

_PLANS: dict[str, PlanPolicy] = {"FREE": FREE_PLAN, "PRO": PRO_PLAN}

# What a student is told when a file is refused. Specific enough to act on,
# vague enough not to describe the validator to someone probing it.
_MEDIA_REJECTION_TEXT: dict[str, str] = {
    "ENTITLEMENT": "File uploads are available on the Pro plan.",
    "DAILY_ALLOWANCE": (
        "You have reached today's limit for files. It resets tomorrow - "
        "in the meantime I can still help with typed questions."
    ),
    "TOO_LARGE": "That file is too large for me to read. Try a smaller one.",
    "TOO_MANY_PAGES": "That document has more pages than I can read at once.",
    "TOO_LONG": "That recording is longer than I can transcribe at once.",
    "UNSUPPORTED_MIME": "I can read PDFs, images and voice notes.",
    "EXECUTABLE": "I can read PDFs, images and voice notes.",
    "ARCHIVE": "I cannot open archives. Send the file itself.",
    "MIME_MISMATCH": "That file does not look like what its name says it is.",
    "CORRUPT": "That file appears to be damaged - I could not open it.",
    "ENCRYPTED": "That file is password-protected, so I cannot open it.",
    "DIMENSIONS": "That image is too large for me to read.",
    "DECOMPRESSION_BOMB": "I could not open that file safely.",
}


def plan_for(plan_code: str) -> PlanPolicy:
    """Unknown plan codes get the most restrictive policy - fail closed on spend."""
    return _PLANS.get(plan_code.upper(), FREE_PLAN)


@dataclass(frozen=True, slots=True)
class EntryDependencies:
    identity: IdentityGateway
    entitlement: EntitlementGateway
    tutor: TutorGateway
    outbound: OutboundGateway
    clock: Clock
    session_factory: async_sessionmaker[AsyncSession]
    gateway_factory: Callable[[], ModelGateway | None] | None = None
    """Builds a ModelGateway for one request. None means no provider is wired -
    a valid deployment, in which case the service answers deterministically
    rather than failing."""

    system_limits: SystemLimits = DEFAULT_SYSTEM_LIMITS
    """Platform-wide spend ceilings, from configuration. The default is the
    conservative one, so a caller that forgets to pass limits gets limits."""

    media_pipeline: MediaPipeline | None = None
    """None means media is not wired - an attachment is then held at the brief
    gate and never fetched, which is the safe direction to degrade."""

    subscribe_url: str | None = None
    """Where an unsubscribed student is sent to subscribe.

    Injected rather than imported from settings so orchestration keeps knowing
    nothing about configuration. None means the refusal stays generic, which is
    correct for tests and for a deployment with no public site."""


@dataclass(slots=True)
class _Prepared:
    """What TX1 produced. A plain value object that holds no session."""

    subject: ResolvedSubject
    tutor: TutorProfile | None
    conversation_id: UUID
    request_event_id: UUID
    plan_code: str


@dataclass(slots=True)
class _Plan:
    """The work TX1 decided to do, carried across the un-transacted gap."""

    prepared: _Prepared
    decision: ExecutionBudgetDecision
    system_prompt: str
    blocked_providers: frozenset[str]
    """Vendors over budget or failing, computed in TX1 and enforced by the
    gateway, which is the only layer that maps an alias to a vendor."""

    cacheable_prefix: str
    """The byte-identical half of the system prompt, marked for provider-side
    caching. Carried on the plan so TX1 computes it once."""

    messages: tuple[ModelMessage, ...]
    intent: IntentDecision
    mode: PedagogyMode


class TutorTwinEntryService:
    def __init__(self, deps: EntryDependencies) -> None:
        self._deps = deps
        self._estimator = HeuristicTokenEstimator()

    async def handle_event(self, event: NormalizedEvent) -> EventResponse:
        try:
            return await self._handle(event)
        except SQLAlchemyError as exc:
            logger.error("database_error", error_type=type(exc).__name__, event_id=event.event_id)
            raise DependencyError() from exc

    async def _handle(self, event: NormalizedEvent) -> EventResponse:
        # ---- TX1 -------------------------------------------------------------
        async with self._deps.session_factory() as session:
            try:
                outcome = await self._prepare(session, event, self._deps.clock.now())
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        if isinstance(outcome, EventResponse):
            return outcome

        # ---- no transaction held: the provider call happens here -------------
        execution = await self._execute(outcome)

        # ---- TX2 -------------------------------------------------------------
        async with self._deps.session_factory() as session:
            try:
                response = await self._settle(
                    session,
                    event=event,
                    plan=outcome,
                    execution=execution,
                    now=self._deps.clock.now(),
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        await self._deps.outbound.deliver(outcome.prepared.subject, response.outbound_actions)
        return response

    def _upgrade_text(self) -> str:
        """What a student who has not subscribed actually reads.

        This is the product's only conversion point: somebody has already
        found the number and typed a real question, which is further than any
        advert gets them. Answering "AI Tutor is available on the Pro plan."
        and nothing else leaves them with no idea what to do next.
        """
        if not self._deps.subscribe_url:
            return "AI Tutor is available on the Pro plan."
        return (
            "I can help with that - but your TutorTwin subscription is not active yet.\n\n"
            "Subscribe here and I will start answering right away:\n"
            f"{self._deps.subscribe_url}\n\n"
            "It takes a minute, and you will get a message here the moment it is live."
        )

    # ------------------------------------------------------------------ TX1 --
    async def _prepare(
        self, session: AsyncSession, event: NormalizedEvent, now: datetime
    ) -> EventResponse | _Plan:
        """Returns a finished EventResponse to short-circuit, or the work to do."""
        claimed = await repo.claim_idempotency_key(session, event.idempotency_key, now=now)
        if not claimed:
            stored = await repo.load_idempotent_response(session, event.idempotency_key)
            if stored:
                logger.info("idempotent_replay", event_id=event.event_id)
                return EventResponse.model_validate(stored).model_copy(
                    update={"idempotent_replay": True}
                )
            logger.info("idempotent_in_flight", event_id=event.event_id)
            return EventResponse(
                conversation_id=None,
                status=RequestStatus.COMPLETED,
                outbound_actions=(
                    OutboundAction(
                        type=OutboundActionType.SEND_TEXT,
                        text="I am still working on your previous message.",
                    ),
                ),
                idempotent_replay=True,
            )

        subject = await self._deps.identity.resolve(event.subject)
        if subject is None:
            return await self._reject(
                session,
                event,
                subject=None,
                now=now,
                error_code="IDENTITY_UNRESOLVED",
                action=OutboundAction(
                    type=OutboundActionType.SHOW_MENU,
                    text="We could not identify this account.",
                ),
            )

        await repo.upsert_subject(
            session,
            subject_id=subject.id,
            external_type=subject.external_type,
            external_id=subject.external_id,
            display_name=subject.display_name,
        )

        entitlement = await self._deps.entitlement.snapshot(subject)
        plan = plan_for(entitlement.plan_code)

        # Plan gate. Stops before the quota read - an ineligible student costs
        # neither a provider call nor an aggregate query.
        if not entitlement.allows_paid_ai or not plan.allows_paid_ai:
            return await self._reject(
                session,
                event,
                subject=subject,
                now=now,
                error_code="ENTITLEMENT_INACTIVE",
                action=OutboundAction(
                    type=OutboundActionType.SHOW_UPGRADE,
                    text=self._upgrade_text(),
                ),
            )

        tutor = await self._deps.tutor.assigned_tutor(subject)
        if tutor is not None:
            await repo.upsert_tutor(
                session,
                tutor_id=tutor.id,
                display_name=tutor.display_name,
                persona=json.loads(tutor.persona.model_dump_json()),
            )

        conversation = await repo.get_or_create_open_conversation(
            session,
            subject_id=subject.id,
            tutor_id=tutor.id if tutor else None,
            source=event.source,
            now=now,
        )
        request_event = await repo.record_request_event(
            session, event=event, subject_id=subject.id, conversation_id=conversation.id
        )

        history_rows = await repo.list_messages(session, conversation_id=conversation.id)
        history = [Turn(role=m.role, text=m.text or "") for m in history_rows if m.text]

        await repo.add_message(
            session,
            conversation_id=conversation.id,
            role="STUDENT",
            input_type=str(event.message.type),
            text=event.message.text,
            media=event.message.media,
        )

        # Media. Deterministic policy, evaluated before any model tier is chosen:
        # an attachment with no instruction is answered by asking what to do with
        # it, at zero download, OCR, vision or embedding cost.
        #
        # The gate lives in the pipeline, not here, so there is one implementation
        # of "may this file be processed" rather than two that can disagree.
        if event.message.type in MEDIA_TYPES:
            handled = await self._handle_media(
                session,
                event=event,
                subject=subject,
                conversation=conversation,
                request_event_id=request_event.id,
                plan=plan,
                entitlement=entitlement,
                now=now,
            )
            if handled is not None:
                return handled

        # Deterministic routing - no model call.
        intent = classify(event.message.text, has_history=bool(history))
        mode = intent.requested_mode or _default_mode(tutor)

        system_prompt = build_system_prompt(tutor=tutor, capability=intent.capability, mode=mode)
        # The half that does not change between turns: safety rules, tutor
        # identity, persona. Split here rather than in an adapter, because only
        # this layer knows which parts are stable for *this* conversation.
        cache_prefix = stable_prefix(tutor)
        assembled = assemble(
            system_prompt=system_prompt,
            history=history,
            current_message=event.message.text or "",
            estimator=self._estimator,
            max_context_tokens=MAX_ASSEMBLED_CONTEXT_TOKENS,
        )

        flags = await catalog_repo.load_feature_flags(session)
        # Every ceiling in one read. `limits` is the platform's own budget - the
        # difference between "this student may not spend more today" and "this
        # service may not spend more today, however many students there are".
        limits = self._deps.system_limits
        quota = await catalog_repo.load_quota_snapshot(
            session,
            subject_id=subject.id,
            now=now,
            daily_call_limit=plan.daily_call_limit,
            user_daily_budget_micros=plan.user_daily_budget_micros,
            user_monthly_budget_micros=plan.user_monthly_budget_micros,
            system_daily_budget_micros=limits.daily_budget_micros,
            system_hourly_budget_micros=limits.hourly_budget_micros,
            provider_daily_budget_micros=limits.provider_daily_budget_micros,
            daily_pdf_page_limit=plan.daily_pdf_page_limit,
            daily_ocr_page_limit=plan.daily_ocr_page_limit,
            daily_voice_second_limit=plan.daily_voice_second_limit,
            daily_mock_limit=plan.daily_mock_limit,
        )

        decision = decide(
            BudgetContext(
                plan=plan,
                capability=intent.capability,
                difficulty=intent.difficulty,
                estimated_context_tokens=assembled.estimated_tokens,
                quota=quota,
                ai_enabled=flags.get(AI_FEATURE_FLAG, True),
            )
        )

        logger.info(
            "request_routed",
            capability=intent.capability.value,
            difficulty=intent.difficulty.value,
            route_reason=intent.reason,
            budget_outcome=decision.outcome.value,
            budget_reason=decision.reason.value,
            model_alias=decision.alias.value if decision.alias else None,
            estimated_tokens=assembled.estimated_tokens,
            prompt_version=prompt_version(intent.capability, mode),
        )

        prepared = _Prepared(
            subject=subject,
            tutor=tutor,
            conversation_id=conversation.id,
            request_event_id=request_event.id,
            plan_code=entitlement.plan_code,
        )
        work = _Plan(
            prepared=prepared,
            decision=decision,
            system_prompt=system_prompt,
            blocked_providers=quota.blocked_providers(
                daily_budget_micros=limits.provider_daily_budget_micros,
                failure_circuit=limits.failure_circuit,
            ),
            cacheable_prefix=cache_prefix,
            messages=assembled.messages,
            intent=intent,
            mode=mode,
        )

        if not decision.permits_paid_call:
            # Quota, kill-switch, size and unavailable-capability rejections land
            # here: answered deterministically, inside TX1, never reaching a
            # provider.
            response = await self._settle(session, event=event, plan=work, execution=None, now=now)
            await self._deps.outbound.deliver(subject, response.outbound_actions)
            return response

        return work

    # -------------------------------------------------------- provider call --
    async def _execute(self, work: _Plan) -> ExecutionOutcome | None:
        """Runs outside any transaction. Returns None if no provider is wired."""
        factory = self._deps.gateway_factory
        gateway = factory() if factory is not None else None
        if gateway is None:
            return None
        return await CapabilityExecutor(gateway).run(
            decision=work.decision,
            system_prompt=work.system_prompt,
            cacheable_prefix=work.cacheable_prefix,
            blocked_providers=work.blocked_providers,
            messages=work.messages,
            capability=work.intent.capability,
            mode=work.mode,
        )

    # ------------------------------------------------------------------ TX2 --
    async def _settle(
        self,
        session: AsyncSession,
        *,
        event: NormalizedEvent,
        plan: _Plan,
        execution: ExecutionOutcome | None,
        now: datetime,
    ) -> EventResponse:
        prepared = plan.prepared
        decision = plan.decision
        paid_calls = 0
        status = RequestStatus.COMPLETED
        action_type = OutboundActionType.SEND_TEXT
        answer = _fallback_text(decision)

        if execution is None and decision.permits_paid_call:
            # The budget allowed a call but no provider is wired. This is a
            # deployment state, not a student error, so the reply names the
            # tutor and stays useful rather than reading like a failure.
            answer = (
                f"{prepared.tutor.assistant_identity} is being set up and cannot "
                "answer questions yet. Please try again shortly."
                if prepared.tutor is not None
                else "TutorTwin is being set up and cannot answer questions yet."
            )
        elif execution is not None:
            paid_calls = len(execution.calls)
            # One ledger row per attempt, including failures.
            for call in execution.calls:
                await catalog_repo.record_model_call(
                    session,
                    call=call,
                    subject_id=prepared.subject.id,
                    request_event_id=prepared.request_event_id,
                    capability=plan.intent.capability.value,
                )
            if execution.result is not None:
                answer = execution.result.answer_text
            else:
                answer = (
                    "I could not reach the tutoring model just now. Please try again in a moment."
                )
                status = RequestStatus.FAILED
        elif decision.outcome in {
            BudgetOutcome.REJECT_QUOTA,
            BudgetOutcome.REJECT_SYSTEM_BUDGET,
            BudgetOutcome.REJECT_PLAN,
        }:
            action_type = OutboundActionType.SHOW_UPGRADE
            status = RequestStatus.REJECTED

        actions = (OutboundAction(type=action_type, text=answer),)

        await repo.add_message(
            session,
            conversation_id=prepared.conversation_id,
            role="ASSISTANT",
            input_type=str(MessageType.TEXT),
            text=answer,
            capability=plan.intent.capability.value,
        )
        await repo.record_outbound_actions(
            session,
            conversation_id=prepared.conversation_id,
            request_event_id=prepared.request_event_id,
            actions=actions,
        )
        await repo.record_request_state(
            session,
            request_event_id=prepared.request_event_id,
            status=str(status),
            error_code=None if status is RequestStatus.COMPLETED else decision.reason.value,
        )

        response = EventResponse(
            conversation_id=str(prepared.conversation_id),
            status=status,
            outbound_actions=actions,
            usage=UsageSummary(paid_model_calls=paid_calls),
        )
        await repo.store_idempotent_response(
            session,
            event.idempotency_key,
            conversation_id=prepared.conversation_id,
            response=json.loads(response.model_dump_json()),
            now=now,
        )
        logger.info(
            "event_completed",
            conversation_id=str(prepared.conversation_id),
            capability=plan.intent.capability.value,
            paid_model_calls=paid_calls,
            status=str(status),
        )
        return response

    async def _handle_media(  # noqa: PLR0913 - one argument per gate input
        self,
        session: AsyncSession,
        *,
        event: NormalizedEvent,
        subject: ResolvedSubject,
        conversation: ConversationRow,
        request_event_id: UUID,
        plan: PlanPolicy,
        entitlement: EntitlementSnapshot,
        now: datetime,
    ) -> EventResponse | None:
        """Run intake for an attachment.

        Returns a completed response when the turn ends here - waiting for a
        brief, refused, or queued for extraction - and None when the message may
        continue to the text path, which is the case for a brief that arrives
        alongside a file already held.

        The media pipeline being absent is not an error: the attachment is held
        at the brief gate and nothing is fetched, which is how a deployment with
        no storage configured should behave.
        """
        pipeline = self._deps.media_pipeline
        if pipeline is None or event.message.media is None:
            if not has_brief(event):
                return await self._answer_deterministically(
                    session,
                    event=event,
                    subject=subject,
                    conversation_id=conversation.id,
                    request_event_id=request_event_id,
                    action=OutboundAction(
                        type=OutboundActionType.ASK_FILE_BRIEF, text=BRIEF_PROMPT
                    ),
                    now=now,
                )
            return None

        # The student's own daily media ceiling, read from the rows the pipeline
        # already wrote. Passed in rather than read inside the pipeline so the
        # pipeline stays free of quota policy.
        allowance = await catalog_repo.load_quota_snapshot(
            session,
            subject_id=subject.id,
            now=now,
            daily_call_limit=plan.daily_call_limit,
            user_daily_budget_micros=plan.user_daily_budget_micros,
            daily_pdf_page_limit=plan.daily_pdf_page_limit,
            daily_ocr_page_limit=plan.daily_ocr_page_limit,
            daily_voice_second_limit=plan.daily_voice_second_limit,
            daily_mock_limit=plan.daily_mock_limit,
        )

        outcome = await pipeline.intake(
            session,
            subject_id=subject.id,
            conversation_id=conversation.id,
            ref=event.message.media,
            message_type=event.message.type,
            brief=event.message.text,
            entitled=entitlement.allows_paid_ai and plan.allows_paid_ai,
            correlation_id=event.correlation_id,
            allowance=allowance,
        )

        if outcome.needs_brief:
            return await self._answer_deterministically(
                session,
                event=event,
                subject=subject,
                conversation_id=conversation.id,
                request_event_id=request_event_id,
                action=OutboundAction(type=OutboundActionType.ASK_FILE_BRIEF, text=BRIEF_PROMPT),
                now=now,
            )

        if outcome.rejected:
            reason = outcome.reject_reason
            text = _MEDIA_REJECTION_TEXT.get(
                reason.value if reason else "",
                "I could not use that file.",
            )
            logger.info("media_rejected", reason=reason.value if reason else None)
            return await self._answer_deterministically(
                session,
                event=event,
                subject=subject,
                conversation_id=conversation.id,
                request_event_id=request_event_id,
                action=OutboundAction(type=OutboundActionType.SEND_TEXT, text=text),
                now=now,
            )

        if outcome.job_created:
            # Extraction is asynchronous by design: it can take minutes, and a
            # request that waits for it holds a Cloud Run instance and a Postgres
            # connection for the whole time.
            return await self._answer_deterministically(
                session,
                event=event,
                subject=subject,
                conversation_id=conversation.id,
                request_event_id=request_event_id,
                action=OutboundAction(
                    type=OutboundActionType.SEND_TEXT,
                    text=(
                        "Got it - I am reading your file now. "
                        "I will come back with the answer shortly."
                    ),
                ),
                now=now,
            )

        return None

    async def _answer_deterministically(
        self,
        session: AsyncSession,
        *,
        event: NormalizedEvent,
        subject: ResolvedSubject,
        conversation_id: UUID,
        request_event_id: UUID,
        action: OutboundAction,
        now: datetime,
    ) -> EventResponse:
        """Complete a turn inside an existing conversation with no model call.

        Used by the media brief gate: the exchange is real and is persisted, it
        simply costs nothing.
        """
        await repo.add_message(
            session,
            conversation_id=conversation_id,
            role="ASSISTANT",
            input_type=str(MessageType.TEXT),
            text=action.text,
            capability="brief_gate",
        )
        await repo.record_outbound_actions(
            session,
            conversation_id=conversation_id,
            request_event_id=request_event_id,
            actions=(action,),
        )
        await repo.record_request_state(
            session, request_event_id=request_event_id, status="COMPLETED"
        )
        response = EventResponse(
            conversation_id=str(conversation_id),
            status=RequestStatus.COMPLETED,
            outbound_actions=(action,),
            usage=UsageSummary(paid_model_calls=0),
        )
        await repo.store_idempotent_response(
            session,
            event.idempotency_key,
            conversation_id=conversation_id,
            response=json.loads(response.model_dump_json()),
            now=now,
        )
        await self._deps.outbound.deliver(subject, response.outbound_actions)
        logger.info("brief_gate_applied", paid_model_calls=0)
        return response

    async def _reject(
        self,
        session: AsyncSession,
        event: NormalizedEvent,
        *,
        subject: ResolvedSubject | None,
        now: datetime,
        error_code: str,
        action: OutboundAction,
    ) -> EventResponse:
        """Terminal rejection before a conversation exists. Never spends."""
        request_event = await repo.record_request_event(
            session,
            event=event,
            subject_id=subject.id if subject else None,
            conversation_id=None,
        )
        await repo.record_request_state(
            session,
            request_event_id=request_event.id,
            status="REJECTED",
            error_code=error_code,
        )
        response = EventResponse(
            conversation_id=None,
            status=RequestStatus.REJECTED,
            outbound_actions=(action,),
            usage=UsageSummary(paid_model_calls=0),
        )
        await repo.store_idempotent_response(
            session,
            event.idempotency_key,
            conversation_id=None,
            response=json.loads(response.model_dump_json()),
            now=now,
        )
        if subject is not None:
            await self._deps.outbound.deliver(subject, response.outbound_actions)
        logger.info("request_rejected", error_code=error_code, paid_model_calls=0)
        return response


def _default_mode(tutor: TutorProfile | None) -> PedagogyMode:
    """Persona supplies the default; an explicit student request overrides it."""
    if tutor is None:
        return PedagogyMode.GUIDED
    if tutor.persona.socratic:
        return PedagogyMode.SOCRATIC
    if tutor.persona.hint_first:
        return PedagogyMode.HINT_FIRST
    if tutor.persona.step_by_step:
        return PedagogyMode.STEP_BY_STEP
    return PedagogyMode.GUIDED


def _fallback_text(decision: ExecutionBudgetDecision) -> str:
    """Deterministic reply for every outcome that does not reach a provider."""
    return {
        BudgetOutcome.REJECT_QUOTA: (
            "You have reached your AI Tutor limit for today. It resets tomorrow."
        ),
        BudgetOutcome.REJECT_SYSTEM_BUDGET: (
            "The AI Tutor is temporarily unavailable. Please try again shortly."
        ),
        BudgetOutcome.REJECT_SIZE: (
            "That message is too long for me to work with. Could you send a "
            "shorter version, or just the specific question?"
        ),
        BudgetOutcome.FEATURE_DISABLED: (
            "The AI Tutor is temporarily unavailable. Please try again shortly."
        ),
        BudgetOutcome.REJECT_PLAN: "AI Tutor is available on the Pro plan.",
    }.get(decision.outcome, "I could not process that request just now.")

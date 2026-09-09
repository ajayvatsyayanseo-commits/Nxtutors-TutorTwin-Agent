"""The teacher's name from signup reaches the runtime.

The signup form collects "favourite teacher name", `assign_named_tutor` writes
it to `tutors` + `tutor_assignments`, and until now the runtime read none of
that: the container wired `FakeTutorGateway`, whose answer is the constant
"Anita Sharma". A test that stubbed the gateway could never catch it, so these
tests go through the real one against the real tables.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tutortwin.db.models import TutorPersonaVersion
from tutortwin.domain.models import ResolvedSubject
from tutortwin.services.subscriptions import (
    WHATSAPP_IDENTITY,
    assign_named_tutor,
    get_or_create_subject,
)
from tutortwin.services.tutors import DatabaseTutorGateway

pytestmark = pytest.mark.integration


def resolved(subject: object) -> ResolvedSubject:
    return ResolvedSubject(
        id=subject.id,  # type: ignore[attr-defined]
        external_type=WHATSAPP_IDENTITY,
        external_id=subject.external_identity_value,  # type: ignore[attr-defined]
    )


async def test_assigned_name_is_the_one_the_student_chose(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    subject = await get_or_create_subject(session, "+919999100001", "Riya")
    await assign_named_tutor(session, subject, "Suresh Iyer")
    await session.commit()

    profile = await DatabaseTutorGateway(session_factory).assigned_tutor(resolved(subject))

    assert profile is not None
    assert profile.display_name == "Suresh Iyer"
    # The one name the agent is allowed to present itself under - a persona
    # styled after the teacher, never a claim to be them.
    assert profile.assistant_identity == "TutorTwin - AI Assistant for Suresh Iyer"


async def test_stored_persona_is_used_not_defaults(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    subject = await get_or_create_subject(session, "+919999100002", "Arjun")
    tutor = await assign_named_tutor(session, subject, "Meera Nair")
    await session.commit()

    version = (
        await session.execute(
            TutorPersonaVersion.__table__.select().where(
                TutorPersonaVersion.tutor_id == tutor.id,
                TutorPersonaVersion.is_active.is_(True),
            )
        )
    ).first()
    assert version is not None

    profile = await DatabaseTutorGateway(session_factory).assigned_tutor(resolved(subject))

    assert profile is not None
    # `assign_named_tutor` writes these; if the gateway silently fell back to
    # `TutorPersona(version=1)` defaults the first two would still pass, so the
    # forbidden-behaviour list is what actually proves the row was read.
    assert profile.persona.hint_first is True
    assert profile.persona.step_by_step is True
    assert any("never claim to be" in rule.lower() for rule in profile.persona.forbidden_behaviors)


async def test_no_assignment_resolves_to_none(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A student who never went through signup has no teacher, and that is fine.

    The entry service treats None as the generic TutorTwin voice. Inventing a
    name here is exactly the bug this module replaced.
    """
    subject = await get_or_create_subject(session, "+919999100003", "Walk-in")
    await session.commit()

    assert await DatabaseTutorGateway(session_factory).assigned_tutor(resolved(subject)) is None


async def test_resubscribing_under_a_new_name_wins(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    subject = await get_or_create_subject(session, "+919999100004", "Kabir")
    await assign_named_tutor(session, subject, "Old Teacher")
    await session.commit()
    await assign_named_tutor(session, subject, "New Teacher")
    await session.commit()

    profile = await DatabaseTutorGateway(session_factory).assigned_tutor(resolved(subject))

    assert profile is not None
    assert profile.display_name == "New Teacher"


async def test_unreadable_persona_json_degrades_instead_of_raising(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Persona JSON is operator-editable, so a bad shape must not 500 a student."""
    subject = await get_or_create_subject(session, "+919999100005", "Nisha")
    tutor = await assign_named_tutor(session, subject, "Broken Persona")
    await session.flush()
    await session.execute(
        TutorPersonaVersion.__table__.update()
        .where(TutorPersonaVersion.tutor_id == tutor.id)
        .values(persona_json={"version": "not-a-number", "tone": []})
    )
    await session.commit()

    profile = await DatabaseTutorGateway(session_factory).assigned_tutor(resolved(subject))

    assert profile is not None
    assert profile.display_name == "Broken Persona"
    assert profile.persona.version == 1

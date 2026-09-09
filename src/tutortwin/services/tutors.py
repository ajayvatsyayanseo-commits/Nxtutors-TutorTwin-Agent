"""The assigned tutor, read from the database instead of a hardcoded name.

Signup asks the student which teacher they want the agent to answer to, and
`assign_named_tutor` faithfully writes a `tutors` row, a persona version and a
`tutor_assignments` row for it. Nothing read them: the container wired
`FakeTutorGateway`, which returns the constant "Anita Sharma" whatever the
student asked for. Every paying student got the same stranger's name in their
system prompt, and the field the signup form spent a line collecting had no
effect on anything.

The name is a persona, never an impersonation - `forbidden_behaviors` carries
that instruction from the persona row into the system prompt, which is why this
reads the stored persona rather than synthesising a default one.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tutortwin.db.models import Tutor, TutorAssignment, TutorPersonaVersion
from tutortwin.domain.models import ResolvedSubject, TutorPersona, TutorProfile
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)


class DatabaseTutorGateway:
    """Resolves the newest active assignment for a subject, or None.

    None is a supported answer, not a failure: a student who reached the agent
    without going through signup has no assignment, and the entry service
    already treats a missing tutor as "generic TutorTwin voice". Inventing a
    name here would be worse than having none.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def assigned_tutor(self, subject: ResolvedSubject) -> TutorProfile | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(Tutor, TutorPersonaVersion)
                    .join(TutorAssignment, TutorAssignment.tutor_id == Tutor.id)
                    # Outer, so a tutor whose persona row is missing still
                    # answers to their name instead of vanishing entirely.
                    .outerjoin(
                        TutorPersonaVersion,
                        (TutorPersonaVersion.tutor_id == Tutor.id)
                        & TutorPersonaVersion.is_active.is_(True),
                    )
                    .where(
                        TutorAssignment.subject_id == subject.id,
                        TutorAssignment.is_active.is_(True),
                    )
                    # Newest assignment wins. A student who re-subscribes under a
                    # different teacher's name gets the teacher they just asked
                    # for, not the one they asked for last year.
                    .order_by(TutorAssignment.created_at.desc(), TutorPersonaVersion.version.desc())
                    .limit(1)
                )
            ).first()

        if row is None:
            return None

        tutor, persona_row = row
        return TutorProfile(
            id=tutor.id,
            display_name=tutor.display_name,
            persona=_persona(persona_row, tutor_id=tutor.id),
        )


def _persona(row: TutorPersonaVersion | None, *, tutor_id: object) -> TutorPersona:
    """Stored persona, falling back to the defaults rather than raising.

    A persona row is operator-editable JSON. If somebody saves a shape this
    build does not understand, the right outcome is a student who gets a
    slightly plainer tutor, not a student who gets an error - so the failure is
    logged and the defaults stand in.
    """
    if row is None:
        return TutorPersona(version=1)
    try:
        return TutorPersona.model_validate(row.persona_json)
    except Exception:
        logger.warning("persona_unreadable", tutor_id=str(tutor_id), version=row.version)
        return TutorPersona(version=row.version)


__all__ = ["DatabaseTutorGateway"]

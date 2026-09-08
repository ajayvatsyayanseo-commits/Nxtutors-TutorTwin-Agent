"""Admin control-plane routers.

Split by operational area rather than by table, because that is how an operator
thinks: "why did this student get no answer" crosses four tables and one module.
"""

from __future__ import annotations

from fastapi import APIRouter

from tutortwin.api.routes.admin import (
    auth,
    catalog,
    jobs,
    overview,
    students,
    tutors,
)

router = APIRouter(prefix="/v1/admin", tags=["admin"])
router.include_router(auth.router)
router.include_router(overview.router)
router.include_router(students.router)
router.include_router(tutors.router)
router.include_router(catalog.router)
router.include_router(jobs.router)

__all__ = ["router"]

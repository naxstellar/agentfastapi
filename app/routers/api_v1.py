from fastapi import APIRouter, Depends

from app.auth.security import CurrentUserDep, get_current_user, require_csrf_header
from app.routers.datasets import router as datasets_router
from app.routers.memories import router as memories_router
from app.routers.settings import router as settings_router
from app.routers.runs import router as runs_router
from app.routers.usage import router as usage_router

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_csrf_header), Depends(get_current_user)],
)


@router.get("/me")
def read_me(user: CurrentUserDep) -> dict:
    return user


router.include_router(datasets_router)
router.include_router(runs_router)
router.include_router(usage_router)
router.include_router(memories_router)
router.include_router(settings_router)

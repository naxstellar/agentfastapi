from fastapi import APIRouter, Depends

from app.auth.security import CurrentUserDep, get_current_user, require_csrf_header
from app.routers.datasets import router as datasets_router

router = APIRouter(
    prefix="/api/v1",
    dependencies=[Depends(require_csrf_header), Depends(get_current_user)],
)


@router.get("/me")
def read_me(user: CurrentUserDep) -> dict:
    return user


router.include_router(datasets_router)

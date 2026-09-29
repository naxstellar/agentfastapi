from fastapi import APIRouter, Depends

from app.auth.security import CurrentUserDep, get_current_user

router = APIRouter(prefix="/api/v1", dependencies=[Depends(get_current_user)])


@router.get("/me")
def read_me(user: CurrentUserDep) -> dict:
    return user

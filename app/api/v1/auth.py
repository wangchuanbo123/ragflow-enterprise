"""鉴权接口：登录、退出、当前用户。"""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, LoginResponse, PasswordChange, UserOut
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, response: Response, request: Request, db: Session = Depends(get_db)):
    address = request.client.host if request.client else "unknown"
    user, token = auth_service.login(db, payload.username, payload.password, address)
    auth_service.attach_token_cookie(response, token)
    return LoginResponse(user=UserOut.model_validate(user))


@router.post("/logout")
def logout(response: Response, request: Request, db: Session = Depends(get_db)):
    from app.core.security import read_auth_cookie

    auth_service.logout(response, db, read_auth_cookie(request))
    return {"message": "已退出"}


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/password", status_code=204)
def change_password(
    payload: PasswordChange,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    auth_service.change_password(db, current_user, payload.current_password, payload.new_password)
    auth_service.logout(response)

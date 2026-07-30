"""鉴权接口：登录、退出、当前用户。"""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, LoginResponse, UserOut
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user, token = auth_service.login(db, payload.username, payload.password)
    auth_service.attach_token_cookie(response, token)
    return LoginResponse(user=UserOut.model_validate(user))


@router.post("/logout")
def logout(response: Response):
    auth_service.logout(response)
    return {"message": "已退出"}


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user

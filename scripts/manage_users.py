"""Local account recovery; passwords are read interactively and never logged."""

import argparse
import getpass


def main() -> int:
    parser = argparse.ArgumentParser(description="服务器本机账号恢复（请先停止应用服务）")
    parser.add_argument("action", choices=["list", "create", "reset-password"])
    parser.add_argument("--username")
    parser.add_argument("--role", choices=["admin", "user"], default="user")
    args = parser.parse_args()
    if args.action != "list" and not args.username:
        parser.error("请指定 --username")

    from sqlalchemy import select
    from app.core.database import SessionLocal
    from app.core.init_db import run_migrations
    from app.models.user import User
    from app.schemas.auth import UserCreate, UserUpdate
    from app.services.user_service import create_user, update_user
    from app.api.errors import APIError
    from pydantic import ValidationError

    run_migrations()
    with SessionLocal() as db:
        if args.action == "list":
            for user in db.scalars(select(User).order_by(User.username)):
                print(f"{user.username}\t{user.role}\t{'enabled' if user.is_active else 'disabled'}")
            return 0
        password = getpass.getpass("新密码（12–72 UTF-8字节，包含字母和数字）: ")
        if password != getpass.getpass("再次输入新密码: "):
            parser.error("两次密码不一致")
        try:
            if args.action == "create":
                create_user(db, UserCreate(username=args.username, password=password, role=args.role))
            else:
                user = db.scalar(select(User).where(User.username == args.username))
                if user is None:
                    parser.error("用户不存在")
                update_user(db, user.id, UserUpdate(password=password))
        except ValidationError:
            parser.error("用户名或密码不符合格式要求")
        except APIError as exc:
            parser.error(exc.message)
    print("账号已更新；旧会话已撤销。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import User
from app.routes.types import SessionDependency
from app.security import hash_password, verify_password
from app.services.common import flash, take_flashes, utcnow


def create_auth_router(
    get_session: SessionDependency,
    templates: Jinja2Templates,
) -> APIRouter:
    router = APIRouter()

    @router.get("/signup", response_class=HTMLResponse)
    def signup_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={"request": request, "mode": "signup", "flashes": take_flashes(request)},
            headers={"Cache-Control": "private, no-store"},
        )

    @router.post("/signup")
    def signup(
        request: Request,
        email: str = Form(),
        password: str = Form(),
        session: Session = Depends(get_session),
    ) -> RedirectResponse:
        normalized_email = email.strip().lower()
        if (
            len(normalized_email) > 320
            or "@" not in normalized_email
            or "." not in normalized_email.rsplit("@", 1)[-1]
        ):
            flash(request, "Enter a valid email address.", "error")
            return RedirectResponse("/signup", status_code=303)
        if not 8 <= len(password) <= 64:
            flash(request, "Password must be between 8 and 64 characters.", "error")
            return RedirectResponse("/signup", status_code=303)
        user = User(
            email=normalized_email,
            password_hash=hash_password(password),
            created_at=utcnow(),
        )
        session.add(user)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            flash(request, "An account with that email already exists.", "error")
            return RedirectResponse("/signup", status_code=303)
        request.session.clear()
        request.session["user_id"] = user.id
        flash(request, "Your account is ready. Welcome to MarketPulse.", "success")
        return RedirectResponse("/", status_code=303)

    @router.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={"request": request, "mode": "login", "flashes": take_flashes(request)},
            headers={"Cache-Control": "private, no-store"},
        )

    @router.post("/login")
    def login(
        request: Request,
        email: str = Form(),
        password: str = Form(),
        session: Session = Depends(get_session),
    ) -> RedirectResponse:
        user = session.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None or not verify_password(password, user.password_hash):
            flash(request, "Email or password is incorrect.", "error")
            return RedirectResponse("/login", status_code=303)
        request.session.clear()
        request.session["user_id"] = user.id
        flash(request, "You are signed in.", "success")
        return RedirectResponse("/", status_code=303)

    @router.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        flash(request, "You are signed out.", "info")
        return RedirectResponse("/", status_code=303)

    return router

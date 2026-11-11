from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.briefs import save_brief
from app.config import Settings
from app.models import (
    Article,
    Brief,
    PaypalOrder,
    ProcessedPaymentCapture,
)
from app.paypal import PayPalError
from app.routes.types import PayPalClientFactory, SessionDependency
from app.services.common import article_is_unlocked, current_user, flash, take_flashes, utcnow
from app.services.payments import (
    BRIEF_CURRENCY,
    BRIEF_PRICE,
    amount_matches,
    approval_url,
    paypal_http_exception,
    purchase_unit,
)


def create_brief_router(
    get_session: SessionDependency,
    settings: Settings,
    templates: Jinja2Templates,
    paypal_client_factory: PayPalClientFactory,
) -> APIRouter:
    router = APIRouter()

    @router.post("/brief/{article_id:int}/purchase")
    def purchase_brief(
        article_id: int,
        request: Request,
        session: Session = Depends(get_session),
    ) -> RedirectResponse:
        user = current_user(request, session)
        if user is None:
            flash(request, "Sign in to purchase a Deep Brief.", "error")
            return RedirectResponse("/login", status_code=303)
        article = session.get(Article, article_id)
        if article is None:
            raise HTTPException(status_code=404, detail="Article not found.")
        existing_brief = session.scalar(
            select(Brief).where(Brief.user_id == user.id, Brief.article_id == article_id)
        )
        if existing_brief:
            return RedirectResponse(f"/brief/{article_id}", status_code=303)
        if not article_is_unlocked(article, user):
            raise HTTPException(status_code=404, detail="Article not found.")
        base_url = settings.base_url.rstrip("/")
        try:
            result = paypal_client_factory(settings).create_order(
                user.id,
                article.id,
                article.title,
                f"{base_url}/brief/return",
                f"{base_url}/brief/cancel",
            )
        except PayPalError as exc:
            raise paypal_http_exception(exc) from exc
        order_id = result.get("id")
        next_approval_url = approval_url(result)
        if not isinstance(order_id, str) or not next_approval_url:
            raise HTTPException(status_code=502, detail="PayPal did not return a valid approval link.")
        session.add(
            PaypalOrder(
                user_id=user.id,
                article_id=article.id,
                paypal_order_id=order_id,
                status="CREATED",
                amount="1.00",
                currency="USD",
                created_at=utcnow(),
            )
        )
        session.commit()
        return RedirectResponse(next_approval_url, status_code=303)

    @router.get("/brief/cancel")
    def brief_purchase_cancelled(request: Request) -> RedirectResponse:
        flash(request, "Deep Brief purchase was cancelled. No payment was taken.", "info")
        return RedirectResponse("/", status_code=303)

    @router.get("/brief/return")
    def brief_purchase_return(
        request: Request,
        token: Annotated[str, Query(min_length=1, max_length=64)],
        session: Session = Depends(get_session),
    ) -> RedirectResponse:
        user = current_user(request, session)
        if user is None:
            flash(request, "Sign in again to confirm your Deep Brief purchase.", "error")
            return RedirectResponse("/login", status_code=303)
        order = session.scalar(select(PaypalOrder).where(PaypalOrder.paypal_order_id == token))
        if order is None or order.user_id != user.id:
            raise HTTPException(status_code=403, detail="This order does not belong to your account.")
        if order.status == "COMPLETED":
            return RedirectResponse(f"/brief/{order.article_id}", status_code=303)
        article = session.get(Article, order.article_id)
        if article is None:
            raise HTTPException(status_code=404, detail="Article not found.")
        expected_custom_id = f"{order.user_id}:{order.article_id}"
        try:
            paypal = paypal_client_factory(settings)
            remote_order = paypal.get_order(token)
            remote_unit = purchase_unit(remote_order)
            remote_amount = remote_unit.get("amount") if remote_unit else None
            if (
                remote_order.get("id") != token
                or remote_order.get("status") != "APPROVED"
                or remote_unit is None
                or remote_unit.get("custom_id") != expected_custom_id
                or not isinstance(remote_amount, dict)
                or not amount_matches(
                    remote_amount.get("value"),
                    remote_amount.get("currency_code"),
                    BRIEF_PRICE,
                    BRIEF_CURRENCY,
                )
            ):
                raise HTTPException(
                    status_code=400,
                    detail="PayPal order details did not match this purchase.",
                )
            captured = paypal.capture_order(token)
        except PayPalError as exc:
            raise paypal_http_exception(exc) from exc

        captured_unit = purchase_unit(captured)
        payments = captured_unit.get("payments") if captured_unit else None
        captures = payments.get("captures") if isinstance(payments, dict) else None
        capture = captures[0] if isinstance(captures, list) and captures else None
        capture_amount = capture.get("amount") if isinstance(capture, dict) else None
        capture_id = capture.get("id") if isinstance(capture, dict) else None
        if (
            captured.get("id") != token
            or captured_unit is None
            or captured_unit.get("custom_id") != expected_custom_id
            or not isinstance(capture, dict)
            or capture.get("status") != "COMPLETED"
            or not isinstance(capture_id, str)
            or not isinstance(capture_amount, dict)
            or not amount_matches(
                capture_amount.get("value"),
                capture_amount.get("currency_code"),
                BRIEF_PRICE,
                BRIEF_CURRENCY,
            )
        ):
            raise HTTPException(
                status_code=400,
                detail="PayPal capture was not completed for the expected amount.",
            )

        if session.get(ProcessedPaymentCapture, capture_id) is not None:
            raise HTTPException(status_code=409, detail="This PayPal capture was already used.")
        session.add(
            ProcessedPaymentCapture(
                capture_id=capture_id,
                event_id=f"return:{token}",
                received_at=utcnow(),
            )
        )
        save_brief(session, user.id, article, settings.groq_api_key.get_secret_value())
        order.status = "COMPLETED"
        order.capture_id = capture_id
        session.commit()
        flash(request, "Your Deep Brief is ready.", "success")
        return RedirectResponse(f"/brief/{article.id}", status_code=303)

    @router.get("/brief/{article_id:int}", response_class=HTMLResponse, response_model=None)
    def view_brief(
        article_id: int,
        request: Request,
        session: Session = Depends(get_session),
    ) -> HTMLResponse | RedirectResponse:
        user = current_user(request, session)
        if user is None:
            flash(request, "Sign in to view your Deep Briefs.", "error")
            return RedirectResponse("/login", status_code=303)
        article = session.get(Article, article_id)
        if article is None:
            raise HTTPException(status_code=404, detail="Article not found.")
        brief = session.scalar(
            select(Brief).where(Brief.user_id == user.id, Brief.article_id == article.id)
        )
        if brief is None and not article_is_unlocked(article, user):
            raise HTTPException(status_code=404, detail="Article not found.")
        return templates.TemplateResponse(
            request=request,
            name="brief.html",
            context={
                "user": user,
                "article": article,
                "brief": brief,
                "flashes": take_flashes(request),
            },
            headers={"Cache-Control": "private, no-store"},
        )

    return router

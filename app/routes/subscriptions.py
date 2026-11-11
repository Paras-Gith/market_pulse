from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import PaypalSubscription
from app.paypal import PayPalError
from app.routes.types import PayPalClientFactory, SessionDependency
from app.services.common import current_user, flash, refresh_pro_status, utcnow
from app.services.payments import approval_url, paypal_http_exception


def create_subscription_router(
    get_session: SessionDependency,
    settings: Settings,
    paypal_client_factory: PayPalClientFactory,
) -> APIRouter:
    router = APIRouter()

    @router.post("/subscribe")
    def subscribe(
        request: Request,
        session: Session = Depends(get_session),
    ) -> RedirectResponse:
        user = current_user(request, session)
        if user is None:
            flash(request, "Sign in before starting a Pro subscription.", "error")
            return RedirectResponse("/login", status_code=303)
        if user.is_pro:
            flash(request, "Your Pro subscription is already active.", "info")
            return RedirectResponse("/", status_code=303)
        pending = session.scalar(
            select(PaypalSubscription)
            .where(
                PaypalSubscription.user_id == user.id,
                PaypalSubscription.status == "APPROVAL_PENDING",
            )
            .order_by(PaypalSubscription.id.desc())
        )
        base_url = settings.base_url.rstrip("/")
        if pending and pending.approval_url and pending.approval_base_url == base_url:
            return RedirectResponse(pending.approval_url, status_code=303)
        if not settings.paypal_plan_id:
            raise HTTPException(status_code=503, detail="PayPal plan is not configured.")
        try:
            result = paypal_client_factory(settings).create_subscription(
                settings.paypal_plan_id,
                user.id,
                f"{base_url}/subscribe/return",
                f"{base_url}/subscribe/cancel",
            )
        except PayPalError as exc:
            raise paypal_http_exception(exc) from exc
        subscription_id = result.get("id")
        next_approval_url = approval_url(result)
        if not isinstance(subscription_id, str) or not next_approval_url:
            raise HTTPException(status_code=502, detail="PayPal did not return a valid approval link.")
        session.add(
            PaypalSubscription(
                user_id=user.id,
                paypal_subscription_id=subscription_id,
                status="APPROVAL_PENDING",
                approval_url=next_approval_url,
                approval_base_url=base_url,
                created_at=utcnow(),
            )
        )
        session.commit()
        return RedirectResponse(next_approval_url, status_code=303)

    @router.get("/subscribe/return")
    def subscription_return(
        request: Request,
        subscription_id: Annotated[str, Query(min_length=1, max_length=64)],
        session: Session = Depends(get_session),
    ) -> RedirectResponse:
        user = current_user(request, session)
        if user is None:
            flash(request, "Sign in to confirm your subscription.", "error")
            return RedirectResponse("/login", status_code=303)
        subscription = session.scalar(
            select(PaypalSubscription).where(
                PaypalSubscription.paypal_subscription_id == subscription_id
            )
        )
        if subscription is None or subscription.user_id != user.id:
            raise HTTPException(
                status_code=403,
                detail="This subscription does not belong to your account.",
            )
        try:
            remote = paypal_client_factory(settings).get_subscription(subscription_id)
        except PayPalError as exc:
            raise paypal_http_exception(exc) from exc
        status = remote.get("status")
        if remote.get("custom_id") != str(user.id):
            raise HTTPException(
                status_code=400,
                detail="PayPal subscription ownership could not be verified.",
            )
        if isinstance(status, str):
            subscription.status = status
        if status == "ACTIVE":
            user.is_pro = True
            flash(request, "PayPal confirmed your Pro subscription. Welcome aboard!", "success")
        else:
            refresh_pro_status(session, user)
            flash(
                request,
                f"Subscription status: {status or 'pending'}. "
                "Pro access starts when PayPal activates it.",
            )
        session.commit()
        return RedirectResponse("/", status_code=303)

    @router.get("/subscribe/cancel")
    def subscription_approval_cancelled(request: Request) -> RedirectResponse:
        flash(request, "Subscription approval was cancelled. No payment was taken.", "info")
        return RedirectResponse("/", status_code=303)

    @router.post("/subscribe/cancel")
    def cancel_subscription(
        request: Request,
        session: Session = Depends(get_session),
    ) -> RedirectResponse:
        user = current_user(request, session)
        if user is None:
            return RedirectResponse("/login", status_code=303)
        subscription = session.scalar(
            select(PaypalSubscription)
            .where(
                PaypalSubscription.user_id == user.id,
                PaypalSubscription.status == "ACTIVE",
            )
            .order_by(PaypalSubscription.id.desc())
        )
        if subscription is None:
            flash(request, "No active Pro subscription was found.", "info")
            return RedirectResponse("/", status_code=303)
        try:
            paypal_client_factory(settings).cancel_subscription(
                subscription.paypal_subscription_id,
                "Cancellation requested by MarketPulse AI account holder",
            )
        except PayPalError as exc:
            raise paypal_http_exception(exc) from exc
        subscription.status = "CANCELLED"
        refresh_pro_status(session, user)
        session.commit()
        flash(request, "Your PayPal subscription has been cancelled.", "success")
        return RedirectResponse("/", status_code=303)

    return router

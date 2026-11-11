from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import Settings
from app.contracts import PayPalWebhookEvent
from app.paypal import PayPalError
from app.routes.types import PayPalClientFactory, SessionDependency
from app.services.payments import handle_verified_webhook


def create_webhook_router(
    get_session: SessionDependency,
    settings: Settings,
    paypal_client_factory: PayPalClientFactory,
) -> APIRouter:
    router = APIRouter()

    @router.post("/webhooks/paypal")
    async def paypal_webhook(
        request: Request,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid webhook payload.") from exc
        try:
            event = PayPalWebhookEvent.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail="Invalid webhook payload.") from exc
        try:
            verified = paypal_client_factory(settings).verify_webhook_signature(
                dict(request.headers),
                event.model_dump(mode="json", exclude_unset=True),
                settings.paypal_webhook_id,
            )
        except PayPalError as exc:
            raise HTTPException(
                status_code=503,
                detail="PayPal signature verification is unavailable.",
            ) from exc
        if not verified:
            raise HTTPException(status_code=400, detail="Invalid PayPal webhook signature.")
        duplicate = handle_verified_webhook(session, event, settings)
        session.commit()
        return JSONResponse({"status": "duplicate" if duplicate else "accepted"})

    return router

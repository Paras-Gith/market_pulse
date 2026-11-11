import logging
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.briefs import save_brief
from app.config import Settings
from app.contracts import PayPalWebhookEvent
from app.models import (
    Article,
    PaypalOrder,
    PaypalSubscription,
    ProcessedPaymentCapture,
    ProcessedWebhookEvent,
    User,
)
from app.paypal import PayPalError, PayPalNotConfigured, PayPalUnavailable
from app.services.common import refresh_pro_status, utcnow

logger = logging.getLogger(__name__)
BRIEF_PRICE = Decimal("1.00")
BRIEF_CURRENCY = "USD"


def paypal_http_exception(error: PayPalError) -> HTTPException:
    if isinstance(error, (PayPalNotConfigured, PayPalUnavailable)):
        return HTTPException(status_code=503, detail=str(error))
    return HTTPException(status_code=502, detail=str(error))


def approval_url(response: dict[str, object]) -> str | None:
    links = response.get("links")
    if not isinstance(links, list):
        return None
    for link in links:
        if isinstance(link, dict) and link.get("rel") in {"approve", "payer-action"}:
            href = link.get("href")
            return href if isinstance(href, str) and href.startswith("https://") else None
    return None


def purchase_unit(response: dict[str, object]) -> dict[str, object] | None:
    units = response.get("purchase_units")
    if not isinstance(units, list) or not units or not isinstance(units[0], dict):
        return None
    return units[0]


def amount_matches(
    value: object,
    currency: object,
    expected: Decimal,
    expected_currency: str,
) -> bool:
    if not isinstance(value, str) or currency != expected_currency:
        return False
    try:
        return Decimal(value) == expected
    except (InvalidOperation, ValueError):
        return False


def record_capture(session: Session, capture_id: str | None, event_id: str) -> bool:
    if not capture_id:
        return True
    if session.get(ProcessedPaymentCapture, capture_id) is not None:
        return False
    session.add(
        ProcessedPaymentCapture(
            capture_id=capture_id,
            event_id=event_id,
            received_at=utcnow(),
        )
    )
    return True


def handle_verified_webhook(
    session: Session,
    event: PayPalWebhookEvent,
    settings: Settings,
) -> bool:
    if session.get(ProcessedWebhookEvent, event.id) is not None:
        return True

    resource = event.resource
    if event.event_type.startswith("BILLING.SUBSCRIPTION."):
        status_by_event = {
            "BILLING.SUBSCRIPTION.ACTIVATED": "ACTIVE",
            "BILLING.SUBSCRIPTION.CANCELLED": "CANCELLED",
            "BILLING.SUBSCRIPTION.SUSPENDED": "SUSPENDED",
            "BILLING.SUBSCRIPTION.EXPIRED": "EXPIRED",
        }
        status = status_by_event.get(event.event_type)
        if resource.id and status:
            subscription = session.scalar(
                select(PaypalSubscription).where(
                    PaypalSubscription.paypal_subscription_id == resource.id
                )
            )
            if subscription is not None:
                if resource.custom_id is None or resource.custom_id == str(subscription.user_id):
                    subscription.status = status
                    user = session.get(User, subscription.user_id)
                    if user is not None:
                        refresh_pro_status(session, user)
                else:
                    logger.warning("Ignoring PayPal subscription event with mismatched custom_id")
    elif event.event_type == "PAYMENT.CAPTURE.COMPLETED":
        capture_id = resource.id
        is_new_capture = record_capture(session, capture_id, event.id)
        related_ids = resource.supplementary_data
        order_id = related_ids.related_ids.order_id if (
            related_ids is not None and related_ids.related_ids is not None
        ) else None
        order = (
            session.scalar(select(PaypalOrder).where(PaypalOrder.paypal_order_id == order_id))
            if order_id
            else None
        )
        amount = resource.amount
        if (
            is_new_capture
            and order is not None
            and order.status != "COMPLETED"
            and capture_id is not None
            and resource.status == "COMPLETED"
            and amount is not None
            and amount_matches(
                amount.value,
                amount.currency_code,
                Decimal(order.amount),
                order.currency,
            )
        ):
            article = session.get(Article, order.article_id)
            if article is not None:
                save_brief(session, order.user_id, article, settings.groq_api_key.get_secret_value())
                order.status = "COMPLETED"
                order.capture_id = capture_id

    session.add(
        ProcessedWebhookEvent(
            event_id=event.id,
            event_type=event.event_type,
            received_at=utcnow(),
        )
    )
    return False

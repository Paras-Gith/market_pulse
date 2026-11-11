from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from app.config import Settings
from app.database import Base, create_database
from app.main import create_app
from app.models import (
    Article,
    Brief,
    PaypalOrder,
    PaypalSubscription,
    ProcessedPaymentCapture,
    ProcessedWebhookEvent,
    User,
)
from app.paypal import PayPalRequestFailed, PayPalUnavailable
from fastapi.testclient import TestClient
from sqlalchemy import select


class FakePayPal:
    subscriptions: dict[str, dict[str, object]] = {}
    orders: dict[str, dict[str, object]] = {}
    capture_result: dict[str, object] = {}
    capture_calls = 0
    verify_result = True
    verify_error: Exception | None = None

    def __init__(self, _settings):
        pass

    def create_subscription(self, plan_id, user_id, return_url, cancel_url):
        self.created_subscription = {
            "plan_id": plan_id,
            "custom_id": str(user_id),
            "return_url": return_url,
            "cancel_url": cancel_url,
        }
        return {
            "id": "I-SUBSCRIPTION-1",
            "status": "APPROVAL_PENDING",
            "links": [{"rel": "approve", "href": "https://paypal.test/approve-subscription"}],
        }

    def get_subscription(self, subscription_id):
        return self.subscriptions[subscription_id]

    def cancel_subscription(self, subscription_id, reason):
        self.cancelled_subscription = (subscription_id, reason)
        return {}

    def create_order(self, user_id, article_id, title, return_url, cancel_url):
        self.created_order = {
            "user_id": user_id,
            "article_id": article_id,
            "title": title,
            "return_url": return_url,
            "cancel_url": cancel_url,
        }
        return {
            "id": "ORDER-1",
            "status": "CREATED",
            "links": [{"rel": "approve", "href": "https://paypal.test/approve-order"}],
        }

    def get_order(self, order_id):
        return self.orders[order_id]

    def capture_order(self, order_id):
        self.capture_calls += 1
        return self.capture_result

    def verify_webhook_signature(self, _headers, _event, _webhook_id):
        if self.verify_error:
            raise self.verify_error
        return self.verify_result


@pytest.fixture
def paypal_app(monkeypatch):
    FakePayPal.subscriptions = {}
    FakePayPal.orders = {}
    FakePayPal.capture_result = {}
    FakePayPal.capture_calls = 0
    FakePayPal.verify_result = True
    FakePayPal.verify_error = None
    monkeypatch.setattr("app.main.PayPalClient", FakePayPal)

    engine, session_factory = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    app = create_app(
        Settings(
            database_url="sqlite:///:memory:",
            app_secret_key="test-secret",
            base_url="http://marketpulse.test/",
            paypal_mode="sandbox",
            paypal_client_id="test-client",
            paypal_client_secret="test-secret",
            paypal_plan_id="P-TEST",
            paypal_webhook_id="WH-TEST",
            news_feeds="",
        ),
        session_factory=session_factory,
    )
    with TestClient(app) as client:
        yield client, session_factory
    engine.dispose()


def _signup(client: TestClient, email: str) -> None:
    response = client.post(
        "/signup",
        data={"email": email, "password": "secure-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _add_old_article(session_factory) -> Article:
    with session_factory() as session:
        article = Article(
            title="Nifty companies report quarterly growth",
            summary="The report describes a change in quarterly market activity.",
            link="https://news.example/article/brief-test",
            source="Example News",
            category="Earnings",
            sentiment="bullish",
            insight="The report describes growth, though further details remain important.",
            published_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2),
            is_demo=False,
        )
        session.add(article)
        session.commit()
        session.refresh(article)
        return article


def test_subscription_return_only_enables_pro_after_paypal_reports_active(paypal_app):
    client, session_factory = paypal_app
    _signup(client, "subscriber@example.com")

    approval = client.post("/subscribe", follow_redirects=False)
    assert approval.status_code == 303
    assert approval.headers["location"] == "https://paypal.test/approve-subscription"
    with session_factory() as session:
        subscription = session.scalar(select(PaypalSubscription))
        user = session.scalar(select(User).where(User.email == "subscriber@example.com"))
        assert subscription is not None
        assert subscription.status == "APPROVAL_PENDING"
        assert subscription.user_id == user.id

    FakePayPal.subscriptions["I-SUBSCRIPTION-1"] = {
        "id": "I-SUBSCRIPTION-1",
        "status": "ACTIVE",
        "custom_id": str(user.id),
    }
    returned = client.get(
        "/subscribe/return?subscription_id=I-SUBSCRIPTION-1",
        follow_redirects=False,
    )
    assert returned.status_code == 303
    with session_factory() as session:
        assert session.get(User, user.id).is_pro is True
        assert session.scalar(select(PaypalSubscription)).status == "ACTIVE"


def test_subscription_replaces_pending_approval_created_for_old_base_url(paypal_app, monkeypatch):
    client, session_factory = paypal_app
    _signup(client, "stale-approval@example.com")
    client.post("/subscribe", follow_redirects=False)
    with session_factory() as session:
        pending = session.scalar(select(PaypalSubscription))
        pending.approval_base_url = "https://old-tunnel.example"
        session.commit()

    def create_new_subscription(self, plan_id, user_id, return_url, cancel_url):
        assert return_url == "http://marketpulse.test/subscribe/return"
        return {
            "id": "I-SUBSCRIPTION-NEW",
            "status": "APPROVAL_PENDING",
            "links": [{"rel": "approve", "href": "https://paypal.test/new-approval"}],
        }

    monkeypatch.setattr(FakePayPal, "create_subscription", create_new_subscription)
    response = client.post("/subscribe", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "https://paypal.test/new-approval"
    with session_factory() as session:
        subscriptions = session.scalars(
            select(PaypalSubscription).order_by(PaypalSubscription.id)
        ).all()
        assert len(subscriptions) == 2
        assert subscriptions[-1].approval_base_url == "http://marketpulse.test"


def test_cancel_subscription_updates_account_after_paypal_success(paypal_app):
    client, session_factory = paypal_app
    _signup(client, "cancel@example.com")
    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == "cancel@example.com"))
        user.is_pro = True
        session.add(
            PaypalSubscription(
                user_id=user.id,
                paypal_subscription_id="I-SUBSCRIPTION-CANCEL",
                status="ACTIVE",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        session.commit()
    response = client.post("/subscribe/cancel", follow_redirects=False)
    assert response.status_code == 303
    with session_factory() as session:
        assert session.scalar(select(User).where(User.email == "cancel@example.com")).is_pro is False
        assert session.scalar(select(PaypalSubscription)).status == "CANCELLED"


def test_webhook_rejects_invalid_signature_and_returns_503_if_unavailable(paypal_app):
    client, _ = paypal_app
    event = {"id": "WH-EVENT-1", "event_type": "BILLING.SUBSCRIPTION.ACTIVATED", "resource": {}}
    FakePayPal.verify_result = False
    invalid = client.post("/webhooks/paypal", json=event)
    assert invalid.status_code == 400

    FakePayPal.verify_error = PayPalUnavailable("offline")
    unavailable = client.post("/webhooks/paypal", json=event)
    assert unavailable.status_code == 503


def test_subscription_webhook_lifecycle_and_duplicate_event_are_idempotent(paypal_app):
    client, session_factory = paypal_app
    _signup(client, "webhook@example.com")
    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == "webhook@example.com"))
        session.add(
            PaypalSubscription(
                user_id=user.id,
                paypal_subscription_id="I-WEBHOOK-SUB",
                status="APPROVAL_PENDING",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        session.commit()

    activated = {
        "id": "WH-ACTIVATED",
        "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
        "resource": {"id": "I-WEBHOOK-SUB", "custom_id": "1"},
    }
    response = client.post("/webhooks/paypal", json=activated)
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert client.post("/webhooks/paypal", json=activated).json() == {"status": "duplicate"}
    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == "webhook@example.com"))
        assert user.is_pro is True
        assert session.scalar(select(PaypalSubscription)).status == "ACTIVE"

    cancelled = {
        "id": "WH-CANCELLED",
        "event_type": "BILLING.SUBSCRIPTION.CANCELLED",
        "resource": {"id": "I-WEBHOOK-SUB", "custom_id": "1"},
    }
    assert client.post("/webhooks/paypal", json=cancelled).status_code == 200
    with session_factory() as session:
        assert session.scalar(select(User).where(User.email == "webhook@example.com")).is_pro is False
        assert session.scalar(select(PaypalSubscription)).status == "CANCELLED"
        assert len(list(session.scalars(select(ProcessedWebhookEvent)))) == 2


def _configure_brief_order(user_id: int, article_id: int, *, amount: str = "1.00") -> None:
    FakePayPal.orders["ORDER-1"] = {
        "id": "ORDER-1",
        "status": "APPROVED",
        "purchase_units": [
            {
                "custom_id": f"{user_id}:{article_id}",
                "amount": {"currency_code": "USD", "value": "1.00"},
            }
        ],
    }
    FakePayPal.capture_result = {
        "id": "ORDER-1",
        "status": "COMPLETED",
        "purchase_units": [
            {
                "custom_id": f"{user_id}:{article_id}",
                "payments": {
                    "captures": [
                        {
                            "id": "CAPTURE-1",
                            "status": "COMPLETED",
                            "amount": {"currency_code": "USD", "value": amount},
                        }
                    ]
                },
            }
        ],
    }


def test_brief_purchase_captures_verified_one_dollar_order_and_saves_fallback(paypal_app):
    client, session_factory = paypal_app
    article = _add_old_article(session_factory)
    _signup(client, "buyer@example.com")
    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == "buyer@example.com"))
        user_id = user.id

    approval = client.post(f"/brief/{article.id}/purchase", follow_redirects=False)
    assert approval.status_code == 303
    assert approval.headers["location"] == "https://paypal.test/approve-order"
    with session_factory() as session:
        order = session.scalar(select(PaypalOrder))
        assert order.user_id == user_id
        assert order.article_id == article.id
        assert order.status == "CREATED"

    _configure_brief_order(user_id, article.id)
    returned = client.get("/brief/return?token=ORDER-1", follow_redirects=False)
    assert returned.status_code == 303
    assert returned.headers["location"] == f"/brief/{article.id}"
    with session_factory() as session:
        brief = session.scalar(select(Brief))
        assert brief is not None
        assert "Nifty companies report" in brief.what_happened
        assert "does not independently establish a market impact" in brief.why_it_matters
        assert session.scalar(select(PaypalOrder)).status == "COMPLETED"
        assert session.get(ProcessedPaymentCapture, "CAPTURE-1") is not None


def test_brief_return_rejects_wrong_logged_in_account_before_capture(paypal_app):
    client, session_factory = paypal_app
    article = _add_old_article(session_factory)
    _signup(client, "first@example.com")
    with session_factory() as session:
        first_user = session.scalar(select(User).where(User.email == "first@example.com"))
        first_user_id = first_user.id
        session.add(
            PaypalOrder(
                user_id=first_user_id,
                article_id=article.id,
                paypal_order_id="ORDER-1",
                status="CREATED",
                amount="1.00",
                currency="USD",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        session.commit()
    client.post("/logout", follow_redirects=False)
    _signup(client, "second@example.com")

    response = client.get("/brief/return?token=ORDER-1", follow_redirects=False)
    assert response.status_code == 403
    assert FakePayPal.capture_calls == 0
    with session_factory() as session:
        assert session.scalar(select(Brief)) is None


def test_brief_return_rejects_wrong_capture_amount(paypal_app):
    client, session_factory = paypal_app
    article = _add_old_article(session_factory)
    _signup(client, "wrong-amount@example.com")
    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == "wrong-amount@example.com"))
        user_id = user.id
        session.add(
            PaypalOrder(
                user_id=user_id,
                article_id=article.id,
                paypal_order_id="ORDER-1",
                status="CREATED",
                amount="1.00",
                currency="USD",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        session.commit()
    _configure_brief_order(user_id, article.id, amount="2.00")

    response = client.get("/brief/return?token=ORDER-1", follow_redirects=False)
    assert response.status_code == 400
    with session_factory() as session:
        assert session.scalar(select(Brief)) is None
        assert session.scalar(select(PaypalOrder)).status == "CREATED"


def test_paypal_api_errors_do_not_mark_subscription_active(paypal_app, monkeypatch):
    client, session_factory = paypal_app
    _signup(client, "inactive@example.com")
    client.post("/subscribe", follow_redirects=False)
    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == "inactive@example.com"))
    FakePayPal.subscriptions["I-SUBSCRIPTION-1"] = {
        "id": "I-SUBSCRIPTION-1",
        "status": "APPROVAL_PENDING",
        "custom_id": str(user.id),
    }
    response = client.get("/subscribe/return?subscription_id=I-SUBSCRIPTION-1", follow_redirects=False)
    assert response.status_code == 303
    with session_factory() as session:
        assert session.get(User, user.id).is_pro is False


def test_subscription_authentication_failure_returns_gateway_error_not_server_crash(
    paypal_app, monkeypatch
):
    client, _ = paypal_app
    _signup(client, "paypal-auth-error@example.com")

    def reject_credentials(self, *_args, **_kwargs):
        raise PayPalRequestFailed(
            "PayPal authentication failed; check the configured credentials"
        )

    monkeypatch.setattr(FakePayPal, "create_subscription", reject_credentials)
    response = client.post("/subscribe")
    assert response.status_code == 502
    assert response.json()["detail"] == (
        "PayPal authentication failed; check the configured credentials"
    )


def test_duplicate_capture_id_is_only_recorded_once(paypal_app):
    client, session_factory = paypal_app
    first = {
        "id": "WH-CAPTURE-1",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "CAPTURE-SHARED", "status": "COMPLETED"},
    }
    second = {
        "id": "WH-CAPTURE-2",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "CAPTURE-SHARED", "status": "COMPLETED"},
    }
    assert client.post("/webhooks/paypal", json=first).status_code == 200
    assert client.post("/webhooks/paypal", json=second).status_code == 200
    with session_factory() as session:
        assert len(list(session.scalars(select(ProcessedPaymentCapture)))) == 1


def test_capture_webhook_fulfills_matching_brief_order_once(paypal_app):
    client, session_factory = paypal_app
    article = _add_old_article(session_factory)
    _signup(client, "webhook-buyer@example.com")
    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == "webhook-buyer@example.com"))
        user_id = user.id
        session.add(
            PaypalOrder(
                user_id=user_id,
                article_id=article.id,
                paypal_order_id="ORDER-WEBHOOK",
                status="CREATED",
                amount="1.00",
                currency="USD",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        session.commit()
    event = {
        "id": "WH-ORDER-CAPTURE",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAPTURE-WEBHOOK",
            "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": "1.00"},
            "supplementary_data": {"related_ids": {"order_id": "ORDER-WEBHOOK"}},
        },
    }

    assert client.post("/webhooks/paypal", json=event).json() == {"status": "accepted"}
    assert client.post("/webhooks/paypal", json=event).json() == {"status": "duplicate"}
    with session_factory() as session:
        assert len(list(session.scalars(select(Brief)))) == 1
        assert session.scalar(select(PaypalOrder)).status == "COMPLETED"
        assert session.get(ProcessedPaymentCapture, "CAPTURE-WEBHOOK") is not None


def test_amount_match_uses_decimal_not_float():
    from app.services.payments import amount_matches

    assert amount_matches("1.00", "USD", Decimal("1.00"), "USD")
    assert not amount_matches("1.01", "USD", Decimal("1.00"), "USD")
    assert not amount_matches("1.00", "CAD", Decimal("1.00"), "USD")

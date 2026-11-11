from app.config import Settings
from app.paypal import PayPalClient


class StubResponse:
    def __init__(self, value):
        self.status_code = 200
        self.content = b"present"
        self.value = value

    def json(self):
        return self.value


def _client() -> PayPalClient:
    return PayPalClient(
        Settings(
            paypal_mode="sandbox",
            paypal_client_id="local-client",
            paypal_client_secret="local-secret",
        )
    )


def test_subscription_api_uses_configured_plan_custom_id_and_return_urls(monkeypatch):
    requests = []
    monkeypatch.setattr(
        "app.paypal.httpx.post",
        lambda *args, **kwargs: StubResponse({"access_token": "test-token"}),
    )

    def fake_request(method, url, **kwargs):
        requests.append((method, url, kwargs))
        return StubResponse({"id": "I-SUB-1"})

    monkeypatch.setattr("app.paypal.httpx.request", fake_request)
    result = _client().create_subscription(
        "P-MONTHLY",
        42,
        "http://192.168.1.20:8000/subscribe/return",
        "http://192.168.1.20:8000/subscribe/cancel",
    )

    assert result["id"] == "I-SUB-1"
    method, url, kwargs = requests[0]
    assert method == "POST"
    assert url == "https://api-m.sandbox.paypal.com/v1/billing/subscriptions"
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert kwargs["json"]["plan_id"] == "P-MONTHLY"
    assert kwargs["json"]["custom_id"] == "42"
    assert kwargs["json"]["application_context"]["return_url"].endswith("/subscribe/return")


def test_order_api_uses_capture_intent_one_dollar_and_account_article_custom_id(monkeypatch):
    requests = []
    monkeypatch.setattr(
        "app.paypal.httpx.post",
        lambda *args, **kwargs: StubResponse({"access_token": "test-token"}),
    )

    def fake_request(method, url, **kwargs):
        requests.append((method, url, kwargs))
        return StubResponse({"id": "ORDER-1"})

    monkeypatch.setattr("app.paypal.httpx.request", fake_request)
    result = _client().create_order(
        42,
        7,
        "Quarterly earnings update",
        "https://marketpulse.example/brief/return",
        "https://marketpulse.example/brief/cancel",
    )

    assert result["id"] == "ORDER-1"
    method, url, kwargs = requests[0]
    assert method == "POST"
    assert url == "https://api-m.sandbox.paypal.com/v2/checkout/orders"
    purchase_unit = kwargs["json"]["purchase_units"][0]
    assert kwargs["json"]["intent"] == "CAPTURE"
    assert purchase_unit["custom_id"] == "42:7"
    assert purchase_unit["amount"] == {"currency_code": "USD", "value": "1.00"}


def test_webhook_signature_request_includes_configured_webhook_id_and_event(monkeypatch):
    requests = []
    monkeypatch.setattr(
        "app.paypal.httpx.post",
        lambda *args, **kwargs: StubResponse({"access_token": "test-token"}),
    )

    def fake_request(method, url, **kwargs):
        requests.append((method, url, kwargs))
        return StubResponse({"verification_status": "SUCCESS"})

    monkeypatch.setattr("app.paypal.httpx.request", fake_request)
    event = {"id": "WH-EVENT-1", "event_type": "BILLING.SUBSCRIPTION.ACTIVATED"}
    headers = {
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-cert-url": "https://api.paypal.com/cert",
        "paypal-transmission-id": "transmission",
        "paypal-transmission-sig": "signature",
        "paypal-transmission-time": "2026-01-01T00:00:00Z",
    }

    assert _client().verify_webhook_signature(headers, event, "WH-LOCAL") is True
    _, url, kwargs = requests[0]
    assert url.endswith("/v1/notifications/verify-webhook-signature")
    assert kwargs["json"]["webhook_id"] == "WH-LOCAL"
    assert kwargs["json"]["webhook_event"] == event

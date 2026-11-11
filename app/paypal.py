import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class PayPalError(Exception):
    pass


class PayPalNotConfigured(PayPalError):
    pass


class PayPalUnavailable(PayPalError):
    pass


class PayPalRequestFailed(PayPalError):
    pass


class PayPalClient:
    def __init__(self, settings: Settings):
        mode = settings.paypal_mode.strip().lower()
        if mode not in {"sandbox", "live"}:
            raise PayPalNotConfigured("PAYPAL_MODE must be sandbox or live")
        self.base_url = (
            "https://api-m.sandbox.paypal.com"
            if mode == "sandbox"
            else "https://api-m.paypal.com"
        )
        self.client_id = settings.paypal_client_id.get_secret_value().strip()
        self.client_secret = settings.paypal_client_secret.get_secret_value().strip()
        if not self.client_id or not self.client_secret:
            raise PayPalNotConfigured("PayPal client credentials are not configured")

    def _access_token(self) -> str:
        try:
            response = httpx.post(
                f"{self.base_url}/v1/oauth2/token",
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
                timeout=20,
            )
        except httpx.HTTPError as exc:
            raise PayPalUnavailable("PayPal is temporarily unavailable") from exc
        if response.status_code >= 500:
            raise PayPalUnavailable("PayPal is temporarily unavailable")
        if response.status_code >= 400:
            raise PayPalRequestFailed("PayPal authentication failed; check the configured credentials")
        try:
            token = response.json().get("access_token")
        except (ValueError, AttributeError) as exc:
            raise PayPalUnavailable("PayPal returned an invalid authentication response") from exc
        if not isinstance(token, str) or not token:
            raise PayPalUnavailable("PayPal returned no access token")
        return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        token = self._access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        headers.update(extra_headers or {})
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                json=payload,
                headers=headers,
                timeout=25,
            )
        except httpx.HTTPError as exc:
            raise PayPalUnavailable("PayPal is temporarily unavailable") from exc
        if response.status_code >= 500:
            raise PayPalUnavailable("PayPal is temporarily unavailable")
        if response.status_code >= 400:
            logger.warning("PayPal API request failed with HTTP %s", response.status_code)
            raise PayPalRequestFailed("PayPal could not complete the request")
        if response.status_code == 204 or not response.content:
            return {}
        try:
            result = response.json()
        except ValueError as exc:
            raise PayPalUnavailable("PayPal returned an invalid response") from exc
        if not isinstance(result, dict):
            raise PayPalUnavailable("PayPal returned an invalid response")
        return result

    def create_subscription(
        self,
        plan_id: str,
        user_id: int,
        return_url: str,
        cancel_url: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/billing/subscriptions",
            payload={
                "plan_id": plan_id,
                "custom_id": str(user_id),
                "application_context": {
                    "brand_name": "MarketPulse AI",
                    "user_action": "SUBSCRIBE_NOW",
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                },
            },
        )

    def create_product(self) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/catalogs/products",
            payload={
                "name": "MarketPulse AI Pro",
                "description": "Instant access to MarketPulse AI classified news",
                "type": "SERVICE",
                "category": "SOFTWARE",
            },
        )

    def create_monthly_plan(self, product_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/billing/plans",
            payload={
                "product_id": product_id,
                "name": "MarketPulse AI Pro — Monthly",
                "description": "MarketPulse AI Pro, billed monthly",
                "status": "ACTIVE",
                "billing_cycles": [
                    {
                        "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                        "tenure_type": "REGULAR",
                        "sequence": 1,
                        "total_cycles": 0,
                        "pricing_scheme": {
                            "fixed_price": {"value": "5.00", "currency_code": "USD"}
                        },
                    }
                ],
                "payment_preferences": {
                    "auto_bill_outstanding": True,
                    "setup_fee_failure_action": "CONTINUE",
                    "payment_failure_threshold": 3,
                },
            },
        )

    def create_webhook(self, webhook_url: str, event_types: list[str]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/notifications/webhooks",
            payload={
                "url": webhook_url,
                "event_types": [{"name": event_type} for event_type in event_types],
            },
        )

    def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/billing/subscriptions/{subscription_id}")

    def cancel_subscription(self, subscription_id: str, reason: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/billing/subscriptions/{subscription_id}/cancel",
            payload={"reason": reason},
        )

    def create_order(
        self,
        user_id: int,
        article_id: int,
        title: str,
        return_url: str,
        cancel_url: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v2/checkout/orders",
            payload={
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "custom_id": f"{user_id}:{article_id}",
                        "description": f"MarketPulse Deep Brief: {title[:90]}",
                        "amount": {"currency_code": "USD", "value": "1.00"},
                    }
                ],
                "application_context": {
                    "brand_name": "MarketPulse AI",
                    "user_action": "PAY_NOW",
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                },
            },
        )

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def capture_order(self, order_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v2/checkout/orders/{order_id}/capture", payload={})

    def verify_webhook_signature(
        self,
        headers: dict[str, str],
        event: dict[str, Any],
        webhook_id: str,
    ) -> bool:
        if not webhook_id:
            raise PayPalNotConfigured("PAYPAL_WEBHOOK_ID is not configured")
        payload = {
            "auth_algo": headers.get("paypal-auth-algo"),
            "cert_url": headers.get("paypal-cert-url"),
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        if any(not payload[key] for key in (
            "auth_algo",
            "cert_url",
            "transmission_id",
            "transmission_sig",
            "transmission_time",
        )):
            return False
        result = self._request(
            "POST",
            "/v1/notifications/verify-webhook-signature",
            payload=payload,
        )
        return result.get("verification_status") == "SUCCESS"

import sys
from urllib.parse import urlparse

from app.config import Settings
from app.paypal import PayPalClient, PayPalError

WEBHOOK_EVENTS = [
    "BILLING.SUBSCRIPTION.ACTIVATED",
    "BILLING.SUBSCRIPTION.CANCELLED",
    "BILLING.SUBSCRIPTION.SUSPENDED",
    "BILLING.SUBSCRIPTION.EXPIRED",
    "PAYMENT.CAPTURE.COMPLETED",
]


def main() -> int:
    settings = Settings()
    base_url = settings.base_url.rstrip("/")
    if urlparse(base_url).scheme != "https" or not urlparse(base_url).hostname:
        print("BASE_URL must be a public https URL before registering the PayPal webhook.", file=sys.stderr)
        return 1
    try:
        paypal = PayPalClient(settings)
        product = paypal.create_product()
        product_id = product.get("id")
        if not isinstance(product_id, str) or not product_id:
            raise PayPalError("PayPal did not return a product ID")
        plan = paypal.create_monthly_plan(product_id)
        plan_id = plan.get("id")
        if not isinstance(plan_id, str) or not plan_id:
            raise PayPalError("PayPal did not return a plan ID")
        webhook = paypal.create_webhook(f"{base_url}/webhooks/paypal", WEBHOOK_EVENTS)
        webhook_id = webhook.get("id")
        if not isinstance(webhook_id, str) or not webhook_id:
            raise PayPalError("PayPal did not return a webhook ID")
    except PayPalError as exc:
        print(f"PayPal setup failed: {exc}", file=sys.stderr)
        return 1

    print(f"PAYPAL_PLAN_ID={plan_id}")
    print(f"PAYPAL_WEBHOOK_ID={webhook_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

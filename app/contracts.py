from pydantic import BaseModel, ConfigDict, Field


class PayPalAmount(BaseModel):
    model_config = ConfigDict(extra="allow")

    value: str | None = None
    currency_code: str | None = None


class PayPalRelatedIds(BaseModel):
    model_config = ConfigDict(extra="allow")

    order_id: str | None = None


class PayPalSupplementaryData(BaseModel):
    model_config = ConfigDict(extra="allow")

    related_ids: PayPalRelatedIds | None = None


class PayPalWebhookResource(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    custom_id: str | None = None
    status: str | None = None
    amount: PayPalAmount | None = None
    supplementary_data: PayPalSupplementaryData | None = None


class PayPalWebhookEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    event_type: str
    resource: PayPalWebhookResource = Field(default_factory=PayPalWebhookResource)

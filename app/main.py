import asyncio
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings
from app.database import Base, create_database, session_dependency
from app.news import parse_feed_urls, run_news_poller
from app.paypal import PayPalClient
from app.routes.auth import create_auth_router
from app.routes.briefs import create_brief_router
from app.routes.pages import create_pages_router
from app.routes.subscriptions import create_subscription_router
from app.routes.webhooks import create_webhook_router

APP_DIRECTORY = Path(__file__).resolve().parent


def _ensure_paypal_approval_base_url(engine: Engine) -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("paypal_subscriptions")}
    if "approval_base_url" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE paypal_subscriptions "
                    "ADD COLUMN approval_base_url VARCHAR(2048) NOT NULL DEFAULT ''"
                )
            )


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    app_settings = settings or Settings()
    engine, default_session_factory = create_database(app_settings.database_url)
    factory = session_factory or default_session_factory
    get_session = session_dependency(factory)
    templates = Jinja2Templates(directory=str(APP_DIRECTORY / "templates"))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        Base.metadata.create_all(engine)
        _ensure_paypal_approval_base_url(engine)
        poller: asyncio.Task[None] | None = None
        if feed_urls := parse_feed_urls(app_settings.news_feeds):
            poller = asyncio.create_task(
                run_news_poller(
                    factory,
                    feed_urls,
                    app_settings.groq_api_key.get_secret_value(),
                )
            )
        try:
            yield
        finally:
            if poller is not None:
                poller.cancel()
                with suppress(asyncio.CancelledError):
                    await poller
            engine.dispose()

    application = FastAPI(title="MarketPulse AI", lifespan=lifespan)
    application.state.settings = app_settings
    application.state.engine = engine
    application.state.session_factory = factory
    application.add_middleware(
        SessionMiddleware,
        secret_key=(app_settings.app_secret_key.get_secret_value() or secrets.token_urlsafe(32)),
        same_site="lax",
        https_only=app_settings.base_url.lower().startswith("https://"),
    )
    application.mount(
        "/static",
        StaticFiles(directory=str(APP_DIRECTORY / "static")),
        name="static",
    )
    application.include_router(create_pages_router(get_session, templates))
    application.include_router(create_auth_router(get_session, templates))
    application.include_router(create_subscription_router(get_session, app_settings, PayPalClient))
    application.include_router(
        create_brief_router(get_session, app_settings, templates, PayPalClient)
    )
    application.include_router(create_webhook_router(get_session, app_settings, PayPalClient))
    return application


app = create_app()

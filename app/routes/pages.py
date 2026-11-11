from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Article, Brief, PaypalSubscription
from app.news import CATEGORIES
from app.routes.types import SessionDependency
from app.services.common import (
    FREE_DELAY,
    article_json,
    current_user,
    take_flashes,
    time_ago,
    visible_articles,
)


def create_pages_router(
    get_session: SessionDependency,
    templates: Jinja2Templates,
) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def front_page(
        request: Request,
        session: Session = Depends(get_session),
        category: str = Query(default="all"),
    ) -> HTMLResponse:
        if category != "all" and category not in CATEGORIES:
            raise HTTPException(status_code=422, detail="Invalid news category.")

        user = current_user(request, session)
        visible, locked_count = visible_articles(session, user)
        if category != "all":
            all_category_articles = list(
                session.scalars(select(Article).where(Article.category == category))
            )
            visible_ids = {article.id for article in visible}
            visible = [article for article in visible if article.category == category]
            locked_count = (
                sum(article.id not in visible_ids for article in all_category_articles)
                if not (user and user.is_pro)
                else 0
            )

        locked_articles: list[dict[str, str]] = []
        if not (user and user.is_pro) and locked_count:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            recent = list(session.scalars(select(Article).order_by(Article.published_at.desc())))
            if category != "all":
                recent = [article for article in recent if article.category == category]
            locked_articles = [
                {"release_at": (article.published_at + FREE_DELAY).isoformat()}
                for article in recent
                if article.published_at + FREE_DELAY > now
            ]

        sentiments = [article.sentiment for article in visible]
        positive = sentiments.count("bullish")
        negative = sentiments.count("bearish")
        decided = positive + negative
        mood_percent = round(positive / decided * 100) if decided else 50
        mood = "bullish" if mood_percent >= 60 else "bearish" if mood_percent <= 40 else "neutral"
        articles = [
            {
                **article_json(article),
                "published_label": time_ago(article.published_at),
                "sentiment_marker": (
                    "▲" if article.sentiment == "bullish"
                    else "▼" if article.sentiment == "bearish"
                    else "•"
                ),
            }
            for article in visible
        ]
        brief_article_ids = (
            set(session.scalars(select(Brief.article_id).where(Brief.user_id == user.id)))
            if user
            else set()
        )
        user_briefs = (
            list(
                session.scalars(
                    select(Article)
                    .join(Brief, Brief.article_id == Article.id)
                    .where(Brief.user_id == user.id)
                    .order_by(Brief.created_at.desc())
                )
            )
            if user
            else []
        )
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "today": datetime.now().astimezone().strftime("%A, %B %d, %Y").replace(" 0", " "),
                "user": user,
                "articles": articles,
                "lead_article": articles[0] if articles else None,
                "more_articles": articles[1:],
                "locked_articles": locked_articles,
                "locked_count": locked_count,
                "categories": CATEGORIES,
                "selected_category": category,
                "mood": mood,
                "mood_percent": mood_percent,
                "flashes": take_flashes(request),
                "subscription": (
                    session.scalar(
                        select(PaypalSubscription)
                        .where(PaypalSubscription.user_id == user.id)
                        .order_by(PaypalSubscription.id.desc())
                    )
                    if user
                    else None
                ),
                "brief_article_ids": brief_article_ids,
                "user_briefs": user_briefs,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/api/articles")
    def articles_api(
        request: Request,
        session: Session = Depends(get_session),
        q: str = Query(default="", max_length=120),
        sentiment: str = Query(default="all"),
    ) -> JSONResponse:
        if sentiment not in {"all", "bullish", "bearish", "neutral"}:
            return JSONResponse({"detail": "Invalid sentiment filter."}, status_code=422)
        user = current_user(request, session)
        visible, locked_count = visible_articles(session, user)
        normalized_query = q.strip().casefold()
        if sentiment != "all":
            visible = [article for article in visible if article.sentiment == sentiment]
        if normalized_query:
            visible = [
                article
                for article in visible
                if normalized_query
                in " ".join(
                    (article.title, article.summary, article.source, article.category, article.insight)
                ).casefold()
            ]
        return JSONResponse(
            {
                "articles": [article_json(article) for article in visible],
                "locked_count": locked_count,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return router

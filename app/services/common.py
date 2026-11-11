from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Article, PaypalSubscription, User

FREE_DELAY = timedelta(minutes=30)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def flash(request: Request, message: str, kind: str = "info") -> None:
    messages = request.session.setdefault("_flashes", [])
    messages.append({"message": message, "kind": kind})


def take_flashes(request: Request) -> list[dict[str, str]]:
    return request.session.pop("_flashes", [])


def current_user(request: Request, session: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not isinstance(user_id, int):
        return None
    return session.get(User, user_id)


def visible_articles(session: Session, user: User | None) -> tuple[list[Article], int]:
    articles = list(session.scalars(select(Article).order_by(Article.published_at.desc())))
    if user is not None and user.is_pro:
        return articles, 0
    cutoff = utcnow() - FREE_DELAY
    visible = [article for article in articles if article.published_at <= cutoff]
    return visible, len(articles) - len(visible)


def article_json(article: Article) -> dict[str, object]:
    return {
        "id": article.id,
        "title": article.title,
        "summary": article.summary,
        "link": article.link,
        "source": article.source,
        "category": article.category,
        "sentiment": article.sentiment,
        "insight": article.insight,
        "published_at": article.published_at.isoformat(),
    }


def article_is_unlocked(article: Article, user: User) -> bool:
    return user.is_pro or article.published_at <= utcnow() - FREE_DELAY


def time_ago(published_at: datetime) -> str:
    elapsed = max(0, int((utcnow() - published_at).total_seconds() // 60))
    if elapsed < 60:
        return f"{elapsed} min ago"
    hours, minutes = divmod(elapsed, 60)
    if hours < 24:
        return f"{hours} hr ago" if minutes < 10 else f"{hours} hr {minutes} min ago"
    days, hours = divmod(hours, 24)
    return f"{days} day ago" if hours == 0 else f"{days} day {hours} hr ago"


def refresh_pro_status(session: Session, user: User) -> None:
    session.flush()
    active = session.scalar(
        select(PaypalSubscription.id)
        .where(
            PaypalSubscription.user_id == user.id,
            PaypalSubscription.status == "ACTIVE",
        )
        .limit(1)
    )
    user.is_pro = active is not None

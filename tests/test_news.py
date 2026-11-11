from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from app.database import Base, create_database
from app.models import Article
from app.news import CATEGORIES, keyword_classification, poll_news_once
from sqlalchemy import select


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    def __init__(self, feeds: dict[str, bytes]):
        self.feeds = feeds

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, url: str) -> FakeResponse:
        if url == "https://feeds.example/broken":
            raise ValueError("feed is unavailable")
        return FakeResponse(self.feeds[url])


@pytest.fixture
def news_database():
    engine, session_factory = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield session_factory
    engine.dispose()


def _rss(stories: list[tuple[str, str, datetime]]) -> bytes:
    items = "".join(
        (
            "<item>"
            f"<title>{title}</title><link>{link}</link>"
            f"<description><![CDATA[<p>{title} description</p>]]></description>"
            f"<pubDate>{format_datetime(published_at)}</pubDate>"
            "</item>"
        )
        for title, link, published_at in stories
    )
    return f"<rss><channel><title>Example Markets</title>{items}</channel></rss>".encode()


def test_polls_newest_first_deduplicates_and_limits_to_twelve(news_database, monkeypatch, caplog):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stories = [
        (f"Market story {index}", f"https://news.example/{index}", now - timedelta(minutes=index))
        for index in range(13, 0, -1)
    ]
    feed_bytes = _rss(stories)
    monkeypatch.setattr(
        "app.news.httpx.Client",
        lambda **_kwargs: FakeClient({"https://feeds.example/markets": feed_bytes}),
    )
    def importer(batch, _key):
        return [
            {
                "category": "Markets",
                "sentiment": "neutral",
                "insight": "This market report adds context.",
            }
            for _story in batch
        ]

    imported = poll_news_once(
        news_database,
        ["https://feeds.example/broken", "https://feeds.example/markets"],
        "test-key",
        classifier=importer,
    )
    with news_database() as session:
        articles = list(session.scalars(select(Article).order_by(Article.published_at.desc())))

    assert imported == 12
    assert len(articles) == 12
    assert articles[0].title == "Market story 1"
    assert articles[-1].title == "Market story 12"
    assert all(not article.is_demo for article in articles)
    assert articles[0].source == "Example Markets"
    assert articles[0].summary == "Market story 1 description"
    assert "Unable to fetch news feed at feeds.example" in caplog.text
    assert poll_news_once(
        news_database,
        ["https://feeds.example/markets"],
        "test-key",
        classifier=importer,
    ) == 1
    with news_database() as session:
        remaining_article = session.scalar(select(Article).where(Article.title == "Market story 13"))
    assert remaining_article is not None
    assert poll_news_once(
        news_database,
        ["https://feeds.example/markets"],
        "test-key",
        classifier=importer,
    ) == 0


@pytest.mark.parametrize(
    ("title", "expected_category", "expected_sentiment"),
    [
        ("RBI holds repo rate in policy review", "RBI & Policy", "neutral"),
        ("Nifty shares surge in market rally", "Markets", "bullish"),
        ("Oil prices drop after supply concerns", "Commodities & Forex", "bearish"),
        ("Company announces quarterly revenue", "Earnings", "neutral"),
        ("Company launches IPO listing", "IPO & Listings", "neutral"),
        ("Company agrees to acquisition deal", "Mergers & Deals", "neutral"),
        ("Asian market rises on global cues", "Global Cues", "bullish"),
        ("A new report is published", "Other", "neutral"),
    ],
)
def test_keyword_classifier_returns_fixed_categories_and_sentiment(
    title, expected_category, expected_sentiment
):
    result = keyword_classification(title, "")
    assert result["category"] == expected_category
    assert result["category"] in CATEGORIES
    assert result["sentiment"] == expected_sentiment
    assert result["insight"].endswith(".")


def test_poll_uses_keyword_fallback_if_ai_classification_fails(news_database, monkeypatch, caplog):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "app.news.httpx.Client",
        lambda **_kwargs: FakeClient(
            {
                "https://feeds.example/markets": _rss(
                    [("Nifty shares rally", "https://news.example/rally", now)]
                )
            }
        ),
    )

    def failed_classifier(_batch, _key):
        raise RuntimeError("AI temporarily unavailable")

    assert poll_news_once(
        news_database,
        ["https://feeds.example/markets"],
        "test-key",
        classifier=failed_classifier,
    ) == 1
    with news_database() as session:
        article = session.scalar(select(Article).where(Article.link == "https://news.example/rally"))
    assert article is not None
    assert article.category == "Markets"
    assert article.sentiment == "bullish"
    assert "AI classification failed" in caplog.text


def test_poll_without_groq_key_uses_keyword_classifier(news_database, monkeypatch):
    monkeypatch.setattr(
        "app.news.httpx.Client",
        lambda **_kwargs: FakeClient(
            {
                "https://feeds.example/markets": _rss(
                    [
                        (
                            "RBI announces policy decision",
                            "https://news.example/rbi",
                            datetime.now(timezone.utc),
                        )
                    ]
                )
            }
        ),
    )
    assert poll_news_once(news_database, ["https://feeds.example/markets"]) == 1
    with news_database() as session:
        article = session.scalar(select(Article).where(Article.link == "https://news.example/rbi"))
    assert article is not None
    assert article.category == "RBI & Policy"

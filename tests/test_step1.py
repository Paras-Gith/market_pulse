from datetime import datetime, timedelta, timezone

import pytest
from app.config import Settings
from app.database import Base, create_database
from app.main import create_app
from app.models import Article, User
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    engine, session_factory = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    app = create_app(
        Settings(
            database_url="sqlite:///:memory:",
            app_secret_key="test-secret",
            base_url="http://testserver",
            news_feeds="",
        ),
        session_factory=session_factory,
    )
    with session_factory() as session:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        session.add_all(
            [
                Article(
                    title="Hidden market headline",
                    summary="Hidden market summary",
                    link="https://example.test/hidden",
                    source="Test",
                    category="Markets",
                    sentiment="bullish",
                    insight="Hidden market insight",
                    published_at=now - timedelta(minutes=5),
                    is_demo=False,
                ),
                Article(
                    title="Visible market headline",
                    summary="Visible market summary",
                    link="https://example.test/visible",
                    source="Test",
                    category="Earnings",
                    sentiment="neutral",
                    insight="Visible market insight",
                    published_at=now - timedelta(minutes=45),
                    is_demo=False,
                ),
            ]
        )
        session.commit()
    with TestClient(app) as test_client:
        yield test_client, session_factory
    engine.dispose()


def test_free_user_does_not_receive_locked_story_content(client):
    test_client, _ = client
    page = test_client.get("/")
    api = test_client.get("/api/articles", params={"q": "Visible market headline"})

    assert "Hidden market headline" not in page.text
    assert "Hidden market summary" not in page.text
    assert "Hidden market insight" not in page.text
    assert "Visible market headline" in page.text
    assert page.text.count("A market story is developing") >= 1
    assert "Hidden market headline" not in api.text
    assert "Hidden market summary" not in api.text
    assert api.json()["locked_count"] >= 1
    assert [article["title"] for article in api.json()["articles"]] == ["Visible market headline"]
    assert api.headers["cache-control"] == "private, no-store"
    assert page.headers["cache-control"] == "private, no-store"


def test_pro_user_sees_locked_stories_immediately(client):
    test_client, session_factory = client
    test_client.post(
        "/signup",
        data={"email": "pro@example.com", "password": "a-secure-password"},
        follow_redirects=False,
    )
    with session_factory() as session:
        user = session.query(User).filter_by(email="pro@example.com").one()
        user.is_pro = True
        session.commit()

    page = test_client.get("/")
    api = test_client.get("/api/articles", params={"q": "Hidden market headline"})
    assert "Hidden market headline" in page.text
    assert [article["title"] for article in api.json()["articles"]] == ["Hidden market headline"]
    assert api.json()["locked_count"] == 0


def test_signup_login_and_logout(client):
    test_client, session_factory = client
    response = test_client.post(
        "/signup",
        data={"email": "Reader@Example.com", "password": "a-secure-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert test_client.get("/").status_code == 200
    with session_factory() as session:
        user = session.query(User).filter_by(email="reader@example.com").one()
        assert user.is_pro is False
        stored_hash = user.password_hash

    test_client.post("/logout", follow_redirects=False)
    response = test_client.post(
        "/login",
        data={"email": "reader@example.com", "password": "a-secure-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert test_client.get("/").status_code == 200
    assert stored_hash != b"a-secure-password"
    test_client.post("/logout", follow_redirects=False)
    assert "reader@example.com" not in test_client.get("/").text


@pytest.mark.parametrize("password", ["short", "x" * 65])
def test_signup_rejects_password_outside_allowed_length(client, password):
    test_client, _ = client
    response = test_client.post(
        "/signup",
        data={"email": "reader@example.com", "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert test_client.get("/signup").text.count("Password must be between 8 and 64 characters.") == 1


def test_health_endpoint(client):
    test_client, _ = client
    assert test_client.get("/healthz").json() == {"status": "ok"}


def test_secret_settings_are_redacted_from_repr():
    settings = Settings(
        app_secret_key="app-secret-for-test",
        groq_api_key="groq-secret-for-test",
        paypal_client_id="paypal-id-for-test",
        paypal_client_secret="paypal-secret-for-test",
    )

    representation = repr(settings)
    assert "app-secret-for-test" not in representation
    assert "groq-secret-for-test" not in representation
    assert "paypal-id-for-test" not in representation
    assert "paypal-secret-for-test" not in representation


def test_category_filter_promotes_newest_matching_story_to_lead(client):
    test_client, _ = client
    response = test_client.get("/", params={"category": "Earnings"})

    assert response.status_code == 200
    assert 'href="/?category=Earnings" aria-current="page"' in response.text
    assert '<h2><a href="https://example.test/visible"' in response.text
    assert "Visible market headline" in response.text
    assert "Hidden market headline" not in response.text


def test_stories_render_once_in_full_width_news_feed(client):
    test_client, session_factory = client
    test_client.post(
        "/signup",
        data={"email": "reader@example.com", "password": "a-secure-password"},
        follow_redirects=False,
    )
    with session_factory() as session:
        user = session.query(User).filter_by(email="reader@example.com").one()
        user.is_pro = True
        session.commit()

    response = test_client.get("/")

    assert response.status_code == 200
    assert '<section class="news-feed" aria-labelledby="latest-title">' in response.text
    assert response.text.count('class="lead-story story-item"') == 1
    assert response.text.count('class="more-story story-card story-item"') == 1
    assert "Hidden market headline" in response.text
    assert "Visible market headline" in response.text


def test_invalid_category_is_rejected(client):
    test_client, _ = client
    assert test_client.get("/", params={"category": "Not a category"}).status_code == 422


def test_signup_accepts_a_64_character_unicode_password(client):
    test_client, _ = client
    response = test_client.post(
        "/signup",
        data={"email": "unicode@example.com", "password": "🙂" * 64},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_startup_removes_existing_demo_stories(client):
    test_client, session_factory = client
    with session_factory() as session:
        session.add(
            Article(
                title="Legacy seeded demo story",
                summary="Old demo",
                link="https://marketpulse.example/demo/legacy",
                source="MarketPulse demo",
                category="Other",
                sentiment="neutral",
                insight="This was a demo story.",
                published_at=datetime.now(timezone.utc).replace(tzinfo=None),
                is_demo=True,
            )
        )
        session.commit()

    with TestClient(test_client.app) as restarted_client:
        assert "Legacy seeded demo story" not in restarted_client.get("/").text

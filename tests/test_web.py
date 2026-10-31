"""Webhook mode (Vercel): HTTP routes, secrets, database URLs, polling guard."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from coach import jobs, main, web
from coach.bot import build_application
from coach.config import get_settings
from coach.db import Database, normalize_database_url

ROOT = Path(__file__).resolve().parent.parent
WEBHOOK_SECRET = "webhook-secret"
CRON_SECRET = "cron-secret"


@pytest.fixture
def prod_settings(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("CRON_SECRET", CRON_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeBot:
    def __init__(self):
        self.webhooks: list[tuple[str, dict]] = []

    async def set_webhook(self, url, **kwargs):
        self.webhooks.append((url, kwargs))


@pytest.fixture
def fake_app(monkeypatch):
    processed = []

    async def process_update(update):
        processed.append(update)

    application = SimpleNamespace(bot=FakeBot(), bot_data={}, process_update=process_update,
                                  processed=processed)

    async def get_application():
        return application

    monkeypatch.setattr(web, "get_application", get_application)
    return application


UPDATE = {"update_id": 1, "message": {"message_id": 1, "date": 0, "text": "bonjour",
                                      "chat": {"id": 42, "type": "private"}}}


async def test_telegram_update_requires_the_webhook_secret(prod_settings, fake_app):
    body = json.dumps(UPDATE).encode()
    status, _ = await web.handle("POST", "/api/telegram", {}, body)
    assert status == 401
    status, _ = await web.handle("POST", "/api/telegram",
                                 {"x-telegram-bot-api-secret-token": "wrong"}, body)
    assert status == 401
    assert fake_app.processed == []

    status, _ = await web.handle("POST", "/api/telegram",
                                 {"x-telegram-bot-api-secret-token": WEBHOOK_SECRET}, body)
    assert status == 200
    assert fake_app.processed[0].update_id == 1


async def test_webhook_refused_when_no_secret_is_configured(monkeypatch, fake_app):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    try:
        status, _ = await web.handle("POST", "/api/telegram",
                                     {"x-telegram-bot-api-secret-token": ""}, b"{}")
    finally:
        get_settings.cache_clear()
    assert status == 401


async def test_invalid_json_is_rejected(prod_settings, fake_app):
    status, _ = await web.handle("POST", "/api/telegram",
                                 {"x-telegram-bot-api-secret-token": WEBHOOK_SECRET}, b"not json")
    assert status == 400


async def test_tick_requires_the_cron_secret_and_runs_the_scheduler(prod_settings, fake_app,
                                                                    monkeypatch):
    contexts = []

    async def fake_tick(context):
        contexts.append(context)

    monkeypatch.setattr(jobs, "tick", fake_tick)
    assert (await web.handle("GET", "/api/tick", {}, b""))[0] == 401
    assert (await web.handle("GET", "/api/tick", {"authorization": "Bearer wrong"}, b""))[0] == 401
    assert contexts == []

    status, _ = await web.handle("GET", "/api/tick", {"authorization": f"Bearer {CRON_SECRET}"}, b"")
    assert status == 200
    assert contexts[0].application is fake_app


async def test_set_webhook_points_telegram_at_the_production_url(prod_settings, fake_app,
                                                                 monkeypatch):
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "coach.vercel.app")
    status, payload = await web.handle("GET", "/api/set-webhook",
                                       {"authorization": f"Bearer {CRON_SECRET}"}, b"")
    assert status == 200
    url, options = fake_app.bot.webhooks[0]
    assert url == payload["url"] == "https://coach.vercel.app/api/telegram"
    assert options["secret_token"] == WEBHOOK_SECRET
    assert options["max_connections"] == 1


async def test_asgi_app_answers_json(prod_settings, fake_app):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await web.app({"type": "http", "method": "GET", "path": "/api/unknown", "headers": []},
                  receive, send)
    assert sent[0]["status"] == 404
    assert json.loads(sent[1]["body"]) == {"error": "not found"}

    # Behind the vercel.json rewrite, the route arrives as a query parameter.
    sent.clear()
    await web.app({"type": "http", "method": "GET", "path": "/api/index",
                   "query_string": b"route=tick", "headers": []}, receive, send)
    assert sent[0]["status"] == 401


@pytest.mark.parametrize(("url", "expected"), [
    ("postgres://u:p@ep-x.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
     "postgresql+asyncpg://u:p@ep-x.eu-central-1.aws.neon.tech/neondb?ssl=require"),
    ("postgresql://u:p@db:5432/coach", "postgresql+asyncpg://u:p@db:5432/coach"),
    ("postgresql+asyncpg://u:p@db:5432/coach", "postgresql+asyncpg://u:p@db:5432/coach"),
    ("sqlite+aiosqlite:///./coach.db", "sqlite+aiosqlite:///./coach.db"),
])
def test_hosted_database_urls_are_normalized(url, expected):
    assert normalize_database_url(url) == expected


async def test_serverless_database_works(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'web.db'}", serverless=True)
    await db.create_all()
    await db.dispose()


@pytest.mark.parametrize("schedule_jobs", [True, False])
def test_internal_scheduler_only_in_polling_mode(prod_settings, schedule_jobs):
    application = build_application(SimpleNamespace(), SimpleNamespace(), schedule_jobs=schedule_jobs)
    assert bool(application.job_queue.jobs()) is schedule_jobs


def test_polling_is_refused_on_a_production_config(prod_settings):
    with pytest.raises(SystemExit, match="polling refused"):
        main.run()


def test_vercel_requirements_match_pyproject():
    def names(specs):
        return {re.split(r"[\[<>=]", s, maxsplit=1)[0].strip().lower() for s in specs}

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lines = (ROOT / "requirements.txt").read_text().splitlines()
    requirements = [x for x in lines if x.strip() and not x.startswith("#")]
    assert names(requirements) == names(pyproject["project"]["dependencies"])

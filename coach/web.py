"""HTTP entry point for serverless hosting (Vercel): Telegram webhook and minute cron.

Routes (plain ASGI, no web framework):
- POST /api/telegram     one Telegram update, authenticated by the webhook secret header;
- GET  /api/tick         runs the scheduler once (`jobs.tick`), called every minute by a cron;
- GET  /api/set-webhook  points the bot's webhook at this deployment.

`/api/tick` and `/api/set-webhook` require `Authorization: Bearer <CRON_SECRET>`.
Locally the bot keeps polling (`coach-bot`); this module only runs in production.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from typing import Any
from urllib.parse import parse_qs

from telegram import Update
from telegram.ext import Application, ContextTypes

from coach import jobs
from coach.agent import Agent
from coach.bot import build_application
from coach.config import get_settings
from coach.db import Database

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s : %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # keeps the bot token out of logged URLs
log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query"]

# One initialized application per event loop, so warm instances reuse it.
_state: dict[str, Any] = {}


async def get_application() -> Application:
    loop = asyncio.get_running_loop()
    if _state.get("loop") is not loop:
        db = Database(serverless=True)
        await db.create_all()  # MVP: create the schema; Alembic migrations still to add
        application = build_application(db, Agent(), schedule_jobs=False)
        await application.initialize()
        _state.update(loop=loop, app=application)
    return _state["app"]


def _secret_matches(expected: str, given: str | None) -> bool:
    return bool(expected) and given is not None and hmac.compare_digest(expected.encode(), given.encode())


def _bearer(header: str | None) -> str | None:
    if header and header.startswith("Bearer "):
        return header.removeprefix("Bearer ")
    return None


def webhook_url(headers: dict[str, str]) -> str:
    host = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or headers.get("host", "")
    return f"https://{host}/api/telegram"


async def handle(method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict]:
    settings = get_settings()
    route = path.rstrip("/").rsplit("/", 1)[-1]

    if route == "telegram" and method == "POST":
        given = headers.get("x-telegram-bot-api-secret-token")
        if not _secret_matches(settings.telegram_webhook_secret, given):
            return 401, {"error": "unauthorized"}
        try:
            data = json.loads(body)
        except ValueError:
            return 400, {"error": "invalid json"}
        application = await get_application()
        # Handler errors are logged by the application; answering 200 stops Telegram retrying.
        await application.process_update(Update.de_json(data, application.bot))
        return 200, {"ok": True}

    if route in ("tick", "set-webhook") and method == "GET":
        if not _secret_matches(settings.cron_secret, _bearer(headers.get("authorization"))):
            return 401, {"error": "unauthorized"}
        application = await get_application()
        if route == "tick":
            await jobs.tick(ContextTypes.DEFAULT_TYPE(application))
            return 200, {"ok": True}
        if not settings.telegram_webhook_secret:
            return 500, {"error": "TELEGRAM_WEBHOOK_SECRET is not set"}
        url = webhook_url(headers)
        # One connection at a time: updates are processed in order, as with polling.
        await application.bot.set_webhook(
            url, secret_token=settings.telegram_webhook_secret,
            allowed_updates=ALLOWED_UPDATES, max_connections=1,
        )
        log.info("Webhook set to %s", url)
        return 200, {"ok": True, "url": url}

    return 404, {"error": "not found"}


async def app(scope: dict, receive, send) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return

    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    # The vercel.json rewrite hands the original route over as ?route=..., since the function
    # itself only sees /api/index.
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    path = "/api/" + query["route"][0] if "route" in query else scope["path"]
    status, payload = await handle(scope["method"], path, headers, body)
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps(payload).encode()})

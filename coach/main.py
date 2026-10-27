"""Entry point: `coach-bot` or `python -m coach.main`."""

from __future__ import annotations

import logging

from coach.agent import Agent
from coach.bot import build_application
from coach.config import get_settings
from coach.db import Database
from coach.library import get_library


def run() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s : %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keeps the bot token out of logged URLs
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is missing (see .env.example).")

    lib = get_library()
    logging.info("Library loaded: %d exercises, %d recipes", len(lib.exercises), len(lib.recipes))

    db = Database()  # schema is created when the bot starts (post_init)
    app = build_application(db, Agent())
    logging.info("Bot started (polling), model %s", settings.llm_model)
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    run()

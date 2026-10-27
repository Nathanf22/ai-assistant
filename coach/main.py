"""Point d'entrée : `coach-bot` ou `python -m coach.main`."""

from __future__ import annotations

import logging

from coach.agent import Agent
from coach.bot import build_application
from coach.config import get_settings
from coach.db import Database
from coach.library import get_library


def run() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s : %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # n'affiche pas le jeton du bot dans les URL
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY manquant (voir .env.example).")

    lib = get_library()
    logging.info("Bibliothèque chargée : %d exercices, %d recettes", len(lib.exercises), len(lib.recipes))

    db = Database()  # schéma créé au démarrage du bot (post_init)
    app = build_application(db, Agent())
    logging.info("Bot démarré (polling), modèle %s", settings.llm_model)
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    run()

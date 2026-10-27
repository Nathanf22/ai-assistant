from __future__ import annotations

from datetime import date, datetime

import pytest

from coach import agent, jobs, services, timeutil
from coach.db import Database

# Lundi 2 novembre 2026, 09h00 heure de Paris : point fixe pour tous les tests.
FROZEN_DAY = date(2026, 11, 2)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    def fake_now():
        return datetime(2026, 11, 2, 9, 0, tzinfo=timeutil.tz())

    for module in (services, agent, timeutil):
        monkeypatch.setattr(module, "today", lambda: FROZEN_DAY, raising=False)
    for module in (services, agent, timeutil, jobs):
        monkeypatch.setattr(module, "now", fake_now, raising=False)
    import coach.bot as bot
    import coach.metrics as metrics

    monkeypatch.setattr(bot, "today", lambda: FROZEN_DAY)
    monkeypatch.setattr(metrics, "today", lambda: FROZEN_DAY)
    return FROZEN_DAY


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_all()
    yield database
    await database.dispose()


USUAL_SLOTS = [
    {"day": 0, "time": "18:30", "minutes": 30},
    {"day": 2, "time": "18:30", "minutes": 25},
    {"day": 4, "time": "12:30", "minutes": 20},
    {"day": 5, "time": "10:00", "minutes": 45},
]


@pytest.fixture
async def onboarded_user(db):
    async with db.session() as s:
        user = await services.get_or_create_user(s, 1001, "Léa")
        await services.record_consent(s, user)
        await services.update_profile(
            s, user, goal_text="3 séances par semaine et des dîners plus équilibrés",
            sessions_per_week=3, level="debutant", equipment=[], slots=USUAL_SLOTS,
            food_exclusions=["porc"], food_dislikes=["champignons"], dinners_per_week=5,
        )
        await services.finish_onboarding(s, user)
        uid = user.id
    return uid

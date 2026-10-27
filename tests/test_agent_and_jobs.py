"""Tests de l'agent (client Claude simulé) et du planificateur (bot Telegram simulé)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from coach import jobs, services, timeutil
from coach.agent import TOOLS, Agent
from coach.db import EventLog, User
from coach.metrics import compute, report

from .conftest import FROZEN_DAY, USUAL_SLOTS

# --- Client Claude simulé ---------------------------------------------------------------


@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)

    def model_dump(self, exclude_none: bool = True) -> dict:
        if self.type == "text":
            return {"type": "text", "text": self.text}
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


def response(*blocks: Block, stop: str | None = None):
    stop = stop or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=80,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage, model="test-model")


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script.pop(0)


class FakeClient:
    def __init__(self, script):
        self.messages = FakeMessages(script)


def tool(name, **inp):
    return Block("tool_use", id=f"tu_{name}", name=name, input=inp)


async def test_agent_onboarding_flow_uses_tools_and_creates_plan(db):
    client = FakeClient([
        response(Block("text", "Je note."), tool(
            "update_profile", goal_text="bouger 3 fois par semaine", sessions_per_week=3,
            level="debutant", slots=USUAL_SLOTS, dinners_per_week=4)),
        response(tool("update_preferences", add_exclusions=["porc"], add_dislikes=["champignons"])),
        response(tool("finish_onboarding")),
        response(Block("text", "C'est parti : ce soir, 30 minutes à 18h30.")),
    ])
    agent = Agent(client=client)
    async with db.session() as s:
        user = await services.get_or_create_user(s, 7, "Léa")
        await services.record_consent(s, user)
        answer = await agent.reply(s, user, "Je veux bouger 3 fois par semaine, lundi mercredi vendredi")

        assert answer.startswith("C'est parti")
        assert user.is_onboarded and user.food_exclusions == ["porc"]
        sessions = await services.sessions_between(s, user, FROZEN_DAY, FROZEN_DAY + timedelta(days=7))
        assert sessions

        # Le prompt système et les outils sont mis en cache, le contexte du profil non.
        first = client.messages.calls[0]
        assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in first["system"][1]
        assert first["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        # Le résultat d'outil est renvoyé au modèle.
        last = client.messages.calls[-1]["messages"][-1]
        assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"
        assert "session_id=" in last["content"][0]["content"]

        usage = (await s.scalars(select(EventLog).where(EventLog.kind == "llm_usage"))).all()
        assert len(usage) == 4


async def test_agent_reports_tool_errors_to_model(db, onboarded_user):
    client = FakeClient([
        response(tool("reschedule_session", session_id=999, action="cancel")),
        response(Block("text", "Je ne retrouve pas cette séance.")),
    ])
    async with db.session() as s:
        user = await s.get(User, onboarded_user)
        answer = await Agent(client=client).reply(s, user, "annule ma séance")
        result = client.messages.calls[-1]["messages"][-1]["content"][0]
        assert result["is_error"] is True and "introuvable" in result["content"]
        assert answer == "Je ne retrouve pas cette séance."


async def test_agent_quick_plan(db, onboarded_user):
    client = FakeClient([
        response(tool("quick_plan", minutes=20, ingredients=["œufs", "épinards", "feta", "pain"])),
        response(Block("text", "Voilà pour ce soir.")),
    ])
    async with db.session() as s:
        user = await s.get(User, onboarded_user)
        await services.create_week_plan(s, user, FROZEN_DAY)
        await Agent(client=client).reply(s, user, "ce soir j'ai 20 minutes et des œufs, épinards, feta")
        content = client.messages.calls[-1]["messages"][-1]["content"][0]["content"]
        assert "Omelette épinards et feta" in content and "Tu as tout ce qu'il faut" in content


def test_tool_schemas_are_well_formed():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names))
    for t in TOOLS:
        assert t["description"] and t["input_schema"]["type"] == "object"


# --- Planificateur ------------------------------------------------------------------------


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str, Any]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))


def at(day, hh, mm=0):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=timeutil.tz())


async def test_morning_brief_sent_once(db, onboarded_user):
    ctx = SimpleNamespace(bot=FakeBot())
    async with db.session() as s:
        user = await s.get(User, onboarded_user)
        assert await jobs.run_for_user(ctx, s, user, at(FROZEN_DAY, 7, 0)) == ["week_plan_created"]
        assert "brief" in await jobs.run_for_user(ctx, s, user, at(FROZEN_DAY, 7, 50))
        assert "brief" not in await jobs.run_for_user(ctx, s, user, at(FROZEN_DAY, 8, 30))
    briefs = [t for _, t, _ in ctx.bot.sent]
    assert len(briefs) == 1 and briefs[0].startswith("Bonjour Léa")
    assert len(briefs[0].splitlines()) <= 3


async def test_quiet_day_sends_nothing(db, onboarded_user):
    ctx = SimpleNamespace(bot=FakeBot())
    async with db.session() as s:
        user = await s.get(User, onboarded_user)
        user.quiet_dates = [FROZEN_DAY.isoformat()]
        await jobs.run_for_user(ctx, s, user, at(FROZEN_DAY, 7, 50))
        await jobs.run_for_user(ctx, s, user, at(FROZEN_DAY, 22, 0))
    assert ctx.bot.sent == []


async def test_feedback_requested_after_session(db, onboarded_user):
    ctx = SimpleNamespace(bot=FakeBot())
    async with db.session() as s:
        user = await s.get(User, onboarded_user)
        await services.create_week_plan(s, user, FROZEN_DAY)
        session = (await services.sessions_between(s, user, FROZEN_DAY, FROZEN_DAY + timedelta(days=1)))[0]
        end = timeutil.local_datetime(session.day, session.time) + timedelta(minutes=session.minutes)
        user.last_brief_on = FROZEN_DAY
        assert "feedback" not in await jobs.run_for_user(ctx, s, user, end)
        assert "feedback" in await jobs.run_for_user(ctx, s, user, end + timedelta(minutes=46))
        assert "feedback" not in await jobs.run_for_user(ctx, s, user, end + timedelta(minutes=90))
    assert sum("C'était comment" in t for _, t, _ in ctx.bot.sent) == 1


async def test_sunday_planning_creates_next_week(db, onboarded_user, monkeypatch):
    sunday = FROZEN_DAY + timedelta(days=6)
    monkeypatch.setattr(services, "today", lambda: sunday)
    ctx = SimpleNamespace(bot=FakeBot())
    async with db.session() as s:
        user = await s.get(User, onboarded_user)
        user.last_brief_on = sunday
        assert "planning" not in await jobs.run_for_user(ctx, s, user, at(sunday, 17, 0))
        assert "planning" in await jobs.run_for_user(ctx, s, user, at(sunday, 18, 5))
        assert "planning" not in await jobs.run_for_user(ctx, s, user, at(sunday, 19, 0))
        next_monday = sunday + timedelta(days=1)
        assert await services.get_week_plan(s, user, next_monday) is not None
    texts = [t for _, t, _ in ctx.bot.sent]
    assert any("semaine prochaine" in t for t in texts)


async def test_metrics_report_runs(db, onboarded_user):
    async with db.session() as s:
        user = await s.get(User, onboarded_user)
        await services.create_week_plan(s, user, FROZEN_DAY)
        text = report(await compute(s))
    assert "Activation" in text and "Rétention S4" in text


async def test_blocked_user_is_marked_and_skipped(db, onboarded_user, monkeypatch):
    from telegram.error import Forbidden

    class BlockedBot(FakeBot):
        async def send_message(self, chat_id, text, reply_markup=None):
            raise Forbidden("bot was blocked by the user")

    monkeypatch.setattr(jobs, "now", lambda: at(FROZEN_DAY, 7, 50))
    ctx = SimpleNamespace(bot=BlockedBot(), application=SimpleNamespace(bot_data={"db": db}))
    await jobs.tick(ctx)
    async with db.session() as s:
        user = await s.get(User, onboarded_user)
        assert user.blocked_at is not None
        assert user.last_brief_on == FROZEN_DAY  # marqueur conservé : pas de renvoi
        assert await services.get_week_plan(s, user, FROZEN_DAY) is not None  # planning conservé

    sent = FakeBot()
    ctx.bot = sent
    await jobs.tick(ctx)  # utilisateur ignoré tant qu'il n'a pas réécrit
    assert sent.sent == []


async def test_failed_send_is_not_retried_every_minute(db, onboarded_user, monkeypatch):
    class FlakyBot(FakeBot):
        async def send_message(self, chat_id, text, reply_markup=None):
            raise RuntimeError("réseau")

    monkeypatch.setattr(jobs, "now", lambda: at(FROZEN_DAY, 7, 50))
    ctx = SimpleNamespace(bot=FlakyBot(), application=SimpleNamespace(bot_data={"db": db}))
    await jobs.tick(ctx)
    ctx.bot = FakeBot()
    await jobs.tick(ctx)
    assert not any(t.startswith("Bonjour") for _, t, _ in ctx.bot.sent)

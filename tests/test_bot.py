"""Tests for the Telegram handlers with fake Update objects (no network)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from coach import bot, services
from coach.config import get_settings
from coach.db import User

from .conftest import FROZEN_DAY


class Recorder:
    def __init__(self):
        self.texts: list[str] = []
        self.markups: list = []
        self.documents: list = []

    async def reply_text(self, text, reply_markup=None):
        self.texts.append(text)
        self.markups.append(reply_markup)

    async def reply_document(self, document, filename=None, caption=None):
        self.documents.append((filename, document.getvalue()))

    async def edit_message_reply_markup(self, markup=None):
        pass


class FakeAgent:
    def __init__(self):
        self.calls = []

    async def reply(self, s, user, text):
        self.calls.append(text)
        return "réponse de l'agent"


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))

    async def send_chat_action(self, chat_id, action):
        pass


def make(db, telegram_id=1001, text=None, data=None, args=None):
    msg = Recorder()
    agent = FakeAgent()
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"db": db, "agent": agent}),
        bot=FakeBot(),
        args=args or [],
    )
    query = None
    if data is not None:
        async def answer():
            return None
        query = SimpleNamespace(data=data, message=msg, answer=answer,
                                edit_message_reply_markup=msg.edit_message_reply_markup)
    message = SimpleNamespace(text=text, reply_text=msg.reply_text, reply_document=msg.reply_document)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id, first_name="Léa"),
        effective_chat=SimpleNamespace(id=telegram_id),
        effective_message=message,
        message=message,
        callback_query=query,
    )
    return update, context, msg, agent


async def test_start_asks_for_consent_then_welcome(db):
    update, ctx, msg, _ = make(db, telegram_id=55)
    await bot.cmd_start(update, ctx)
    assert "données" in msg.texts[0] and msg.markups[0] is not None
    update, ctx, msg, _ = make(db, telegram_id=55, data="consent:yes")
    await bot.on_button(update, ctx)
    async with db.session() as s:
        assert (await services.get_user(s, 55)).consent_at is not None
    assert "qu'est-ce que tu aimerais changer" in msg.texts[0]


async def test_invite_code_required_when_configured(db, monkeypatch):
    monkeypatch.setenv("PILOT_INVITE_CODES", "INP2026")
    get_settings.cache_clear()
    try:
        update, ctx, msg, _ = make(db, telegram_id=77)
        await bot.cmd_start(update, ctx)
        assert "réservé" in msg.texts[0]
        update, ctx, msg, _ = make(db, telegram_id=77, args=["inp2026"])
        await bot.cmd_start(update, ctx)
        assert "données" in msg.texts[0]
    finally:
        monkeypatch.delenv("PILOT_INVITE_CODES")
        get_settings.cache_clear()


async def test_pain_message_never_reaches_the_llm(db, onboarded_user):
    update, ctx, msg, agent = make(db, text="j'ai mal au genou depuis la dernière séance")
    await bot.on_text(update, ctx)
    assert agent.calls == []
    assert "pause" in msg.texts[0]
    async with db.session() as s:
        assert (await s.get(User, onboarded_user)).safety_hold


async def test_ordinary_message_goes_to_agent(db, onboarded_user):
    update, ctx, msg, agent = make(db, text="réunion jusqu'à 19h ce soir")
    await bot.on_text(update, ctx)
    assert agent.calls == ["réunion jusqu'à 19h ce soir"]
    assert msg.texts == ["réponse de l'agent"]


async def _today_session(db, uid):
    async with db.session() as s:
        user = await s.get(User, uid)
        await services.create_week_plan(s, user, FROZEN_DAY)
        return (await services.sessions_between(s, user, FROZEN_DAY, FROZEN_DAY + timedelta(days=1)))[0].id


async def test_feedback_button_records_feedback(db, onboarded_user):
    sid = await _today_session(db, onboarded_user)
    update, ctx, msg, _ = make(db, data=f"fb:{sid}:hard")
    await bot.on_button(update, ctx)
    assert "Retour enregistré" in msg.texts[0]
    async with db.session() as s:
        user = await s.get(User, onboarded_user)
        ps = await services.get_session(s, user, sid)
        assert ps.status == "done" and ps.feedback == "hard"


async def test_not_today_then_postpone(db, onboarded_user):
    sid = await _today_session(db, onboarded_user)
    update, ctx, msg, _ = make(db, data=f"brief:{sid}:no")
    await bot.on_button(update, ctx)
    assert msg.markups[0] is not None
    update, ctx, msg, _ = make(db, data=f"nt:{sid}:later")
    await bot.on_button(update, ctx)
    assert "placée" in msg.texts[0]
    async with db.session() as s:
        ps = await services.get_session(s, await s.get(User, onboarded_user), sid)
        assert ps.day > FROZEN_DAY and ps.status == "planned"


async def test_commands_today_week_grocery(db, onboarded_user):
    await _today_session(db, onboarded_user)
    for handler, expected in ((bot.cmd_today, "Bonjour"), (bot.cmd_week, "Séances"), (bot.cmd_grocery, "Liste")):
        update, ctx, msg, _ = make(db, text="/x")
        await handler(update, ctx)
        assert msg.texts and msg.texts[0].startswith(expected), (handler.__name__, msg.texts)


async def test_planning_command_sends_plan_with_buttons(db, onboarded_user):
    update, ctx, msg, _ = make(db, text="/planifier")
    await bot.cmd_plan(update, ctx)
    chat_id, text, markup = ctx.bot.sent[-1]
    assert chat_id == 1001 and "Séances" in text and markup is not None


async def test_export_then_delete(db, onboarded_user):
    update, ctx, msg, _ = make(db, text="/export")
    await bot.cmd_export(update, ctx)
    assert msg.documents and msg.documents[0][0] == "mes-donnees.json"
    update, ctx, msg, _ = make(db, data="del:yes")
    await bot.on_button(update, ctx)
    assert "effacées" in msg.texts[0]
    async with db.session() as s:
        assert await services.get_user(s, 1001) is None


def test_long_messages_are_split():
    sent = []

    async def target(text, reply_markup=None):
        sent.append((len(text), reply_markup))

    import asyncio
    asyncio.run(bot.send_long(target, "\n".join(["x" * 100] * 120), reply_markup="kb"))
    assert len(sent) >= 3 and all(n <= 4096 for n, _ in sent)
    assert sent[-1][1] == "kb" and all(m is None for _, m in sent[:-1])


def test_application_builds_with_all_handlers(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST")
    get_settings.cache_clear()
    try:
        from coach.db import Database
        app = bot.build_application(Database("sqlite+aiosqlite:///:memory:"), FakeAgent())
        assert sum(len(h) for h in app.handlers.values()) >= 14
    finally:
        get_settings.cache_clear()


async def test_button_on_inaccessible_message_falls_back_to_send(db, onboarded_user):
    sid = await _today_session(db, onboarded_user)
    update, ctx, msg, _ = make(db, data=f"fb:{sid}:ok")

    async def refuse(markup=None):
        from telegram.error import BadRequest
        raise BadRequest("Message can't be edited")

    update.callback_query.message = SimpleNamespace()  # InaccessibleMessage: no reply_text
    update.callback_query.edit_message_reply_markup = refuse
    await bot.on_button(update, ctx)
    assert ctx.bot.sent and "Retour enregistré" in ctx.bot.sent[0][1]


async def test_agent_failure_rolls_back_partial_changes(db, onboarded_user):
    class BrokenAgent:
        async def reply(self, s, user, text):
            user.goal_text = "modifié avant la panne"
            await services.add_chat(s, user, "user", text)
            raise RuntimeError("API indisponible")

    update, ctx, msg, _ = make(db, text="décale ma séance")
    ctx.application.bot_data["agent"] = BrokenAgent()
    await bot.on_text(update, ctx)
    assert "souci technique" in msg.texts[0]
    async with db.session() as s:
        user = await s.get(User, onboarded_user)
        assert user.goal_text != "modifié avant la panne"
        assert await services.recent_chat(s, user) == []


async def test_large_telegram_ids_are_supported(db):
    async with db.session() as s:
        await services.get_or_create_user(s, 7_912_345_678, "Grand ID")
    async with db.session() as s:
        assert (await services.get_user(s, 7_912_345_678)).first_name == "Grand ID"

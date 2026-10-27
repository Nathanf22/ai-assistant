"""Bot Telegram : commandes, messages libres et boutons en un tap."""

from __future__ import annotations

import io
import json
import logging
from datetime import date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from coach import messages, planner, safety, services
from coach.agent import Agent, feedback_result_text, summary_text
from coach.config import get_settings
from coach.db import Database, User, log_event
from coach.metrics import compute, report
from coach.planner import format_grocery
from coach.timeutil import today

log = logging.getLogger(__name__)
MAX_LEN = 4000


def db_of(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def agent_of(context: ContextTypes.DEFAULT_TYPE) -> Agent:
    return context.application.bot_data["agent"]


async def send_long(target, text: str, reply_markup=None) -> None:
    """Envoie un texte en plusieurs messages si nécessaire (limite Telegram : 4096 caractères)."""
    chunks, current = [], ""
    for para in text.split("\n"):
        if len(current) + len(para) + 1 > MAX_LEN:
            chunks.append(current)
            current = ""
        current += para + "\n"
    chunks.append(current)
    chunks = [c.strip() for c in chunks if c.strip()]
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        await target(chunk, reply_markup=markup)


# --- Claviers -------------------------------------------------------------------------


def consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("J'accepte", callback_data="consent:yes"),
        InlineKeyboardButton("Non merci", callback_data="consent:no"),
    ]])


def feedback_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Facile", callback_data=f"fb:{session_id}:easy"),
            InlineKeyboardButton("Ok", callback_data=f"fb:{session_id}:ok"),
            InlineKeyboardButton("Dur", callback_data=f"fb:{session_id}:hard"),
        ],
        [InlineKeyboardButton("Je ne l'ai pas faite", callback_data=f"fb:{session_id}:skip")],
    ])


def brief_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Ok 👍", callback_data=f"brief:{session_id}:go"),
            InlineKeyboardButton("Pas aujourd'hui", callback_data=f"brief:{session_id}:no"),
        ],
        [InlineKeyboardButton("Voir la séance", callback_data=f"show:{session_id}")],
    ])


def not_today_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("La décaler", callback_data=f"nt:{session_id}:later"),
        InlineKeyboardButton("L'abandonner", callback_data=f"nt:{session_id}:drop"),
    ]])


def planning_keyboard(week_start: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Ça me va", callback_data=f"plan:ok:{week_start.isoformat()}"),
        InlineKeyboardButton("Liste de courses", callback_data=f"plan:list:{week_start.isoformat()}"),
    ]])


def survey_keyboard() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(str(n), callback_data=f"nps:{n}") for n in range(0, 6)]
    row2 = [InlineKeyboardButton(str(n), callback_data=f"nps:{n}") for n in range(6, 11)]
    return InlineKeyboardMarkup([row1, row2])


# --- Commandes ------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    settings = get_settings()
    code = (context.args[0].strip().upper() if context.args else "")
    async with db_of(context).session() as s:
        existing = await services.get_user(s, tg.id)
        if existing is None and settings.invite_codes and code not in settings.invite_codes:
            await update.message.reply_text(
                "Cet assistant est pour l'instant réservé aux participants du pilote. "
                "Utilise le lien d'invitation que tu as reçu."
            )
            return
        user = await services.get_or_create_user(s, tg.id, tg.first_name or "")
        user.blocked_at = None
        if code and not user.invite_code:
            user.invite_code = code
        if user.consent_at is None:
            name = f" {user.first_name}" if user.first_name else ""
            await update.message.reply_text(messages.CONSENT_TEXT.format(name=name), reply_markup=consent_keyboard())
            return
    await update.message.reply_text("Content de te revoir ! " + messages.HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(messages.HELP_TEXT)


async def _ready_user(update: Update, s) -> User | None:
    user = await services.get_user(s, update.effective_user.id)
    if user is None or user.consent_at is None:
        await update.effective_message.reply_text("Écris /start pour commencer.")
        return None
    if not user.is_onboarded:
        await update.effective_message.reply_text("On termine d'abord de faire connaissance : réponds à ma dernière question.")
        return None
    return user


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db_of(context).session() as s:
        if not (user := await _ready_user(update, s)):
            return
        t = today()
        sessions = [p for p in await services.sessions_between(s, user, t, t + timedelta(days=1)) if p.status == "planned"]
        meals = await services.meals_between(s, user, t, t + timedelta(days=1))
        text = messages.brief_text(user, sessions[0] if sessions else None, meals[0] if meals else None)
        markup = brief_keyboard(sessions[0].id) if sessions else None
    await update.message.reply_text(text, reply_markup=markup)


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db_of(context).session() as s:
        if not (user := await _ready_user(update, s)):
            return
        ws = planner.week_start_of(today())
        text = services.describe_week(
            await services.sessions_between(s, user, ws, ws + timedelta(days=7)),
            await services.meals_between(s, user, ws, ws + timedelta(days=7)),
        )
    await send_long(update.message.reply_text, text)


async def cmd_grocery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db_of(context).session() as s:
        if not (user := await _ready_user(update, s)):
            return
        t = today()
        # Le dimanche après le planning, la liste utile est celle de la semaine suivante.
        ws = planner.next_week_start(t) if t.weekday() == 6 else planner.week_start_of(t)
        plan = await services.get_week_plan(s, user, ws) or await services.get_week_plan(s, user, planner.week_start_of(t))
        if plan is None:
            text = "Pas encore de planning : écris /planifier."
        else:
            await services.refresh_grocery(s, user, plan)
            text = "Liste de courses\n\n" + format_grocery(plan.grocery)
    await send_long(update.message.reply_text, text)


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db_of(context).session() as s:
        if not (user := await _ready_user(update, s)):
            return
        t = today()
        ws = planner.next_week_start(t) if t.weekday() >= 4 else planner.week_start_of(t)
        await send_planning(context, s, user, ws, with_summary=False)


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db_of(context).session() as s:
        if not (user := await _ready_user(update, s)):
            return
        text = summary_text(await services.week_summary(s, user, planner.week_start_of(today())))
    await update.message.reply_text(text)


async def cmd_quiet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db_of(context).session() as s:
        if not (user := await _ready_user(update, s)):
            return
        user.quiet_dates = sorted({*user.quiet_dates, today().isoformat()})
    await update.message.reply_text("D'accord, pas de message automatique aujourd'hui.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db_of(context).session() as s:
        if not (user := await _ready_user(update, s)):
            return
        if not user.safety_hold:
            await update.message.reply_text("Rien n'est en pause, tout roule.")
            return
        await services.lift_safety_hold(s, user)
    await update.message.reply_text(
        "Content que ça aille mieux ! Les séances reprennent au niveau d'avant la pause. "
        "Au moindre doute, écoute ton corps et dis-le-moi."
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db_of(context).session() as s:
        user = await services.get_user(s, update.effective_user.id)
        if user is None:
            await update.message.reply_text("Je n'ai aucune donnée te concernant.")
            return
        data = await services.export_user_data(s, user)
        log_event(s, user.id, "data_exported")
    payload = json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    await update.message.reply_document(io.BytesIO(payload), filename="mes-donnees.json",
                                        caption="Voici toutes les données que je garde sur toi.")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Je vais effacer définitivement ton profil, tes plannings, tes retours et notre conversation. "
        "C'est irréversible. On y va ?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Oui, tout effacer", callback_data="del:yes"),
            InlineKeyboardButton("Annuler", callback_data="del:no"),
        ]]),
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in get_settings().admin_ids:
        return
    async with db_of(context).session() as s:
        text = report(await compute(s))
    await update.message.reply_text(text)


# --- Messages libres -------------------------------------------------------------------


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    async with db_of(context).session() as s:
        user = await services.get_user(s, update.effective_user.id)
        if user is None or user.consent_at is None:
            await update.message.reply_text("Écris /start pour commencer.")
            return

        user.blocked_at = None
        risk = safety.check(text)
        if risk is not None:
            log_event(s, user.id, "safety_triggered", category=risk.category)  # sans le texte
            if risk.holds_progression:
                await services.set_safety_hold(s, user, risk.category)
            await update.message.reply_text(risk.message)
            return

        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        try:
            answer = await agent_of(context).reply(s, user, text)
        except Exception:
            log.exception("Échec de l'agent")
            await s.rollback()  # pas de changement partiel ni de tour utilisateur orphelin
            answer = "Je rencontre un souci technique, réessaie dans quelques minutes."
    await send_long(update.message.reply_text, answer)


# --- Boutons ---------------------------------------------------------------------------


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    kind, _, rest = data.partition(":")
    chat_id = update.effective_chat.id

    async def reply(text, reply_markup=None):
        # Un bouton sur un message de plus de 48 h arrive avec un message inaccessible,
        # sans reply_text : on répond alors directement dans la conversation.
        target = getattr(query.message, "reply_text", None)
        if target is not None:
            return await target(text, reply_markup=reply_markup)
        return await context.bot.send_message(chat_id, text, reply_markup=reply_markup)

    async def clear_buttons(markup=None):
        try:
            await query.edit_message_reply_markup(markup)
        except TelegramError:
            pass  # message trop ancien ou déjà modifié

    async with db_of(context).session() as s:
        user = await services.get_user(s, update.effective_user.id)
        if user is None:
            await reply("Écris /start pour commencer.")
            return

        if kind == "consent":
            await clear_buttons()
            if rest == "yes":
                await services.record_consent(s, user)
                await reply(messages.WELCOME_AFTER_CONSENT)
            else:
                await reply("Pas de souci. Je n'enregistre rien de plus ; /supprimer efface le peu que j'ai.")
            return

        if kind == "del":
            await clear_buttons()
            if rest == "yes":
                await services.delete_user(s, user)
                await reply("C'est fait : toutes tes données ont été effacées. Merci d'avoir essayé !")
            else:
                await reply("Rien n'a été effacé.")
            return

        if kind == "nps":
            await clear_buttons()
            log_event(s, user.id, "nps", score=int(rest))
            await reply("Merci ! Si tu veux ajouter un mot sur ce qui t'a aidé ou manqué, écris-le-moi.")
            return

        if not user.is_onboarded:
            await reply("On termine d'abord de faire connaissance.")
            return

        try:
            if kind == "fb":
                sid, value = rest.split(":")
                await clear_buttons()
                if value == "skip":
                    await services.skip_session(s, user, int(sid))
                    nxt = await services.next_free_day(s, user, today())
                    tail = " Je peux la replacer, dis-moi juste quand." if nxt else ""
                    await reply("Pas grave, ça arrive. On repart sur la prochaine." + tail)
                else:
                    out = await services.record_feedback(s, user, int(sid), value)
                    await reply(feedback_result_text(out) + " Si tu veux ajouter un détail, écris-le.")

            elif kind == "brief":
                sid, value = rest.split(":")
                await clear_buttons()
                if value == "go":
                    log_event(s, user.id, "brief_ack", session_id=int(sid))
                    await reply("Parfait. Je te demanderai comment ça s'est passé après la séance.")
                else:
                    await reply("D'accord. Tu préfères la décaler ou l'abandonner cette semaine ?",
                                reply_markup=not_today_keyboard(int(sid)))

            elif kind == "nt":
                sid, value = rest.split(":")
                await clear_buttons()
                if value == "later":
                    ps = await services.get_session(s, user, int(sid))
                    nxt = await services.next_free_day(s, user, max(ps.day, today()))
                    if nxt is None:
                        await services.skip_session(s, user, int(sid))
                        await reply("Plus de jour libre cette semaine : on la laisse de côté, sans souci.")
                    else:
                        ps = await services.reschedule(s, user, int(sid), "postpone", new_day=nxt)
                        await reply(f"C'est noté, je l'ai placée {services.day_label(nxt)} à {ps.time.replace(':', 'h')}.")
                else:
                    await services.skip_session(s, user, int(sid))
                    await reply("C'est noté. Pas de rattrapage nécessaire, on continue la semaine normalement.")

            elif kind == "show":
                ps = await services.get_session(s, user, int(rest))
                await send_long(reply, services.describe_session(ps))

            elif kind == "plan":
                action, _, iso = rest.partition(":")
                ws = date.fromisoformat(iso)
                if action == "ok":
                    await clear_buttons(planning_keyboard_list_only(ws))
                    log_event(s, user.id, "planning_confirmed", week_start=iso, source="button")
                    await reply("Super, c'est calé. Bonne semaine !")
                else:
                    plan = await services.get_week_plan(s, user, ws)
                    if plan:
                        await services.refresh_grocery(s, user, plan)
                        await send_long(reply, "Liste de courses\n\n" + format_grocery(plan.grocery))
        except services.ServiceError as exc:
            await reply(str(exc))


def planning_keyboard_list_only(week_start: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Liste de courses", callback_data=f"plan:list:{week_start.isoformat()}"),
    ]])


# --- Envoi du planning (commande et planificateur) ----------------------------------------------


async def send_planning(context: ContextTypes.DEFAULT_TYPE, s, user: User, week_start: date,
                        with_summary: bool) -> None:
    summary_line = None
    if with_summary:
        summary = await services.week_summary(s, user, planner.week_start_of(today()))
        summary_line = summary_text(summary) if summary.planned else None
    load_before = user.load_level
    await services.create_week_plan(s, user, week_start)
    await s.commit()  # le planning existe même si l'envoi échoue
    end = week_start + timedelta(days=7)
    body = services.describe_week(
        await services.sessions_between(s, user, week_start, end),
        await services.meals_between(s, user, week_start, end),
    )
    intro = messages.planning_intro(user, summary_line)
    if user.load_level < load_before:
        intro += "\n\n" + messages.comeback_text()
    text = f"{intro}\n\n{body}\n\n{messages.planning_outro()}"
    await send_long(lambda t, reply_markup=None: context.bot.send_message(user.telegram_id, t, reply_markup=reply_markup),
                    text, reply_markup=planning_keyboard(week_start))


# --- Application ---------------------------------------------------------------------------


def build_application(db: Database, agent: Agent) -> Application:
    from coach.jobs import register_jobs

    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN manquant (voir .env.example).")
    async def post_init(_: Application) -> None:
        await db.create_all()  # MVP : création du schéma ; migrations Alembic à prévoir

    async def post_shutdown(_: Application) -> None:
        await db.dispose()

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["db"] = db
    app.bot_data["agent"] = agent

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["aide", "help"], cmd_help))
    app.add_handler(CommandHandler("aujourdhui", cmd_today))
    app.add_handler(CommandHandler("semaine", cmd_week))
    app.add_handler(CommandHandler("courses", cmd_grocery))
    app.add_handler(CommandHandler("planifier", cmd_plan))
    app.add_handler(CommandHandler("bilan", cmd_summary))
    app.add_handler(CommandHandler("silence", cmd_quiet))
    app.add_handler(CommandHandler("reprendre", cmd_resume))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("supprimer", cmd_delete))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    register_jobs(app)
    return app

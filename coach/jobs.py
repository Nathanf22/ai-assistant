"""Planificateur : l'assistant écrit en premier (brief, retour après séance, planning du dimanche).

Une tâche tourne chaque minute et décide, pour chaque utilisateur, ce qui doit partir.
Des marqueurs en base (`last_brief_on`, `last_planning_on`, `feedback_requested_at`) évitent
les doublons, y compris après un redémarrage. Chaque marqueur est validé (commit) AVANT l'envoi :
en cas d'échec, on préfère perdre un message plutôt que de le renvoyer chaque minute.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from telegram.error import Forbidden
from telegram.ext import Application, ContextTypes

from coach import messages, planner, services
from coach.config import get_settings
from coach.db import Database, User, log_event
from coach.timeutil import local_datetime, now, parse_hhmm

log = logging.getLogger(__name__)

BRIEF_WINDOW = timedelta(hours=3)
SURVEY_AFTER = timedelta(days=56)  # fin du pilote de 8 semaines


def register_jobs(app: Application) -> None:
    app.job_queue.run_repeating(tick, interval=60, first=10, name="tick")


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    async with db.session() as s:
        ids = list((await s.scalars(
            select(User.id).where(
                User.onboarded_at.is_not(None), User.consent_at.is_not(None), User.blocked_at.is_(None)
            )
        )).all())
    for uid in ids:
        try:
            async with db.session() as s:
                user = await s.get(User, uid)
                if user is not None:
                    await run_for_user(context, s, user, now())
        except Forbidden:
            log.info("Utilisateur %s a bloqué le bot : envois suspendus", uid)
            async with db.session() as s:
                user = await s.get(User, uid)
                if user is not None:
                    user.blocked_at = datetime.now(UTC)
                    log_event(s, uid, "bot_blocked")
        except Exception:
            log.exception("Échec du planificateur pour l'utilisateur %s", uid)


def _reached(current: datetime, hhmm: str) -> bool:
    t = parse_hhmm(hhmm)
    return (current.hour, current.minute) >= (t.hour, t.minute)


async def run_for_user(context: ContextTypes.DEFAULT_TYPE, s, user: User, current: datetime) -> list[str]:
    """Exécute les envois dus pour un utilisateur. Renvoie la liste des actions (pour les tests)."""
    from coach.bot import brief_keyboard, feedback_keyboard, send_planning, survey_keyboard

    done: list[str] = []
    t: date = current.date()
    quiet = t.isoformat() in (user.quiet_dates or [])
    bot = context.bot

    # Filet de sécurité : toujours un planning pour la semaine en cours.
    ws = planner.week_start_of(t)
    if await services.get_week_plan(s, user, ws) is None and user.slots:
        await services.create_week_plan(s, user, ws)
        await s.commit()
        done.append("week_plan_created")

    # Planning du dimanche pour la semaine suivante (US5) + bilan (US19).
    if t.weekday() == 6 and user.last_planning_on != t and _reached(current, user.planning_time):
        user.last_planning_on = t
        await s.commit()
        if quiet:
            await services.create_week_plan(s, user, planner.next_week_start(t))
        else:
            await send_planning(context, s, user, planner.next_week_start(t), with_summary=True)
        done.append("planning")

    # Brief du matin (US9).
    brief_at = local_datetime(t, user.brief_time)
    if user.last_brief_on != t and brief_at <= current < brief_at + BRIEF_WINDOW:
        user.last_brief_on = t
        await s.commit()
        sessions = [p for p in await services.sessions_between(s, user, t, t + timedelta(days=1))
                    if p.status == "planned"]
        meals = await services.meals_between(s, user, t, t + timedelta(days=1))
        if not quiet and (sessions or meals):
            session = sessions[0] if sessions else None
            await bot.send_message(
                user.telegram_id,
                messages.brief_text(user, session, meals[0] if meals else None),
                reply_markup=brief_keyboard(session.id) if session else None,
            )
            log_event(s, user.id, "brief_sent", has_session=bool(session))
            done.append("brief")

    # Demande de retour après la séance (US14).
    delay = timedelta(minutes=get_settings().feedback_delay_minutes)
    recent = await services.sessions_between(s, user, t - timedelta(days=1), t + timedelta(days=1))
    for p in recent:
        if p.status != "planned" or p.feedback_requested_at is not None:
            continue
        end = local_datetime(p.day, p.time) + timedelta(minutes=p.minutes) + delay
        if end <= current:
            p.feedback_requested_at = datetime.now(UTC)
            await s.commit()
            if not quiet:
                await bot.send_message(user.telegram_id, messages.feedback_prompt(p),
                                       reply_markup=feedback_keyboard(p.id))
                log_event(s, user.id, "feedback_requested", session_id=p.id)
                done.append("feedback")

    # Questionnaire de fin de pilote (US24).
    onboarded = user.onboarded_at if user.onboarded_at.tzinfo else user.onboarded_at.replace(tzinfo=UTC)
    if user.survey_sent_at is None and current - onboarded >= SURVEY_AFTER and not quiet \
            and _reached(current, "12:00"):
        user.survey_sent_at = datetime.now(UTC)
        await s.commit()
        await bot.send_message(user.telegram_id, messages.SURVEY_TEXT, reply_markup=survey_keyboard())
        log_event(s, user.id, "survey_sent")
        done.append("survey")
    return done

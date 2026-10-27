"""Business operations on the database, shared by the agent, the bot and the scheduler.

Every significant change is recorded in `event_log` (pilot observability).
`ServiceError` messages can be shown to users as-is, so they stay in French.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from coach import planner, progression
from coach.db import ChatMessage, EventLog, PlannedMeal, PlannedSession, User, WeekPlan, log_event
from coach.library import EQUIPMENT, LEVELS, Library, get_library, recipe_violates
from coach.timeutil import day_label, normalize_hhmm, now, today
from coach.workouts import (
    FOCUS_ROTATION,
    SessionRequest,
    build_session,
    format_session,
    total_minutes,
)

HISTORY_TURNS = 12


class ServiceError(ValueError):
    """Business error whose message can be shown to the user or the agent (French)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- Users -----------------------------------------------------------------------


async def get_user(s: AsyncSession, telegram_id: int) -> User | None:
    return await s.scalar(select(User).where(User.telegram_id == telegram_id))


async def get_or_create_user(s: AsyncSession, telegram_id: int, first_name: str = "") -> User:
    user = await get_user(s, telegram_id)
    if user is None:
        user = User(telegram_id=telegram_id, first_name=first_name[:80])
        s.add(user)
        await s.flush()
        log_event(s, user.id, "user_created")
    return user


def _clean_list(values) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    return sorted({str(v).strip().lower() for v in values if str(v).strip()})


def _clean_slots(slots: list[dict]) -> list[dict]:
    out = []
    for raw in slots:
        day = int(raw["day"])
        if not 0 <= day <= 6:
            raise ServiceError("Le jour d'un créneau doit aller de 0 (lundi) à 6 (dimanche).")
        minutes = int(raw.get("minutes", 30))
        if not 10 <= minutes <= 90:
            raise ServiceError("Une séance dure entre 10 et 90 minutes.")
        out.append({"day": day, "time": normalize_hhmm(str(raw.get("time", "18:30"))), "minutes": minutes})
    return sorted(out, key=lambda x: (x["day"], x["time"]))


async def update_profile(s: AsyncSession, user: User, **fields) -> list[str]:
    """Update the profile with validation. Returns the list of changed fields."""
    changed: list[str] = []
    if "goal_text" in fields and fields["goal_text"]:
        user.goal_text = str(fields["goal_text"])[:500]
        changed.append("goal_text")
    if "sessions_per_week" in fields and fields["sessions_per_week"] is not None:
        n = int(fields["sessions_per_week"])
        if not 1 <= n <= 6:
            raise ServiceError("Entre 1 et 6 séances par semaine.")
        user.sessions_per_week = n
        changed.append("sessions_per_week")
    if fields.get("level"):
        if fields["level"] not in LEVELS:
            raise ServiceError(f"Niveau inconnu, choisir parmi : {', '.join(LEVELS)}.")
        user.level = fields["level"]
        changed.append("level")
    if fields.get("equipment") is not None:
        eq = [e for e in _clean_list(fields["equipment"]) if e in EQUIPMENT and e != "none"]
        user.equipment = eq
        changed.append("equipment")
    if fields.get("slots") is not None:
        user.slots = _clean_slots(fields["slots"])
        changed.append("slots")
    for key in ("food_exclusions", "food_dislikes", "pantry", "liked_exercises", "avoided_exercises"):
        if fields.get(key) is not None:
            setattr(user, key, _clean_list(fields[key]))
            changed.append(key)
    if fields.get("dinners_per_week") is not None:
        n = int(fields["dinners_per_week"])
        if not 0 <= n <= 7:
            raise ServiceError("Entre 0 et 7 dîners par semaine.")
        user.dinners_per_week = n
        changed.append("dinners_per_week")
    for key in ("brief_time", "planning_time"):
        if fields.get(key):
            setattr(user, key, normalize_hhmm(str(fields[key])))
            changed.append(key)
    if changed:
        log_event(s, user.id, "profile_updated", fields=changed)
    if "food_exclusions" in changed:
        await enforce_exclusions(s, user)
    return changed


def missing_profile_fields(user: User) -> list[str]:
    missing = []
    if not user.goal_text:
        missing.append("goal_text")
    if not user.slots:
        missing.append("slots")
    return missing


async def finish_onboarding(s: AsyncSession, user: User) -> None:
    missing = missing_profile_fields(user)
    if missing:
        raise ServiceError(f"Profil incomplet : {', '.join(missing)}.")
    if user.onboarded_at is None:
        user.onboarded_at = _utcnow()
        log_event(s, user.id, "onboarded")


async def record_consent(s: AsyncSession, user: User) -> None:
    if user.consent_at is None:
        user.consent_at = _utcnow()
        log_event(s, user.id, "consent_given")


# --- Conversation history ----------------------------------------------------------


async def add_chat(s: AsyncSession, user: User, role: str, content: str) -> None:
    s.add(ChatMessage(user_id=user.id, role=role, content=content[:4000]))


async def recent_chat(s: AsyncSession, user: User, limit: int = HISTORY_TURNS) -> list[ChatMessage]:
    rows = await s.scalars(
        select(ChatMessage)
        .where(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.id.desc())
        .limit(limit)
    )
    return list(reversed(rows.all()))


# --- Sessions ----------------------------------------------------------------------


async def done_history(s: AsyncSession, user: User) -> list[progression.DoneSession]:
    rows = await s.scalars(
        select(PlannedSession)
        .where(PlannedSession.user_id == user.id, PlannedSession.status == "done")
        .order_by(PlannedSession.day, PlannedSession.id)
    )
    return [progression.DoneSession(r.day, r.load_level, r.feedback) for r in rows]


def session_request(user: User, minutes: int, focus: str, seed: str) -> SessionRequest:
    if user.safety_hold:
        focus = "mobility"
    return SessionRequest(
        minutes=minutes,
        focus=focus,
        load=user.load_level,
        level=LEVELS.get(user.level, 1),
        equipment=frozenset(user.equipment or []),
        liked=frozenset(user.liked_exercises or []),
        avoided=frozenset(user.avoided_exercises or []),
        low_impact=user.safety_hold,
        seed=seed,
    )


def fill_session(lib: Library, user: User, ps: PlannedSession) -> None:
    req = session_request(user, ps.minutes, ps.focus, seed=f"{user.id}:{ps.day.isoformat()}")
    ps.exercises = build_session(lib, req)
    ps.load_level = user.load_level
    if user.safety_hold:
        ps.focus = "mobility"


async def get_session(s: AsyncSession, user: User, session_id: int) -> PlannedSession:
    ps = await s.get(PlannedSession, session_id)
    if ps is None or ps.user_id != user.id:
        raise ServiceError("Séance introuvable.")
    return ps


async def sessions_between(s: AsyncSession, user: User, start: date, end: date) -> list[PlannedSession]:
    rows = await s.scalars(
        select(PlannedSession)
        .where(PlannedSession.user_id == user.id, PlannedSession.day >= start, PlannedSession.day < end)
        .order_by(PlannedSession.day, PlannedSession.time)
    )
    return list(rows.all())


async def upcoming_planned(s: AsyncSession, user: User, from_day: date) -> list[PlannedSession]:
    rows = await s.scalars(
        select(PlannedSession)
        .where(
            PlannedSession.user_id == user.id,
            PlannedSession.day >= from_day,
            PlannedSession.status == "planned",
        )
        .order_by(PlannedSession.day, PlannedSession.time)
    )
    return list(rows.all())


async def reschedule(
    s: AsyncSession,
    user: User,
    session_id: int,
    action: str,
    minutes: int | None = None,
    new_day: date | None = None,
    new_time: str | None = None,
) -> PlannedSession:
    """Adapt a session to a last-minute change: shorten, postpone or cancel (F7, US10)."""
    ps = await get_session(s, user, session_id)
    if ps.status != "planned":
        raise ServiceError("Cette séance n'est plus prévue.")
    lib = get_library()
    before = {"day": ps.day.isoformat(), "time": ps.time, "minutes": ps.minutes}
    if action == "shorten":
        if not minutes or minutes < 8:
            raise ServiceError("Indiquer une durée d'au moins 8 minutes.")
        ps.minutes = min(minutes, ps.minutes)
        fill_session(lib, user, ps)
    elif action == "postpone":
        if new_day is None:
            raise ServiceError("Indiquer le jour de report.")
        if new_day < today():
            raise ServiceError("Impossible de reporter dans le passé.")
        ps.day = new_day
        if new_time:
            ps.time = normalize_hhmm(new_time)
        if minutes:
            ps.minutes = minutes
        ps.feedback_requested_at = None
        fill_session(lib, user, ps)
    elif action == "cancel":
        ps.status = "cancelled"
    else:
        raise ServiceError("Action inconnue : shorten, postpone ou cancel.")
    log_event(s, user.id, "session_rescheduled", session_id=ps.id, action=action, before=before,
              after={"day": ps.day.isoformat(), "time": ps.time, "minutes": ps.minutes})
    return ps


@dataclass
class FeedbackOutcome:
    session: PlannedSession
    decision: progression.LoadDecision


async def record_feedback(
    s: AsyncSession, user: User, session_id: int, feedback: str, note: str = ""
) -> FeedbackOutcome:
    """Record a feedback (easy / ok / hard) and apply the progression rules (F5, F6)."""
    if feedback not in ("easy", "ok", "hard"):
        raise ServiceError("Retour attendu : easy, ok ou hard.")
    ps = await get_session(s, user, session_id)
    if ps.status not in ("planned", "done"):
        raise ServiceError("Cette séance a été annulée ou sautée.")
    ps.status = "done"
    ps.feedback = feedback
    ps.feedback_note = note[:500]
    ps.feedback_at = _utcnow()
    await s.flush()

    history = await done_history(s, user)
    decision = progression.after_feedback(user.load_level, history, user.safety_hold)
    log_event(s, user.id, "feedback", session_id=ps.id, feedback=feedback,
              load_before=user.load_level, load_after=decision.load, reason=decision.reason)
    if decision.changed:
        user.load_level = decision.load
        lib = get_library()
        for upcoming in await upcoming_planned(s, user, today()):
            fill_session(lib, user, upcoming)
    return FeedbackOutcome(ps, decision)


async def skip_session(s: AsyncSession, user: User, session_id: int) -> PlannedSession:
    ps = await get_session(s, user, session_id)
    if ps.status == "planned":
        ps.status = "skipped"
        log_event(s, user.id, "session_skipped", session_id=ps.id)
    return ps


async def next_free_day(s: AsyncSession, user: User, after: date) -> date | None:
    """Next day of the current week, after `after`, with no session already planned."""
    week_end = planner.week_start_of(after) + timedelta(days=7)
    taken = {p.day for p in await sessions_between(s, user, after, week_end) if p.status == "planned"}
    d = after + timedelta(days=1)
    while d < week_end:
        if d not in taken:
            return d
        d += timedelta(days=1)
    return None


# --- Weekly planning ---------------------------------------------------------------


async def get_week_plan(s: AsyncSession, user: User, week_start: date) -> WeekPlan | None:
    return await s.scalar(
        select(WeekPlan)
        .where(WeekPlan.user_id == user.id, WeekPlan.week_start == week_start)
        .order_by(WeekPlan.id.desc())
    )


async def meals_between(s: AsyncSession, user: User, start: date, end: date) -> list[PlannedMeal]:
    rows = await s.scalars(
        select(PlannedMeal)
        .where(PlannedMeal.user_id == user.id, PlannedMeal.day >= start, PlannedMeal.day < end)
        .order_by(PlannedMeal.day)
    )
    return list(rows.all())


async def create_week_plan(
    s: AsyncSession,
    user: User,
    week_start: date,
    constraints: list[dict] | None = None,
    constraints_text: str = "",
) -> WeekPlan:
    """Create (or recreate) a week's plan: sessions, dinners, grocery list (F2, US5-US8)."""
    if not user.slots:
        raise ServiceError("Aucun créneau habituel : demander d'abord les disponibilités.")
    lib = get_library()
    constraints = constraints or []
    week_end = week_start + timedelta(days=7)
    first_day = max(week_start, today())

    # Comeback after a break (US18).
    history = await done_history(s, user)
    last_done = history[-1] if history else None
    comeback = progression.before_planning(user.load_level, last_done, today())
    if comeback.changed:
        user.load_level = comeback.load
        log_event(s, user.id, "load_comeback", load=comeback.load)

    # Replace any existing plan, touching only items that are still ahead.
    old = await get_week_plan(s, user, week_start)
    previous_recipes: set[str] = set()
    prev_meals = await meals_between(s, user, week_start - timedelta(days=7), week_start)
    previous_recipes |= {m.recipe_id for m in prev_meals}
    await s.execute(
        delete(PlannedSession).where(
            PlannedSession.user_id == user.id,
            PlannedSession.day >= first_day,
            PlannedSession.day < week_end,
            PlannedSession.status == "planned",
        )
    )
    await s.execute(
        delete(PlannedMeal).where(
            PlannedMeal.user_id == user.id, PlannedMeal.day >= first_day, PlannedMeal.day < week_end
        )
    )
    already_done = [p for p in await sessions_between(s, user, week_start, week_end) if p.status == "done"]

    plan = old or WeekPlan(user_id=user.id, week_start=week_start)
    plan.constraints_text = constraints_text[:1000]
    if old is None:
        s.add(plan)
    await s.flush()

    slots = [sl for sl in planner.candidate_slots(week_start, user.slots, constraints) if sl.day >= first_day]
    wanted = max(0, user.sessions_per_week - len(already_done))
    chosen = planner.choose_sessions(slots, wanted)
    for n, sl in enumerate(chosen):
        ps = PlannedSession(
            user_id=user.id,
            week_plan_id=plan.id,
            day=sl.day,
            time=sl.time,
            minutes=sl.minutes,
            focus=FOCUS_ROTATION[(len(already_done) + n) % len(FOCUS_ROTATION)],
        )
        fill_session(lib, user, ps)
        s.add(ps)

    dinners = planner.choose_dinners(
        lib, week_start, user.dinners_per_week, user.food_exclusions, user.food_dislikes,
        {sl.day for sl in chosen}, seed=str(user.id), avoid_recipes=previous_recipes,
        from_day=first_day,
    )
    for d, recipe_id in dinners:
        s.add(PlannedMeal(user_id=user.id, week_plan_id=plan.id, day=d, recipe_id=recipe_id))
    await s.flush()
    await refresh_grocery(s, user, plan)

    log_event(s, user.id, "week_planned", week_start=week_start.isoformat(),
              sessions=[{"day": c.day.isoformat(), "time": c.time, "minutes": c.minutes} for c in chosen],
              dinners=[r for _, r in dinners], constraints=constraints, load=user.load_level)
    return plan


async def refresh_grocery(s: AsyncSession, user: User, plan: WeekPlan) -> list[dict]:
    meals = await meals_between(s, user, plan.week_start, plan.week_start + timedelta(days=7))
    upcoming = [(m.recipe_id, m.servings) for m in meals if m.day >= today()]
    plan.grocery = planner.grocery_list(get_library(), upcoming, user.pantry)
    return plan.grocery


async def swap_meal(s: AsyncSession, user: User, day: date, recipe_id: str | None = None) -> PlannedMeal:
    """Replace a day's dinner (US7). Without `recipe_id`, pick a compatible alternative."""
    lib = get_library()
    meal = await s.scalar(select(PlannedMeal).where(PlannedMeal.user_id == user.id, PlannedMeal.day == day))
    if meal is None:
        raise ServiceError(f"Pas de dîner prévu {day_label(day)}.")
    week_start = planner.week_start_of(day)
    taken = {m.recipe_id for m in await meals_between(s, user, week_start, week_start + timedelta(days=7))}
    if recipe_id:
        recipe = lib.recipe(recipe_id)
        if recipe is None:
            raise ServiceError("Recette inconnue dans la bibliothèque.")
        if recipe_violates(recipe, user.food_exclusions):
            raise ServiceError("Cette recette contient un aliment exclu.")
    else:
        options = planner.swap_candidates(lib, meal.recipe_id, user.food_exclusions, user.food_dislikes, taken)
        if not options:
            raise ServiceError("Aucune autre recette compatible pour le moment.")
        recipe = options[(day.toordinal() + len(taken)) % len(options)]
    old = meal.recipe_id
    meal.recipe_id = recipe.id
    plan = await get_week_plan(s, user, week_start)
    if plan:
        await s.flush()
        await refresh_grocery(s, user, plan)
    log_event(s, user.id, "meal_swapped", day=day.isoformat(), before=old, after=recipe.id)
    return meal


async def enforce_exclusions(s: AsyncSession, user: User) -> int:
    """After an exclusion is added, replace every upcoming dinner that contains it (US3 guarantee)."""
    lib = get_library()
    swapped = 0
    rows = await s.scalars(
        select(PlannedMeal).where(PlannedMeal.user_id == user.id, PlannedMeal.day >= today())
    )
    for meal in rows.all():
        recipe = lib.recipe(meal.recipe_id)
        if recipe is not None and recipe_violates(recipe, user.food_exclusions):
            try:
                await swap_meal(s, user, meal.day)
                swapped += 1
            except ServiceError:
                await s.delete(meal)
    return swapped


# --- One-off requests ------------------------------------------------------------------


async def quick_session(s: AsyncSession, user: User, minutes: int, day: date | None = None) -> PlannedSession:
    """Immediate session for the time available (F10, US11). Replaces a session planned that day."""
    day = day or today()
    lib = get_library()
    same_day = [p for p in await sessions_between(s, user, day, day + timedelta(days=1)) if p.status == "planned"]
    if same_day:
        ps = same_day[0]
        ps.minutes = minutes
        fill_session(lib, user, ps)
    else:
        week = await get_week_plan(s, user, planner.week_start_of(day))
        ps = PlannedSession(user_id=user.id, week_plan_id=week.id if week else None, day=day,
                            time=now().strftime("%H:%M"), minutes=minutes, focus="full_body")
        fill_session(lib, user, ps)
        s.add(ps)
        await s.flush()
    log_event(s, user.id, "quick_session", session_id=ps.id, minutes=minutes)
    return ps


# --- Safety guardrail -----------------------------------------------------------------


async def set_safety_hold(s: AsyncSession, user: User, category: str) -> None:
    if not user.safety_hold:
        user.safety_hold = True
        user.safety_hold_at = _utcnow()
        lib = get_library()
        for upcoming in await upcoming_planned(s, user, today()):
            fill_session(lib, user, upcoming)
    log_event(s, user.id, "safety_hold", category=category)


async def lift_safety_hold(s: AsyncSession, user: User) -> None:
    if user.safety_hold:
        user.safety_hold = False
        user.safety_hold_at = None
        lib = get_library()
        rotation = 0
        for upcoming in await upcoming_planned(s, user, today()):
            upcoming.focus = FOCUS_ROTATION[rotation % len(FOCUS_ROTATION)]
            rotation += 1
            fill_session(lib, user, upcoming)
        log_event(s, user.id, "safety_hold_lifted")


# --- Weekly summary -------------------------------------------------------------------


@dataclass
class WeekSummary:
    planned: int
    done: int
    skipped: int
    load_start: int | None
    load_end: int


async def week_summary(s: AsyncSession, user: User, week_start: date) -> WeekSummary:
    sessions = await sessions_between(s, user, week_start, week_start + timedelta(days=7))
    counted = [p for p in sessions if p.status != "cancelled"]
    done = [p for p in counted if p.status == "done"]
    return WeekSummary(
        planned=len(counted),
        done=len(done),
        skipped=sum(p.status == "skipped" for p in counted),
        load_start=done[0].load_level if done else None,
        load_end=user.load_level,
    )


# --- Personal data (US21) ---------------------------------------------------------------


async def export_user_data(s: AsyncSession, user: User) -> dict:
    def iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v

    profile = {c.name: iso(getattr(user, c.name)) for c in User.__table__.columns}
    sessions = await s.scalars(select(PlannedSession).where(PlannedSession.user_id == user.id))
    meals = await s.scalars(select(PlannedMeal).where(PlannedMeal.user_id == user.id))
    plans = await s.scalars(select(WeekPlan).where(WeekPlan.user_id == user.id))
    chats = await s.scalars(select(ChatMessage).where(ChatMessage.user_id == user.id))
    events = await s.scalars(select(EventLog).where(EventLog.user_id == user.id))

    def rows(items, table):
        return [{c.name: iso(getattr(r, c.name)) for c in table.columns} for r in items]

    return {
        "profile": profile,
        "week_plans": rows(plans.all(), WeekPlan.__table__),
        "sessions": rows(sessions.all(), PlannedSession.__table__),
        "dinners": rows(meals.all(), PlannedMeal.__table__),
        "conversation": rows(chats.all(), ChatMessage.__table__),
        "events": rows(events.all(), EventLog.__table__),
    }


async def delete_user(s: AsyncSession, user: User) -> None:
    """Immediate and complete deletion of the user's data."""
    uid = user.id
    for model in (ChatMessage, PlannedMeal, PlannedSession, WeekPlan, EventLog):
        await s.execute(delete(model).where(model.user_id == uid))
    await s.delete(user)
    log_event(s, None, "user_deleted")  # no user id: only the count remains


# --- User-facing rendering (French) ------------------------------------------------------


def describe_session(ps: PlannedSession, ref: date | None = None) -> str:
    head = f"{day_label(ps.day, ref).capitalize()} à {ps.time.replace(':', 'h')}"
    return f"{head}\n{format_session(ps.exercises, ps.focus, total_minutes(ps.exercises))}"


def describe_week(sessions: list[PlannedSession], meals: list[PlannedMeal], ref: date | None = None) -> str:
    lib = get_library()
    lines = ["Séances"]
    planned = [p for p in sessions if p.status in ("planned", "done")]
    if not planned:
        lines.append("  aucune")
    for p in planned:
        mark = " (faite)" if p.status == "done" else ""
        minutes = total_minutes(p.exercises) if p.exercises else p.minutes
        lines.append(f"  • {day_label(p.day, ref)} {p.time.replace(':', 'h')} · {minutes} min{mark}")
    lines.append("Dîners")
    if not meals:
        lines.append("  aucun")
    for m in meals:
        r = lib.recipe(m.recipe_id)
        lines.append(f"  • {day_label(m.day, ref)} : {r.name if r else m.recipe_id}")
    return "\n".join(lines)

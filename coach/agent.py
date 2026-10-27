"""Conversational agent: Claude understands and phrases, the tools decide.

The LLM never invents an exercise, a recipe or a training load: it calls tools that rely
on the controlled library and the progression rules. Prompts and tool descriptions are in
English; Claude is instructed to always answer users in French.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import AsyncSession

from coach import planner, services
from coach.config import get_settings
from coach.db import User, log_event
from coach.library import get_library
from coach.planner import format_grocery, format_recipe
from coach.services import ServiceError
from coach.timeutil import day_label, now, today
from coach.workouts import FOCUS_LABELS, total_minutes

log = logging.getLogger(__name__)

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

SYSTEM_PROMPT = """You are a personal sport and nutrition assistant used by students, PhD candidates \
and staff of INP Grenoble during a pilot. You talk with them on a messaging app (Telegram).

Language: always write to the user in French, using "tu". Tool results may contain French text \
meant for the user; you can pass it on or rephrase it.

Your mission: turn the person's goals into simple actions that fit their daily life. They must always \
know what to do today without having to think about it.

Strict rules:
- NEVER create an exercise, a workout, a recipe or a grocery list yourself. Always use the tools, which \
rely on a reviewed library. You may rephrase their output.
- The difficulty (load) is computed by rules. Never change it on your own initiative.
- Give no medical advice, no diagnosis, and no numeric calorie or weight-loss target. If the person \
mentions pain, injury, a medical condition, pregnancy or an eating disorder, recommend seeing a \
professional and only offer gentle mobility.
- Warm, direct tone, never guilt-inducing. A skipped session is not a failure.
- Short messages: 2 to 6 lines, except when showing a session, a recipe, a plan or a grocery list. \
No markdown headings or bold; simple dashes or bullets are enough.
- Ask one question at a time.
- Technical identifiers (session ids) are for the tools only: never show them.

Onboarding (when the profile is incomplete): in under 5 minutes of natural conversation, find out \
1) the goal, 2) the target number of sessions per week, 3) the usual time slots (days, time, duration), \
4) the equipment (none, resistance band, dumbbells, a sturdy chair), 5) the level, 6) foods to exclude \
or disliked, and how many dinners to plan. Save as you go with update_profile and update_preferences. \
As soon as the goal and at least one slot are known and the other questions have been asked, call \
finish_onboarding, then present what to do today or at the next slot.

Last-minute changes: when the person reports a conflict, offer to shorten, postpone or cancel, then \
apply their choice with reschedule_session. If the request is clear ("move it to tomorrow"), apply it directly.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "update_profile",
        "description": "Save or change the profile: goal, sessions per week, level, equipment, usual "
        "time slots, number of dinners, message times. Only the fields provided change. "
        "`slots` replaces all slots.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_text": {"type": "string", "description": "The goal in the person's own words."},
                "sessions_per_week": {"type": "integer", "minimum": 1, "maximum": 6},
                "level": {"type": "string", "enum": ["beginner", "intermediate", "advanced"]},
                "equipment": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["band", "dumbbells", "bench"]},
                    "description": "Equipment available besides bodyweight. bench = a sturdy chair.",
                },
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "day": {"type": "integer", "minimum": 0, "maximum": 6, "description": "0 = Monday"},
                            "time": {"type": "string", "description": "HH:MM"},
                            "minutes": {"type": "integer", "minimum": 10, "maximum": 90},
                        },
                        "required": ["day", "time", "minutes"],
                    },
                },
                "dinners_per_week": {"type": "integer", "minimum": 0, "maximum": 7},
                "brief_time": {"type": "string", "description": "Time of the morning message, HH:MM"},
                "planning_time": {"type": "string", "description": "Time of the Sunday planning, HH:MM"},
            },
        },
    },
    {
        "name": "update_preferences",
        "description": "Add or remove preferences: excluded foods (never suggested: diet, allergy, "
        "religion...), disliked foods (avoided), ingredients in stock, liked or avoided exercises "
        "(by id). Upcoming dinners that contain an excluded food are replaced automatically. "
        "Food terms can be given in French as the user wrote them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "add_exclusions": {"type": "array", "items": {"type": "string"}},
                "remove_exclusions": {"type": "array", "items": {"type": "string"}},
                "add_dislikes": {"type": "array", "items": {"type": "string"}},
                "remove_dislikes": {"type": "array", "items": {"type": "string"}},
                "add_pantry": {"type": "array", "items": {"type": "string"}},
                "remove_pantry": {"type": "array", "items": {"type": "string"}},
                "like_exercises": {"type": "array", "items": {"type": "string"}},
                "avoid_exercises": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "finish_onboarding",
        "description": "Finish onboarding and create the current week's plan. Returns what to do today.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_today",
        "description": "Session and dinner planned today (or at the next slot), with details.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_week",
        "description": "A week's plan (sessions and dinners) with session ids.",
        "input_schema": {
            "type": "object",
            "properties": {"week": {"type": "string", "enum": ["current", "next"]}},
            "required": ["week"],
        },
    },
    {
        "name": "plan_week",
        "description": "Create or recreate a week's plan from the usual slots and the reported "
        "constraints. Replaces the sessions and dinners of that week that are still ahead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "week": {"type": "string", "enum": ["current", "next"]},
                "constraints": {
                    "type": "array",
                    "description": "Dated constraints. available=false: no session that day. "
                    "max_minutes: maximum duration. time: new time. available=true + time + minutes: "
                    "extra one-off slot.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "YYYY-MM-DD"},
                            "available": {"type": "boolean"},
                            "max_minutes": {"type": "integer"},
                            "time": {"type": "string"},
                            "minutes": {"type": "integer"},
                        },
                        "required": ["date"],
                    },
                },
                "constraints_text": {"type": "string", "description": "The constraints in the person's own words."},
            },
            "required": ["week"],
        },
    },
    {
        "name": "reschedule_session",
        "description": "Adapt a planned session: shorten (to `minutes`), postpone (to `new_date`, "
        "optionally `new_time`), cancel. For postpone without `new_date`, the next free day of the "
        "week is used.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "integer"},
                "action": {"type": "string", "enum": ["shorten", "postpone", "cancel"]},
                "minutes": {"type": "integer"},
                "new_date": {"type": "string", "description": "YYYY-MM-DD"},
                "new_time": {"type": "string", "description": "HH:MM"},
            },
            "required": ["session_id", "action"],
        },
    },
    {
        "name": "record_feedback",
        "description": "Record how a completed session felt (easy, ok, hard) with an optional note. "
        "The rules adjust the difficulty of the next sessions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "integer"},
                "feedback": {"type": "string", "enum": ["easy", "ok", "hard"]},
                "note": {"type": "string"},
            },
            "required": ["session_id", "feedback"],
        },
    },
    {
        "name": "skip_session",
        "description": "Mark a session as skipped (no penalty).",
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "integer"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "swap_dinner",
        "description": "Replace the dinner planned on a given day, with a specific recipe (id) or an "
        "automatically chosen compatible alternative.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "recipe_id": {"type": "string"},
            },
            "required": ["date"],
        },
    },
    {
        "name": "get_recipe",
        "description": "Full recipe (ingredients and steps) for a day's dinner, or for a recipe id.",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string"}, "recipe_id": {"type": "string"}},
        },
    },
    {
        "name": "list_recipes",
        "description": "Recipes compatible with the person's exclusions (id, name, duration).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "quick_plan",
        "description": "An immediate session for the time available and a dinner doable with the listed "
        "ingredients. For \"tonight I have 20 minutes and this in the fridge\".",
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "minimum": 8, "maximum": 90},
                "ingredients": {"type": "array", "items": {"type": "string"}},
                "max_cooking_minutes": {"type": "integer"},
                "with_session": {"type": "boolean", "default": True},
            },
            "required": ["minutes"],
        },
    },
    {
        "name": "get_grocery_list",
        "description": "The week's grocery list, grouped by aisle.",
        "input_schema": {
            "type": "object",
            "properties": {"week": {"type": "string", "enum": ["current", "next"]}},
            "required": ["week"],
        },
    },
    {
        "name": "set_quiet_day",
        "description": "No automatic message on that day.",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["date"],
        },
    },
    {
        "name": "get_week_summary",
        "description": "Summary of the current week: sessions done, skipped, load trend.",
        "input_schema": {"type": "object", "properties": {}},
    },
]
TOOLS[-1] = {**TOOLS[-1], "cache_control": {"type": "ephemeral"}}


def _week_start(which: str) -> date:
    current = planner.week_start_of(today())
    return current + timedelta(days=7) if which == "next" else current


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ServiceError(f"Date invalide : {value} (format AAAA-MM-JJ).") from exc


def profile_context(user: User) -> str:
    """Per-turn context (not cached): date, profile, state."""
    t = today()
    slots = ", ".join(f"{WEEKDAY_NAMES[sl['day']]} {sl['time']} ({sl['minutes']} min)" for sl in user.slots) or "none"
    missing = services.missing_profile_fields(user)
    lines = [
        f"Today is {WEEKDAY_NAMES[t.weekday()]} {t.isoformat()}, local time {now().strftime('%H:%M')}.",
        f"First name: {user.first_name or 'unknown'}",
        f"Onboarding finished: {'yes' if user.is_onboarded else 'no'}"
        + (f" (missing: {', '.join(missing)})" if missing else ""),
        f"Goal: {user.goal_text or 'not set'}",
        f"Target sessions per week: {user.sessions_per_week} · level: {user.level} · "
        f"equipment: {', '.join(user.equipment) or 'bodyweight only'}",
        f"Usual slots: {slots}",
        f"Excluded foods: {', '.join(user.food_exclusions) or 'none'} · "
        f"disliked: {', '.join(user.food_dislikes) or 'none'} · in stock: {', '.join(user.pantry) or 'nothing'}",
        f"Dinners planned per week: {user.dinners_per_week}",
        f"Messages: brief at {user.brief_time}, Sunday planning at {user.planning_time}",
        f"Current load: {user.load_level}/10",
    ]
    if user.safety_hold:
        lines.append("WARNING: progression paused (pain or medical advice reported). "
                     "Gentle mobility only; remind them to see a professional. /reprendre lifts the pause.")
    return "\n".join(lines)


@dataclass
class ToolContext:
    session: AsyncSession
    user: User


async def run_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
    """Execute a tool call. The returned text goes back to the model; user-facing parts are French."""
    s, user, lib = ctx.session, ctx.user, get_library()
    t = today()

    if name == "update_profile":
        changed = await services.update_profile(s, user, **args)
        return f"Profile updated: {', '.join(changed) or 'nothing'}."

    if name == "update_preferences":
        def merged(current: list[str], add_key: str, remove_key: str) -> list[str]:
            values = set(current or [])
            values |= {v.strip().lower() for v in args.get(add_key, []) if v.strip()}
            values -= {v.strip().lower() for v in args.get(remove_key, [])}
            return sorted(values)

        unknown = [e for e in args.get("like_exercises", []) + args.get("avoid_exercises", [])
                   if e not in lib.exercises]
        fields = {
            "food_exclusions": merged(user.food_exclusions, "add_exclusions", "remove_exclusions"),
            "food_dislikes": merged(user.food_dislikes, "add_dislikes", "remove_dislikes"),
            "pantry": merged(user.pantry, "add_pantry", "remove_pantry"),
            "liked_exercises": sorted(set(user.liked_exercises or []) |
                                      {e for e in args.get("like_exercises", []) if e in lib.exercises}),
            "avoided_exercises": sorted(set(user.avoided_exercises or []) |
                                        {e for e in args.get("avoid_exercises", []) if e in lib.exercises}),
        }
        await services.update_profile(s, user, **fields)
        note = f" Unknown exercise ids ignored: {', '.join(unknown)}." if unknown else ""
        return "Preferences saved." + note

    if name == "finish_onboarding":
        await services.finish_onboarding(s, user)
        plan = await services.create_week_plan(s, user, planner.week_start_of(t))
        log_event(s, user.id, "planning_confirmed", week_start=plan.week_start.isoformat(), source="onboarding")
        return "Onboarding finished, this week's plan was created.\n" + await _today_text(ctx)

    if name == "get_today":
        return await _today_text(ctx)

    if name == "get_week":
        return await _week_text(ctx, _week_start(args.get("week", "current")))

    if name == "plan_week":
        ws = _week_start(args.get("week", "current"))
        plan = await services.create_week_plan(
            s, user, ws, args.get("constraints") or [], args.get("constraints_text", "")
        )
        log_event(s, user.id, "planning_confirmed", week_start=ws.isoformat(), source="agent")
        return (await _week_text(ctx, ws)) + "\n\nGrocery list:\n" + format_grocery(plan.grocery)

    if name == "reschedule_session":
        new_day = _parse_date(args.get("new_date"))
        action = args["action"]
        if action == "postpone" and new_day is None:
            ps0 = await services.get_session(s, user, int(args["session_id"]))
            new_day = await services.next_free_day(s, user, max(ps0.day, t))
            if new_day is None:
                return "No free day left this week: offer to shorten or cancel instead."
        ps = await services.reschedule(
            s, user, int(args["session_id"]), action,
            minutes=args.get("minutes"), new_day=new_day, new_time=args.get("new_time"),
        )
        if ps.status == "cancelled":
            return "Session cancelled."
        return "Session updated:\n" + services.describe_session(ps, t)

    if name == "record_feedback":
        out = await services.record_feedback(s, user, int(args["session_id"]), args["feedback"], args.get("note", ""))
        return feedback_result_text(out)

    if name == "skip_session":
        await services.skip_session(s, user, int(args["session_id"]))
        return "Session marked as skipped."

    if name == "swap_dinner":
        meal = await services.swap_meal(s, user, _parse_date(args["date"]), args.get("recipe_id"))
        recipe = lib.recipe(meal.recipe_id)
        return (f"New dinner for {meal.day.isoformat()}: {recipe.name} ({recipe.minutes} min). "
                "Grocery list updated.")

    if name == "get_recipe":
        recipe_id = args.get("recipe_id")
        if not recipe_id and args.get("date"):
            d = _parse_date(args["date"])
            meals = await services.meals_between(s, user, d, d + timedelta(days=1))
            if not meals:
                return f"No dinner planned on {d.isoformat()}."
            recipe_id = meals[0].recipe_id
        recipe = lib.recipe(recipe_id or "")
        return format_recipe(recipe) if recipe else "Unknown recipe."

    if name == "list_recipes":
        pool = planner.allowed_recipes(lib, user.food_exclusions, user.food_dislikes)
        return "\n".join(f"{r.id} · {r.name} · {r.minutes} min" for r in pool)

    if name == "quick_plan":
        parts = []
        if args.get("with_session", True):
            ps = await services.quick_session(s, user, int(args["minutes"]), t)
            parts.append("Session:\n" + services.describe_session(ps, t))
        recipe, missing = planner.quick_dinner(
            lib, args.get("ingredients", []) + list(user.pantry or []), user.food_exclusions,
            user.food_dislikes, args.get("max_cooking_minutes"),
        )
        if recipe:
            extra = f"\nIl manque : {', '.join(missing)}." if missing else "\nTu as tout ce qu'il faut."
            parts.append("Dinner:\n" + format_recipe(recipe) + extra)
        log_event(s, user.id, "quick_plan", recipe=recipe.id if recipe else None, missing=missing)
        return "\n\n".join(parts)

    if name == "get_grocery_list":
        ws = _week_start(args.get("week", "current"))
        plan = await services.get_week_plan(s, user, ws)
        if plan is None:
            return "No plan for that week yet."
        await services.refresh_grocery(s, user, plan)
        return format_grocery(plan.grocery)

    if name == "set_quiet_day":
        d = _parse_date(args["date"])
        user.quiet_dates = sorted({*user.quiet_dates, d.isoformat()})
        return f"No automatic message on {d.isoformat()}."

    if name == "get_week_summary":
        summary = await services.week_summary(s, user, planner.week_start_of(t))
        return summary_text(summary)

    raise ServiceError(f"Outil inconnu : {name}")


async def _today_text(ctx: ToolContext) -> str:
    s, user, t = ctx.session, ctx.user, today()
    lib = get_library()
    lines: list[str] = []
    sessions = [p for p in await services.sessions_between(s, user, t, t + timedelta(days=1)) if p.status == "planned"]
    if sessions:
        for p in sessions:
            lines.append(f"[session_id={p.id}] " + services.describe_session(p, t))
    else:
        upcoming = await services.upcoming_planned(s, user, t + timedelta(days=1))
        if upcoming:
            p = upcoming[0]
            lines.append(f"No session today. Next one: [session_id={p.id}] {day_label(p.day, t)} at "
                         f"{p.time} ({FOCUS_LABELS.get(p.focus, p.focus)}, {total_minutes(p.exercises)} min).")
        else:
            lines.append("No session planned for the rest of the week.")
    meals = await services.meals_between(s, user, t, t + timedelta(days=1))
    if meals:
        r = lib.recipe(meals[0].recipe_id)
        missing = [i.name for i in r.ingredients if not i.basic and not planner.in_pantry(i.name, user.pantry)]
        lines.append(f"Dinner: {r.name} ({r.minutes} min). Ingredients needed: {', '.join(missing) or 'none'}.")
    else:
        lines.append("No dinner planned today.")
    return "\n\n".join(lines)


async def _week_text(ctx: ToolContext, ws: date) -> str:
    s, user = ctx.session, ctx.user
    end = ws + timedelta(days=7)
    sessions = await services.sessions_between(s, user, ws, end)
    meals = await services.meals_between(s, user, ws, end)
    ids = "\n".join(f"session_id={p.id} -> {p.day.isoformat()} {p.time} ({p.status})" for p in sessions)
    return services.describe_week(sessions, meals, today()) + ("\n\nIds:\n" + ids if ids else "")


def feedback_result_text(out: services.FeedbackOutcome) -> str:
    """User-facing (French): also sent directly by the bot after a feedback button."""
    reason = {
        "harder": "Deux séances faciles d'affilée : les prochaines seront un peu plus soutenues.",
        "easier": "Deux séances dures d'affilée : les prochaines seront allégées.",
        "stable": "Le niveau des prochaines séances ne change pas.",
    }.get(out.decision.reason, "")
    return f"Retour enregistré. {reason}"


def summary_text(summary: services.WeekSummary) -> str:
    """User-facing (French) weekly summary line."""
    if summary.planned == 0:
        return "Aucune séance cette semaine."
    trend = ""
    if summary.load_start is not None and summary.load_end != summary.load_start:
        trend = " La charge monte doucement." if summary.load_end > summary.load_start else " La charge a été allégée."
    return (f"{summary.done} séance(s) faite(s) sur {summary.planned} prévue(s)"
            f"{f', {summary.skipped} sautée(s)' if summary.skipped else ''}.{trend}")


class Agent:
    def __init__(self, client: AsyncAnthropic | None = None) -> None:
        settings = get_settings()
        self.settings = settings
        self.client = client or AsyncAnthropic(api_key=settings.anthropic_api_key or None)

    async def reply(self, s: AsyncSession, user: User, text: str) -> str:
        """Answer a user message, calling whatever tools are needed."""
        history = await services.recent_chat(s, user)
        messages: list[dict[str, Any]] = [{"role": m.role, "content": m.content} for m in history]
        # The API requires the conversation to start with a user message.
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        messages.append({"role": "user", "content": text})
        await services.add_chat(s, user, "user", text)

        system = [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": profile_context(user)},
        ]
        ctx = ToolContext(s, user)
        final_text = ""
        for _ in range(self.settings.llm_max_tool_rounds):
            response = await self.client.messages.create(
                model=self.settings.llm_model,
                max_tokens=self.settings.llm_max_tokens,
                system=system,
                tools=TOOLS,
                messages=messages,
            )
            self._log_usage(s, user, response)
            texts = [b.text for b in response.content if b.type == "text"]
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if texts:
                final_text = "\n".join(texts).strip()
            if response.stop_reason != "tool_use" or not tool_uses:
                break
            messages.append({"role": "assistant", "content": [_block_param(b) for b in response.content
                                                              if b.type in ("text", "tool_use")]})
            results = []
            for tu in tool_uses:
                try:
                    output = await run_tool(ctx, tu.name, dict(tu.input or {}))
                    is_error = False
                except ServiceError as exc:
                    output, is_error = str(exc), True
                except Exception:  # internal error: let the agent apologize gracefully
                    log.exception("Tool %s failed", tu.name)
                    output, is_error = "Internal tool error, try again later.", True
                log_event(s, user.id, "tool_call", tool=tu.name, ok=not is_error)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": output, "is_error": is_error})
            messages.append({"role": "user", "content": results})
            # If the last round ends on a tool call, call again to get the final text.
            final_text = ""

        if not final_text:
            final_text = "Désolé, je n'ai pas réussi à traiter ta demande. Tu peux reformuler ?"
        await services.add_chat(s, user, "assistant", final_text)
        return final_text

    def _log_usage(self, s: AsyncSession, user: User, response) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        log_event(
            s, user.id, "llm_usage",
            model=getattr(response, "model", self.settings.llm_model),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )


def _block_param(block) -> dict[str, Any]:
    """Return a response block in the minimal shape the API accepts as input."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}


def dump_tools() -> str:
    """Debugging helper: the tool schemas as JSON."""
    return json.dumps(TOOLS, ensure_ascii=False, indent=2)

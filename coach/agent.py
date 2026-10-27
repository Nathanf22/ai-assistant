"""Agent conversationnel : Claude comprend et formule, les outils décident.

Le LLM n'invente ni exercice, ni recette, ni charge : il appelle des outils qui
s'appuient sur la bibliothèque contrôlée et les règles de progression.
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
from coach.timeutil import WEEKDAYS, day_label, now, today
from coach.workouts import FOCUS_LABELS, total_minutes

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Tu es un assistant personnel de sport et d'alimentation, utilisé par des étudiants, \
doctorants et membres du personnel de l'INP dans le cadre d'un pilote. Tu écris en français, en tutoyant, \
sur une messagerie (Telegram).

Ta mission : transformer les objectifs de la personne en actions simples adaptées à son quotidien. \
Elle doit toujours savoir quoi faire aujourd'hui, sans réfléchir.

Règles impératives :
- Tu ne crées JAMAIS toi-même un exercice, une séance, une recette ou une liste de courses. \
Tu utilises toujours les outils, qui s'appuient sur une bibliothèque validée. Tu peux reformuler leur résultat.
- La difficulté (charge) est calculée par des règles. Tu ne la modifies jamais de ta propre initiative.
- Tu ne donnes aucun conseil médical, aucun diagnostic, aucun objectif calorique ou de perte de poids \
chiffré. En cas de douleur, blessure, pathologie, grossesse ou trouble alimentaire, tu recommandes de \
consulter un professionnel et tu ne proposes que de la mobilité douce.
- Ton chaleureux, direct, jamais culpabilisant. Une séance sautée n'est pas un échec.
- Messages courts : 2 à 6 lignes, sauf pour afficher une séance, une recette, un planning ou une liste \
de courses. Pas de titres markdown ni de gras ; des tirets ou des puces simples suffisent.
- Une seule question à la fois.
- Les identifiants techniques (id de séance) servent aux outils : ne les montre jamais.

Onboarding (si le profil est incomplet) : en moins de 5 minutes et en conversation naturelle, découvre \
1) l'objectif, 2) le nombre de séances visé par semaine, 3) les créneaux habituels (jours, heure, durée), \
4) le matériel (aucun, élastique, haltères, chaise solide), 5) le niveau, 6) les aliments exclus ou \
détestés et le nombre de dîners à planifier. Enregistre au fur et à mesure avec update_profile et \
update_preferences. Dès que l'objectif et au moins un créneau sont connus et que les autres questions \
ont été posées, appelle finish_onboarding, puis présente ce qu'il y a à faire aujourd'hui ou au prochain créneau.

Imprévus : quand la personne signale un contretemps, propose de raccourcir, décaler ou annuler, puis \
applique son choix avec reschedule_session. Si la demande est claire (« décale à demain »), applique directement.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "update_profile",
        "description": "Enregistre ou modifie le profil : objectif, séances par semaine, niveau, matériel, "
        "créneaux habituels, nombre de dîners, heures des messages. Seuls les champs fournis changent. "
        "`slots` remplace tous les créneaux.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_text": {"type": "string", "description": "Objectif dans les mots de la personne."},
                "sessions_per_week": {"type": "integer", "minimum": 1, "maximum": 6},
                "level": {"type": "string", "enum": ["debutant", "intermediaire", "avance"]},
                "equipment": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["elastique", "halteres", "banc"]},
                    "description": "Matériel disponible en plus du poids du corps. banc = chaise solide.",
                },
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "day": {"type": "integer", "minimum": 0, "maximum": 6, "description": "0 = lundi"},
                            "time": {"type": "string", "description": "HH:MM"},
                            "minutes": {"type": "integer", "minimum": 10, "maximum": 90},
                        },
                        "required": ["day", "time", "minutes"],
                    },
                },
                "dinners_per_week": {"type": "integer", "minimum": 0, "maximum": 7},
                "brief_time": {"type": "string", "description": "Heure du message du matin, HH:MM"},
                "planning_time": {"type": "string", "description": "Heure du planning du dimanche, HH:MM"},
            },
        },
    },
    {
        "name": "update_preferences",
        "description": "Ajoute ou retire des préférences : aliments exclus (jamais proposés : régime, "
        "allergie, religion…), aliments détestés (évités), ingrédients en stock, exercices appréciés ou à éviter "
        "(par leur id). Les dîners à venir contenant un aliment exclu sont remplacés automatiquement.",
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
        "description": "Termine l'onboarding et crée le planning de la semaine en cours. "
        "Renvoie ce qu'il y a à faire aujourd'hui.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_today",
        "description": "Séance et dîner prévus aujourd'hui (ou au prochain créneau), avec le détail.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_week",
        "description": "Planning d'une semaine (séances et dîners) avec les id de séance.",
        "input_schema": {
            "type": "object",
            "properties": {"week": {"type": "string", "enum": ["current", "next"]}},
            "required": ["week"],
        },
    },
    {
        "name": "plan_week",
        "description": "Crée ou recrée le planning d'une semaine à partir des créneaux habituels et des "
        "contraintes signalées. Remplace les séances et dîners encore à venir de cette semaine.",
        "input_schema": {
            "type": "object",
            "properties": {
                "week": {"type": "string", "enum": ["current", "next"]},
                "constraints": {
                    "type": "array",
                    "description": "Contraintes datées. available=false : pas de séance ce jour. "
                    "max_minutes : durée maximale. time : nouvelle heure. available=true + time + minutes : "
                    "créneau exceptionnel ajouté.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "AAAA-MM-JJ"},
                            "available": {"type": "boolean"},
                            "max_minutes": {"type": "integer"},
                            "time": {"type": "string"},
                            "minutes": {"type": "integer"},
                        },
                        "required": ["date"],
                    },
                },
                "constraints_text": {"type": "string", "description": "Contraintes dans les mots de la personne."},
            },
            "required": ["week"],
        },
    },
    {
        "name": "reschedule_session",
        "description": "Adapte une séance prévue : shorten (raccourcir à `minutes`), postpone (décaler à "
        "`new_date`, éventuellement `new_time`), cancel (annuler). Sans `new_date` pour postpone, "
        "le prochain jour libre de la semaine est choisi.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "integer"},
                "action": {"type": "string", "enum": ["shorten", "postpone", "cancel"]},
                "minutes": {"type": "integer"},
                "new_date": {"type": "string", "description": "AAAA-MM-JJ"},
                "new_time": {"type": "string", "description": "HH:MM"},
            },
            "required": ["session_id", "action"],
        },
    },
    {
        "name": "record_feedback",
        "description": "Enregistre le ressenti après une séance faite (easy, ok, hard) avec une note libre. "
        "La difficulté des séances suivantes est ajustée par les règles.",
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
        "description": "Marque une séance comme sautée (aucune pénalité).",
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "integer"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "swap_dinner",
        "description": "Remplace le dîner prévu un jour donné, par une recette précise (id) ou une "
        "alternative compatible choisie automatiquement.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "AAAA-MM-JJ"},
                "recipe_id": {"type": "string"},
            },
            "required": ["date"],
        },
    },
    {
        "name": "get_recipe",
        "description": "Recette complète (ingrédients et étapes) du dîner d'un jour, ou d'un id de recette.",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string"}, "recipe_id": {"type": "string"}},
        },
    },
    {
        "name": "list_recipes",
        "description": "Recettes compatibles avec les exclusions de la personne (id, nom, durée).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "quick_plan",
        "description": "Séance immédiate de la durée disponible et dîner faisable avec les ingrédients cités. "
        "Pour « ce soir j'ai 20 minutes et ça dans le frigo ».",
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
        "description": "Liste de courses de la semaine, regroupée par rayon.",
        "input_schema": {
            "type": "object",
            "properties": {"week": {"type": "string", "enum": ["current", "next"]}},
            "required": ["week"],
        },
    },
    {
        "name": "set_quiet_day",
        "description": "Aucun message automatique ce jour-là.",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "AAAA-MM-JJ"}},
            "required": ["date"],
        },
    },
    {
        "name": "get_week_summary",
        "description": "Bilan de la semaine en cours : séances faites, sautées, évolution de la charge.",
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
    """Contexte variable (non mis en cache) : date, profil, état."""
    t = today()
    slots = ", ".join(f"{WEEKDAYS[sl['day']]} {sl['time']} ({sl['minutes']} min)" for sl in user.slots) or "aucun"
    missing = services.missing_profile_fields(user)
    lines = [
        f"Nous sommes {WEEKDAYS[t.weekday()]} {t.isoformat()}, il est {now().strftime('%H:%M')}.",
        f"Prénom : {user.first_name or 'inconnu'}",
        f"Onboarding terminé : {'oui' if user.is_onboarded else 'non'}"
        + (f" (manque : {', '.join(missing)})" if missing else ""),
        f"Objectif : {user.goal_text or 'non renseigné'}",
        f"Séances visées par semaine : {user.sessions_per_week} · niveau : {user.level} · "
        f"matériel : {', '.join(user.equipment) or 'poids du corps'}",
        f"Créneaux habituels : {slots}",
        f"Aliments exclus : {', '.join(user.food_exclusions) or 'aucun'} · "
        f"détestés : {', '.join(user.food_dislikes) or 'aucun'} · en stock : {', '.join(user.pantry) or 'rien'}",
        f"Dîners planifiés par semaine : {user.dinners_per_week}",
        f"Messages : brief à {user.brief_time}, planning le dimanche à {user.planning_time}",
        f"Charge actuelle : {user.load_level}/10",
    ]
    if user.safety_hold:
        lines.append("ATTENTION : progression suspendue (douleur ou avis médical signalé). "
                     "Uniquement mobilité douce ; rappeler de consulter. /reprendre lève la suspension.")
    return "\n".join(lines)


@dataclass
class ToolContext:
    session: AsyncSession
    user: User


async def run_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
    s, user, lib = ctx.session, ctx.user, get_library()
    t = today()

    if name == "update_profile":
        changed = await services.update_profile(s, user, **args)
        return f"Profil mis à jour : {', '.join(changed) or 'rien'}."

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
        note = f" Exercices inconnus ignorés : {', '.join(unknown)}." if unknown else ""
        return "Préférences enregistrées." + note

    if name == "finish_onboarding":
        await services.finish_onboarding(s, user)
        plan = await services.create_week_plan(s, user, planner.week_start_of(t))
        log_event(s, user.id, "planning_confirmed", week_start=plan.week_start.isoformat(), source="onboarding")
        return "Onboarding terminé, planning de la semaine créé.\n" + await _today_text(ctx)

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
        return (await _week_text(ctx, ws)) + "\n\nListe de courses :\n" + format_grocery(plan.grocery)

    if name == "reschedule_session":
        new_day = _parse_date(args.get("new_date"))
        action = args["action"]
        if action == "postpone" and new_day is None:
            ps0 = await services.get_session(s, user, int(args["session_id"]))
            new_day = await services.next_free_day(s, user, max(ps0.day, t))
            if new_day is None:
                return "Aucun jour libre d'ici la fin de la semaine : proposer de raccourcir ou d'annuler."
        ps = await services.reschedule(
            s, user, int(args["session_id"]), action,
            minutes=args.get("minutes"), new_day=new_day, new_time=args.get("new_time"),
        )
        if ps.status == "cancelled":
            return "Séance annulée."
        return "Séance mise à jour :\n" + services.describe_session(ps, t)

    if name == "record_feedback":
        out = await services.record_feedback(s, user, int(args["session_id"]), args["feedback"], args.get("note", ""))
        return feedback_result_text(out)

    if name == "skip_session":
        await services.skip_session(s, user, int(args["session_id"]))
        return "Séance marquée comme sautée."

    if name == "swap_dinner":
        meal = await services.swap_meal(s, user, _parse_date(args["date"]), args.get("recipe_id"))
        recipe = lib.recipe(meal.recipe_id)
        return f"Nouveau dîner {day_label(meal.day, t)} : {recipe.name} ({recipe.minutes} min). Liste de courses mise à jour."

    if name == "get_recipe":
        recipe_id = args.get("recipe_id")
        if not recipe_id and args.get("date"):
            d = _parse_date(args["date"])
            meals = await services.meals_between(s, user, d, d + timedelta(days=1))
            if not meals:
                return f"Pas de dîner prévu {day_label(d, t)}."
            recipe_id = meals[0].recipe_id
        recipe = lib.recipe(recipe_id or "")
        return format_recipe(recipe) if recipe else "Recette inconnue."

    if name == "list_recipes":
        pool = planner.allowed_recipes(lib, user.food_exclusions, user.food_dislikes)
        return "\n".join(f"{r.id} · {r.name} · {r.minutes} min" for r in pool)

    if name == "quick_plan":
        parts = []
        if args.get("with_session", True):
            ps = await services.quick_session(s, user, int(args["minutes"]), t)
            parts.append("Séance :\n" + services.describe_session(ps, t))
        recipe, missing = planner.quick_dinner(
            lib, args.get("ingredients", []) + list(user.pantry or []), user.food_exclusions,
            user.food_dislikes, args.get("max_cooking_minutes"),
        )
        if recipe:
            extra = f"\nIl manque : {', '.join(missing)}." if missing else "\nTu as tout ce qu'il faut."
            parts.append("Dîner :\n" + format_recipe(recipe) + extra)
        log_event(s, user.id, "quick_plan", recipe=recipe.id if recipe else None, missing=missing)
        return "\n\n".join(parts)

    if name == "get_grocery_list":
        ws = _week_start(args.get("week", "current"))
        plan = await services.get_week_plan(s, user, ws)
        if plan is None:
            return "Pas encore de planning pour cette semaine."
        await services.refresh_grocery(s, user, plan)
        return format_grocery(plan.grocery)

    if name == "set_quiet_day":
        d = _parse_date(args["date"])
        user.quiet_dates = sorted({*user.quiet_dates, d.isoformat()})
        return f"Aucun message automatique {day_label(d, t)}."

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
            lines.append(f"Pas de séance aujourd'hui. Prochaine : [session_id={p.id}] {day_label(p.day, t)} à "
                         f"{p.time.replace(':', 'h')} ({FOCUS_LABELS.get(p.focus, p.focus)}, {total_minutes(p.exercises)} min).")
        else:
            lines.append("Aucune séance prévue d'ici la fin de la semaine.")
    meals = await services.meals_between(s, user, t, t + timedelta(days=1))
    if meals:
        r = lib.recipe(meals[0].recipe_id)
        missing = [i.name for i in r.ingredients if not i.basic and not planner.in_pantry(i.name, user.pantry)]
        lines.append(f"Dîner : {r.name} ({r.minutes} min). Ingrédients à avoir : {', '.join(missing) or 'aucun'}.")
    else:
        lines.append("Pas de dîner planifié aujourd'hui.")
    return "\n\n".join(lines)


async def _week_text(ctx: ToolContext, ws: date) -> str:
    s, user = ctx.session, ctx.user
    end = ws + timedelta(days=7)
    sessions = await services.sessions_between(s, user, ws, end)
    meals = await services.meals_between(s, user, ws, end)
    ids = "\n".join(f"session_id={p.id} → {p.day.isoformat()} {p.time} ({p.status})" for p in sessions)
    return services.describe_week(sessions, meals, today()) + ("\n\nIdentifiants :\n" + ids if ids else "")


def feedback_result_text(out: services.FeedbackOutcome) -> str:
    reason = {
        "harder": "Deux séances faciles d'affilée : les prochaines seront un peu plus soutenues.",
        "easier": "Deux séances dures d'affilée : les prochaines seront allégées.",
        "stable": "Le niveau des prochaines séances ne change pas.",
    }.get(out.decision.reason, "")
    return f"Retour enregistré. {reason}"


def summary_text(summary: services.WeekSummary) -> str:
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
        """Répond à un message de l'utilisateur, en appelant les outils nécessaires."""
        history = await services.recent_chat(s, user)
        messages: list[dict[str, Any]] = [{"role": m.role, "content": m.content} for m in history]
        # L'API exige que la conversation commence par un message utilisateur.
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
                except Exception:  # erreur interne : l'agent doit pouvoir s'excuser proprement
                    log.exception("Échec de l'outil %s", tu.name)
                    output, is_error = "Erreur interne de l'outil, réessayer plus tard.", True
                log_event(s, user.id, "tool_call", tool=tu.name, ok=not is_error)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": output, "is_error": is_error})
            messages.append({"role": "user", "content": results})
            # Si le dernier tour s'arrête sur un outil, on refait un appel pour obtenir le texte final.
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
    """Renvoie un bloc de réponse sous la forme minimale acceptée en entrée par l'API."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}


def dump_tools() -> str:
    """Aide au débogage : schéma des outils en JSON."""
    return json.dumps(TOOLS, ensure_ascii=False, indent=2)

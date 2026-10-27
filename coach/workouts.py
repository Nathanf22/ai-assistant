"""Génération de séances à partir de la bibliothèque (F4, US13)."""

from __future__ import annotations

import random
from dataclasses import dataclass

from coach.library import Exercise, Library
from coach.progression import volume

REST_SECONDS = 40
SECONDS_PER_REP = 3
TRANSITION_SECONDS = 15
MIN_MINUTES = 8

# Rotation des séances dans la semaine.
FOCUS_ROTATION = ("full_body", "bas_gainage", "haut_cardio")
FOCUS_TAGS = {
    "full_body": {"jambes", "haut", "gainage"},
    "bas_gainage": {"jambes", "gainage"},
    "haut_cardio": {"haut", "cardio", "gainage"},
    "mobilite": {"mobilite"},
}
FOCUS_ORDER = {
    "full_body": ["jambes", "haut", "gainage"],
    "bas_gainage": ["jambes", "gainage", "jambes"],
    "haut_cardio": ["haut", "cardio", "gainage"],
}
FOCUS_LABELS = {
    "full_body": "Corps entier",
    "bas_gainage": "Jambes et gainage",
    "haut_cardio": "Haut du corps et cardio",
    "mobilite": "Mobilité douce",
}


@dataclass(frozen=True)
class SessionRequest:
    minutes: int
    focus: str
    load: int
    level: int  # 1 débutant, 2 intermédiaire, 3 avancé
    equipment: frozenset[str]
    liked: frozenset[str] = frozenset()
    avoided: frozenset[str] = frozenset()
    low_impact: bool = False
    seed: str = ""


def _available(ex: Exercise, req: SessionRequest) -> bool:
    if ex.id in req.avoided:
        return False
    if ex.equipment != "aucun" and ex.equipment not in req.equipment:
        return False
    if ex.level > req.level:
        return False
    if req.low_impact and ex.impact == "high":
        return False
    return True


def _item(ex: Exercise, sets: int, mult: float, rest: int = REST_SECONDS) -> dict:
    amount = max(1, round(ex.base * mult))
    if ex.mode == "time":
        amount = max(10, int(5 * round(amount / 5)))
    item = {"id": ex.id, "name": ex.name, "sets": sets, "rest": rest, "cue": ex.cue}
    item["seconds" if ex.mode == "time" else "reps"] = amount
    return item


def item_seconds(item: dict) -> int:
    work = item.get("seconds") or item.get("reps", 0) * SECONDS_PER_REP
    sets = item["sets"]
    return sets * work + (sets - 1) * item["rest"] + TRANSITION_SECONDS


def total_minutes(items: list[dict]) -> int:
    return round(sum(item_seconds(i) for i in items) / 60)


def _pick(pool: list[Exercise], k: int, rng: random.Random, liked: frozenset[str]) -> list[Exercise]:
    """Tirage sans remise, les exercices appréciés ont trois fois plus de chances."""
    chosen: list[Exercise] = []
    pool = list(pool)
    while pool and len(chosen) < k:
        weights = [3 if e.id in liked else 1 for e in pool]
        ex = rng.choices(pool, weights=weights, k=1)[0]
        chosen.append(ex)
        pool.remove(ex)
    return chosen


def build_session(lib: Library, req: SessionRequest) -> list[dict]:
    """Construit une séance : échauffement, bloc principal en circuit, retour au calme."""
    minutes = max(MIN_MINUTES, req.minutes)
    rng = random.Random(f"{req.seed}:{req.focus}:{minutes}:{req.load}")
    budget = minutes * 60
    exercises = [e for e in lib.exercises.values() if _available(e, req)]
    if req.focus == "mobilite":  # séance douce : jamais de sauts
        exercises = [e for e in exercises if e.impact == "low"]

    warmups = [e for e in exercises if e.role == "warmup"]
    cooldowns = [e for e in exercises if e.role == "cooldown"]
    warm = [_item(e, 1, 1.0, rest=0) for e in _pick(warmups, 2 if minutes < 25 else 3, rng, req.liked)]
    cool = [_item(e, 1, 1.0, rest=0) for e in _pick(cooldowns, 1 if minutes < 20 else 2, rng, req.liked)]
    budget -= sum(item_seconds(i) for i in warm + cool)

    if req.focus == "mobilite":
        # Séance douce : uniquement mobilité et retour au calme, à faible charge.
        taken = {i["id"] for i in warm + cool}
        soft = [e for e in exercises if "mobilite" in e.focus and e.id not in taken]
        main = []
        for ex in _pick(soft, len(soft), rng, req.liked):
            item = _item(ex, 2, 1.0, rest=15)
            if item_seconds(item) > budget:
                continue
            main.append(item)
            budget -= item_seconds(item)
        return warm + main + cool

    base_sets, mult = volume(req.load)
    tags = FOCUS_TAGS.get(req.focus, FOCUS_TAGS["full_body"])
    mains = [e for e in exercises if e.role == "main" and set(e.focus) & tags]
    target = max_main_exercises(minutes)

    # 1. Choisir les exercices en tournant entre les groupes du focus (le premier groupe
    #    nommé, par exemple les jambes, passe avant le gainage).
    chosen: list[Exercise] = []
    order = FOCUS_ORDER.get(req.focus, sorted(tags))
    while len(chosen) < target:
        added = False
        for tag in order:
            if len(chosen) >= target:
                break
            candidates = [e for e in mains if e.focus[0] == tag and e not in chosen]
            candidates = candidates or [e for e in mains if tag in e.focus and e not in chosen]
            picked = _pick(candidates, 1, rng, req.liked)
            chosen += picked
            added = added or bool(picked)
        if not added:
            break

    # 2. Ajuster le nombre de séries au temps disponible (jusqu'à +2 tours).
    def block(sets: int, exs: list[Exercise]) -> list[dict]:
        return [_item(e, sets, mult) for e in exs]

    def cost(items: list[dict]) -> int:
        return sum(item_seconds(i) for i in items)

    main = block(base_sets, chosen)
    while len(main) > 2 and cost(main) > budget:  # trop long : retirer des exercices
        chosen = chosen[:-1]
        main = block(base_sets, chosen)
    while cost(main) > budget and main[0]["sets"] > 1:  # très court : moins de séries
        main = block(main[0]["sets"] - 1, chosen)
    for extra in (1, 2):
        longer = block(base_sets + extra, chosen)
        if cost(longer) <= budget:
            main = longer
    return warm + main + cool


def max_main_exercises(minutes: int) -> int:
    return 3 if minutes < 15 else 4 if minutes < 25 else 5 if minutes < 40 else 6


def format_session(items: list[dict], focus: str, minutes: int) -> str:
    lines = [f"{FOCUS_LABELS.get(focus, focus)} · environ {minutes} min", ""]
    for n, it in enumerate(items, 1):
        amount = f"{it['seconds']} s" if "seconds" in it else f"{it['reps']} rép."
        sets = f"{it['sets']} × {amount}" if it["sets"] > 1 else amount
        rest = f", repos {it['rest']} s" if it["sets"] > 1 and it["rest"] else ""
        lines.append(f"{n}. {it['name']} — {sets}{rest}")
        lines.append(f"   {it['cue']}")
    return "\n".join(lines)

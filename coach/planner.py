"""Pure planning (no database): session slots, dinners, grocery list.

Every function is deterministic for a given seed, which makes it testable and reproducible
in the pilot's event log.
"""

from __future__ import annotations

import itertools
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from coach.library import AISLES, Library, Recipe, normalize, recipe_violates

QUICK_RECIPE_MINUTES = 20


@dataclass(frozen=True)
class Slot:
    day: date
    time: str
    minutes: int


def week_start_of(d: date) -> date:
    """Monday of the week containing `d`."""
    return d - timedelta(days=d.weekday())


def next_week_start(d: date) -> date:
    return week_start_of(d) + timedelta(days=7)


# --- Session slots -------------------------------------------------------------


def candidate_slots(week_start: date, usual: list[dict], constraints: list[dict]) -> list[Slot]:
    """The week's usual slots, after applying the constraints the user reported.

    `usual`: [{"day": 0..6, "time": "18:30", "minutes": 30}]
    `constraints`: [{"date": "2026-11-05", "available": false}]
                   [{"date": "...", "max_minutes": 20}], [{"date": "...", "time": "20:00"}]
                   [{"date": "...", "available": true, "time": "10:00", "minutes": 45}] (extra slot)
    """
    by_date: dict[date, dict] = {}
    for c in constraints:
        try:
            by_date[date.fromisoformat(c["date"])] = c
        except (KeyError, ValueError):
            continue

    slots: list[Slot] = []
    for u in usual:
        d = week_start + timedelta(days=int(u["day"]))
        c = by_date.get(d, {})
        if c.get("available") is False:
            continue
        minutes = int(u.get("minutes", 30))
        if c.get("max_minutes"):
            minutes = min(minutes, int(c["max_minutes"]))
        slots.append(Slot(d, c.get("time") or u.get("time", "18:30"), minutes))

    for d, c in by_date.items():
        in_week = week_start <= d < week_start + timedelta(days=7)
        if in_week and c.get("available") is True and c.get("time"):
            slots.append(Slot(d, c["time"], int(c.get("minutes", 30))))

    # One slot per day at most: keep the longest.
    best: dict[date, Slot] = {}
    for s in slots:
        if s.day not in best or s.minutes > best[s.day].minutes:
            best[s.day] = s
    return sorted(best.values(), key=lambda s: s.day)


def choose_sessions(slots: list[Slot], count: int) -> list[Slot]:
    """Pick `count` slots, spreading sessions as much as possible (no back-to-back days if avoidable)."""
    if count <= 0 or not slots:
        return []
    if len(slots) <= count:
        return slots

    def score(combo: tuple[Slot, ...]) -> tuple[int, int, int]:
        gaps = [(b.day - a.day).days for a, b in itertools.pairwise(combo)] or [7]
        return (min(gaps), sum(s.minutes for s in combo), -combo[0].day.toordinal())

    return list(max(itertools.combinations(slots, count), key=score))


# --- Dinners ---------------------------------------------------------------------

DINNER_DAY_ORDER = (0, 1, 2, 3, 4, 6, 5)  # Monday to Friday, then Sunday, then Saturday


def allowed_recipes(lib: Library, exclusions: list[str], dislikes: list[str]) -> list[Recipe]:
    """Compatible recipes. Exclusions are strict; dislikes are dropped if too few recipes remain."""
    safe = [r for r in lib.recipes.values() if not recipe_violates(r, exclusions)]
    liked = [r for r in safe if not recipe_violates(r, dislikes)]
    return liked if len(liked) >= 3 else safe


def choose_dinners(
    lib: Library,
    week_start: date,
    count: int,
    exclusions: list[str],
    dislikes: list[str],
    session_days: set[date],
    seed: str,
    avoid_recipes: set[str] = frozenset(),
    from_day: date | None = None,
) -> list[tuple[date, str]]:
    """Choose the week's dinners. With `from_day`, only the remaining days are planned
    (someone who starts on a Saturday still gets their weekend dinners)."""
    pool = allowed_recipes(lib, exclusions, dislikes)
    if not pool:
        return []
    rng = random.Random(f"{seed}:dinners:{week_start.isoformat()}")
    candidates = [week_start + timedelta(days=i) for i in DINNER_DAY_ORDER]
    if from_day is not None:
        candidates = [d for d in candidates if d >= from_day]
    days = sorted(candidates[: max(0, min(count, 7))])

    fresh = [r for r in pool if r.id not in avoid_recipes] or pool
    rng.shuffle(fresh)
    quick = [r for r in fresh if r.minutes <= QUICK_RECIPE_MINUTES + 5]

    plan: list[tuple[date, str]] = []
    used: set[str] = set()
    for d in days:
        source = quick if d in session_days else fresh
        choice = next((r for r in source if r.id not in used), None)
        if choice is None:
            choice = next((r for r in fresh if r.id not in used), None)
        if choice is None:  # library exhausted: allow a repeat
            choice = fresh[len(plan) % len(fresh)]
        used.add(choice.id)
        plan.append((d, choice.id))
    return plan


def swap_candidates(
    lib: Library, current_id: str, exclusions: list[str], dislikes: list[str], taken: set[str]
) -> list[Recipe]:
    pool = allowed_recipes(lib, exclusions, dislikes)
    return [r for r in pool if r.id != current_id and r.id not in taken]


# --- Grocery list ------------------------------------------------------------------


def _round_qty(qty: float, unit: str) -> float:
    if unit == "piece":
        return float(math.ceil(qty - 1e-9))
    if unit in ("g", "ml"):
        return float(max(10, 10 * math.ceil(qty / 10 - 1e-9)))
    return round(qty, 1)


# French words that start a compound food name ("lait de coco" is not "lait").
_COMPOUND_LINKS = {"de", "d'", "au", "aux", "a", "en"}


def _tokens(text: str) -> list[str]:
    words = normalize(text).replace("d'", "d' ").replace("-", " ").split()
    return [w[:-1] if len(w) > 3 and w.endswith(("s", "x")) else w for w in words]


def in_pantry(name: str, pantry: list[str]) -> bool:
    """True if an item in stock covers the ingredient.

    The stock item must be the beginning of the ingredient name, word by word: "riz" covers
    "riz basmati" but not "nouilles de riz"; "lait" does not cover "lait de coco" (compound
    name); "tomates cerises" does not cover "tomate". Singular and plural are equivalent.
    """
    ingredient = _tokens(name)
    for item in pantry:
        have = _tokens(item)
        if not have or len(have) > len(ingredient) or ingredient[: len(have)] != have:
            continue
        rest = ingredient[len(have):]
        if not rest or rest[0] not in _COMPOUND_LINKS:
            return True
    return False


def grocery_list(lib: Library, meals: list[tuple[str, int]], pantry: list[str]) -> list[dict]:
    """Aggregate the meals' ingredients, drop staples and stock, group by aisle (US8)."""
    totals: dict[tuple[str, str], dict] = {}
    for recipe_id, servings in meals:
        recipe = lib.recipe(recipe_id)
        if recipe is None:
            continue
        for ing in recipe.ingredients:
            if ing.basic or in_pantry(ing.name, pantry):
                continue
            key = (normalize(ing.name), ing.unit)
            entry = totals.setdefault(
                key, {"name": ing.name, "quantity": 0.0, "unit": ing.unit, "aisle": ing.aisle}
            )
            entry["quantity"] += ing.qty * servings
    items = []
    for entry in totals.values():
        entry["quantity"] = _round_qty(entry["quantity"], entry["unit"])
        items.append(entry)
    order = list(AISLES)
    items.sort(key=lambda i: (order.index(i["aisle"]) if i["aisle"] in order else 99, i["name"]))
    return items


# Unit id -> label shown to users (French).
UNIT_LABELS = {"tbsp": "c. à s.", "tsp": "c. à c."}


def _fmt_qty(qty: float, unit: str) -> str:
    q = int(qty) if float(qty).is_integer() else qty
    if unit == "piece":
        return f"× {q}"
    return f"{q} {UNIT_LABELS.get(unit, unit)}"


def format_grocery(items: list[dict]) -> str:
    """User-facing grocery list (French)."""
    if not items:
        return "Rien à acheter : tu as déjà tout."
    by_aisle: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by_aisle[it["aisle"]].append(it)
    lines: list[str] = []
    for aisle in AISLES:
        if aisle not in by_aisle:
            continue
        lines.append(AISLES[aisle])
        lines += [f"  • {it['name']} {_fmt_qty(it['quantity'], it['unit'])}" for it in by_aisle[aisle]]
    return "\n".join(lines)


# --- "Tonight I have 20 minutes and this in the fridge" (US11) ------------------------


def quick_dinner(
    lib: Library, have: list[str], exclusions: list[str], dislikes: list[str], max_minutes: int | None
) -> tuple[Recipe | None, list[str]]:
    """The most feasible recipe with what the user has. Returns the recipe and missing ingredients."""
    pool = allowed_recipes(lib, exclusions, dislikes)
    if max_minutes:
        pool = [r for r in pool if r.minutes <= max_minutes] or pool
    best: tuple[tuple[int, int, int], Recipe, list[str]] | None = None
    for r in pool:
        needed = [i for i in r.ingredients if not i.basic]
        missing = [i.name for i in needed if not in_pantry(i.name, have)]
        matched = len(needed) - len(missing)
        key = (-len(missing), matched, -r.minutes)
        if best is None or key > best[0]:
            best = (key, r, missing)
    if best is None:
        return None, []
    return best[1], best[2]


def format_recipe(recipe: Recipe, servings: int = 1) -> str:
    """User-facing recipe (French)."""
    lines = [f"{recipe.name} · {recipe.minutes} min", ""]
    for ing in recipe.ingredients:
        if ing.basic:
            continue
        lines.append(f"• {ing.name} {_fmt_qty(_round_qty(ing.qty * servings, ing.unit), ing.unit)}")
    lines.append("")
    lines += [f"{n}. {s}" for n, s in enumerate(recipe.steps, 1)]
    return "\n".join(lines)

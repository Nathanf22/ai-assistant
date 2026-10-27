"""Tests for the deterministic core: library, progression, sessions, planning, safety."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from coach import planner, progression, safety
from coach.library import (
    AISLES,
    EQUIPMENT,
    EXCLUSION_ALIASES,
    get_library,
    normalize,
    recipe_violates,
)
from coach.progression import DoneSession
from coach.workouts import FOCUS_ROTATION, SessionRequest, build_session, total_minutes

LIB = get_library()
MONDAY = date(2026, 11, 2)


# --- Library --------------------------------------------------------------------------


def test_library_is_consistent():
    assert len(LIB.exercises) >= 40 and len(LIB.recipes) >= 25
    for ex in LIB.exercises.values():
        assert ex.equipment in EQUIPMENT
        assert ex.role in {"warmup", "main", "cooldown"}
        assert ex.mode in {"reps", "time"} and ex.base > 0
        assert ex.cue
    for r in LIB.recipes.values():
        assert r.steps and r.ingredients and r.minutes <= 30
        assert all(i.aisle in AISLES for i in r.ingredients)


@pytest.mark.parametrize("term", ["végétarien", "vegan", "porc", "sans gluten", "lactose", "œufs", "poisson"])
def test_exclusion_terms_filter_tags(term):
    banned = EXCLUSION_ALIASES[normalize(term)]
    kept = [r for r in LIB.recipes.values() if not recipe_violates(r, [term])]
    assert kept, f"aucune recette compatible avec {term}"
    for r in kept:
        assert not (r.tags & banned)


def test_free_text_exclusion_matches_ingredient_names():
    kept = [r for r in LIB.recipes.values() if not recipe_violates(r, ["avocat"])]
    assert all("avocat" not in i.name for r in kept for i in r.ingredients)


# --- Progression (US16, US18) ------------------------------------------------------------


def _hist(*feedbacks, load=5):
    return [DoneSession(MONDAY + timedelta(days=i), load, fb) for i, fb in enumerate(feedbacks)]


def test_two_hard_in_a_row_lowers_load():
    assert progression.after_feedback(5, _hist("ok", "hard", "hard")).load == 4


def test_two_easy_in_a_row_raises_load():
    d = progression.after_feedback(5, _hist("easy", "easy"))
    assert d.load == 6 and d.reason == "harder"


def test_mixed_feedback_keeps_load():
    assert not progression.after_feedback(5, _hist("hard", "easy", "hard")).changed


def test_only_feedback_at_current_level_counts():
    history = _hist("hard", load=6) + _hist("hard", load=5)
    assert not progression.after_feedback(5, history).changed


def test_safety_hold_blocks_increase_not_decrease():
    assert not progression.after_feedback(5, _hist("easy", "easy"), safety_hold=True).changed
    assert progression.after_feedback(5, _hist("hard", "hard"), safety_hold=True).load == 4


def test_load_is_clamped():
    assert progression.after_feedback(1, _hist("hard", "hard", load=1)).load == 1
    assert progression.after_feedback(10, _hist("easy", "easy", load=10)).load == 10


def test_comeback_after_ten_days():
    last = DoneSession(MONDAY - timedelta(days=10), 6, "ok")
    d = progression.before_planning(6, last, MONDAY)
    assert d.load == 4 and d.reason == "comeback"
    recent = DoneSession(MONDAY - timedelta(days=9), 6, "ok")
    assert not progression.before_planning(6, recent, MONDAY).changed


def test_comeback_does_not_compound_on_replanning():
    last = DoneSession(MONDAY - timedelta(days=12), 8, "ok")
    load = 8
    for _ in range(3):  # /planifier, then the agent, then Sunday: same break
        load = progression.before_planning(load, last, MONDAY).load
    assert load == 6


def test_load_does_not_flip_after_a_change():
    """Regression: easy x2 (3->4), hard x2 (4->3), then a single "easy" must not raise the load."""
    history, load = [], 3
    for fb in ("easy", "easy", "hard", "hard", "easy"):
        history.append(DoneSession(MONDAY + timedelta(days=len(history)), load, fb))
        load = progression.after_feedback(load, history).load
    assert load == 3
    history.append(DoneSession(MONDAY + timedelta(days=len(history)), load, "easy"))
    assert progression.after_feedback(load, history).load == 4


# --- Sessions (US13) -----------------------------------------------------------------


@pytest.mark.parametrize("minutes", [10, 15, 20, 25, 30, 45])
@pytest.mark.parametrize("focus", [*FOCUS_ROTATION, "mobility"])
def test_session_fits_time_budget(minutes, focus):
    items = build_session(LIB, SessionRequest(minutes, focus, 5, 1, frozenset(), seed="t"))
    assert items
    assert total_minutes(items) <= minutes + 1


def test_session_respects_equipment_level_and_avoidance():
    req = SessionRequest(30, "full_body", 5, 1, frozenset({"band"}), avoided=frozenset({"squat"}), seed="x")
    items = build_session(LIB, req)
    for it in items:
        ex = LIB.exercise(it["id"])
        assert ex.equipment in {"none", "band"}
        assert ex.level == 1
        assert ex.id != "squat"


def test_low_impact_excludes_jumps():
    req = SessionRequest(30, "upper_cardio", 5, 3, frozenset(), low_impact=True, seed="x")
    assert all(LIB.exercise(i["id"]).impact == "low" for i in build_session(LIB, req))


def test_session_is_reproducible():
    req = SessionRequest(25, "full_body", 4, 2, frozenset({"dumbbells"}), seed="u1:2026-11-02")
    assert build_session(LIB, req) == build_session(LIB, req)


def test_higher_load_means_more_volume():
    def volume(load):
        items = build_session(LIB, SessionRequest(45, "full_body", load, 1, frozenset(), seed="v"))
        return sum(i["sets"] for i in items if i["sets"] > 1)

    assert volume(9) >= volume(2)


# --- Planning (US5–US8) -------------------------------------------------------------


USUAL = [
    {"day": 0, "time": "18:30", "minutes": 30},
    {"day": 1, "time": "18:30", "minutes": 30},
    {"day": 3, "time": "19:00", "minutes": 25},
    {"day": 5, "time": "10:00", "minutes": 45},
]


def test_sessions_only_in_declared_slots_and_spaced():
    slots = planner.candidate_slots(MONDAY, USUAL, [])
    chosen = planner.choose_sessions(slots, 3)
    assert len(chosen) == 3
    assert {c.day.weekday() for c in chosen} <= {0, 1, 3, 5}
    gaps = [(b.day - a.day).days for a, b in zip(chosen, chosen[1:], strict=False)]
    assert min(gaps) >= 2  # Monday, Thursday, Saturday rather than Monday-Tuesday


def test_constraints_remove_cap_and_add_slots():
    constraints = [
        {"date": "2026-11-05", "available": False},
        {"date": "2026-11-02", "max_minutes": 15, "time": "20:00"},
        {"date": "2026-11-08", "available": True, "time": "09:00", "minutes": 40},
    ]
    slots = {s.day: s for s in planner.candidate_slots(MONDAY, USUAL, constraints)}
    assert date(2026, 11, 5) not in slots
    assert slots[date(2026, 11, 2)].minutes == 15 and slots[date(2026, 11, 2)].time == "20:00"
    assert slots[date(2026, 11, 8)].minutes == 40


@pytest.mark.parametrize("exclusions", [["porc"], ["végétarien"], ["vegan"], ["gluten", "lactose"],
                                        ["poisson", "œufs"], ["avocat", "riz"]])
@pytest.mark.parametrize("seed", ["a", "b", "c", "d"])
def test_dinners_never_contain_excluded_food(exclusions, seed):
    """US3 acceptance criterion: no suggested dish contains an excluded food."""
    dinners = planner.choose_dinners(LIB, MONDAY, 7, exclusions, [], set(), seed)
    assert dinners
    for _, rid in dinners:
        assert not recipe_violates(LIB.recipe(rid), exclusions)


def test_dinners_avoid_repeats_and_dislikes():
    dinners = planner.choose_dinners(LIB, MONDAY, 5, [], ["champignons"], set(), "s")
    ids = [r for _, r in dinners]
    assert len(ids) == len(set(ids)) == 5
    assert "mushroom_risotto" not in ids


def test_grocery_aggregates_and_skips_basics_and_pantry():
    items = planner.grocery_list(LIB, [("spinach_feta_omelette", 1), ("shakshuka", 1)], pantry=["pain"])
    by_name = {i["name"]: i for i in items}
    assert by_name["œufs"]["quantity"] == 5
    assert "huile d'olive" not in by_name and "cumin" not in by_name
    assert not any("pain" in n for n in by_name)
    aisles = [i["aisle"] for i in items]
    assert aisles == sorted(aisles, key=list(AISLES).index)


def test_quick_dinner_prefers_what_you_have():
    recipe, missing = planner.quick_dinner(LIB, ["œufs", "épinards", "feta", "pain"], [], [], 20)
    assert recipe.id == "spinach_feta_omelette" and missing == []


# --- Safety guardrail (US20) ---------------------------------------------------------


@pytest.mark.parametrize("text,category", [
    ("mon genou tire depuis hier", "pain"),
    ("J'ai mal au dos", "pain"),
    ("je me suis fait une entorse", "pain"),
    ("j'ai une douleur dans la poitrine", "emergency"),
    ("je veux manger 900 kcal par jour", "eating"),
    ("je suis enceinte, je peux continuer ?", "medical"),
    ("j'ai envie d'en finir", "crisis"),
])
def test_safety_detects_risks(text, category):
    result = safety.check(text)
    assert result is not None and result.category == category


@pytest.mark.parametrize("text", [
    "réunion jusqu'à 19h", "c'était top", "je n'aime pas les champignons", "un dîner de 600 kcal ?",
    "ce soir j'ai 20 minutes", "pas mal du tout",
])
def test_safety_ignores_ordinary_messages(text):
    assert safety.check(text) is None


@pytest.mark.parametrize("have,ingredient,expected", [
    ("riz", "riz basmati", True),
    ("riz", "nouilles de riz", False),
    ("lait", "lait demi-écrémé", True),
    ("lait", "lait de coco", False),
    ("tomates cerises", "tomate", False),
    ("tomate", "tomates cerises", True),
    ("oeuf", "œufs", True),
    ("épinards", "épinards frais", True),
    ("pain", "pain complet", True),
])
def test_pantry_matching(have, ingredient, expected):
    assert planner.in_pantry(ingredient, [have]) is expected


def test_late_starter_still_gets_dinners():
    saturday = MONDAY + timedelta(days=5)
    dinners = planner.choose_dinners(LIB, MONDAY, 5, [], [], set(), "late", from_day=saturday)
    assert [d.weekday() for d, _ in dinners] == [5, 6]


def test_curly_apostrophes_are_detected():
    assert safety.check("j\u2019ai mal à l\u2019épaule").category == "pain"

"""Tests for database operations: planning, feedback, changes, exclusions, personal data."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from coach import planner, services
from coach.db import EventLog, PlannedMeal, PlannedSession, User
from coach.library import get_library, recipe_violates
from coach.services import ServiceError

MONDAY = date(2026, 11, 2)


async def _user(s, uid) -> User:
    return await s.get(User, uid)


async def test_first_plan_gives_something_to_do_this_week(db, onboarded_user):
    async with db.session() as s:
        user = await _user(s, onboarded_user)
        plan = await services.create_week_plan(s, user, MONDAY)
        sessions = await services.sessions_between(s, user, MONDAY, MONDAY + timedelta(days=7))
        meals = await services.meals_between(s, user, MONDAY, MONDAY + timedelta(days=7))
        assert len(sessions) == 3
        assert {p.day.weekday() for p in sessions} <= {0, 2, 4, 5}
        assert all(p.exercises for p in sessions)
        assert len(meals) == 5
        assert plan.grocery
        lib = get_library()
        assert not any(recipe_violates(lib.recipe(m.recipe_id), ["porc"]) for m in meals)


async def test_replanning_keeps_done_sessions(db, onboarded_user):
    async with db.session() as s:
        user = await _user(s, onboarded_user)
        await services.create_week_plan(s, user, MONDAY)
        first = (await services.sessions_between(s, user, MONDAY, MONDAY + timedelta(days=1)))[0]
        await services.record_feedback(s, user, first.id, "ok")
        await services.create_week_plan(s, user, MONDAY, [{"date": "2026-11-04", "available": False}])
        sessions = await services.sessions_between(s, user, MONDAY, MONDAY + timedelta(days=7))
        assert first.id in {p.id for p in sessions}
        assert len([p for p in sessions if p.status in ("planned", "done")]) == 3
        assert date(2026, 11, 4) not in {p.day for p in sessions}


async def test_two_hard_sessions_lighten_the_next_ones(db, onboarded_user):
    async with db.session() as s:
        user = await _user(s, onboarded_user)
        await services.create_week_plan(s, user, MONDAY)
        sessions = await services.sessions_between(s, user, MONDAY, MONDAY + timedelta(days=7))
        start = user.load_level
        await services.record_feedback(s, user, sessions[0].id, "hard")
        out = await services.record_feedback(s, user, sessions[1].id, "hard", "trop de pompes")
        assert out.decision.reason == "easier"
        assert user.load_level == start - 1
        assert sessions[2].load_level == start - 1  # upcoming session rebuilt


async def test_reschedule_shorten_postpone_cancel(db, onboarded_user):
    async with db.session() as s:
        user = await _user(s, onboarded_user)
        await services.create_week_plan(s, user, MONDAY)
        ps = (await services.upcoming_planned(s, user, MONDAY))[0]

        short = await services.reschedule(s, user, ps.id, "shorten", minutes=15)
        assert short.minutes == 15

        later = await services.reschedule(s, user, ps.id, "postpone", new_day=MONDAY + timedelta(days=1),
                                          new_time="20h15")
        assert later.day == MONDAY + timedelta(days=1) and later.time == "20:15"

        with pytest.raises(ServiceError):
            await services.reschedule(s, user, ps.id, "postpone", new_day=MONDAY - timedelta(days=1))

        cancelled = await services.reschedule(s, user, ps.id, "cancel")
        assert cancelled.status == "cancelled"
        with pytest.raises(ServiceError):
            await services.reschedule(s, user, ps.id, "shorten", minutes=10)


async def test_new_exclusion_replaces_upcoming_dinners(db, onboarded_user):
    async with db.session() as s:
        user = await _user(s, onboarded_user)
        await services.create_week_plan(s, user, MONDAY)
        await services.update_profile(s, user, food_exclusions=["porc", "végétarien"])
        meals = await services.meals_between(s, user, MONDAY, MONDAY + timedelta(days=7))
        lib = get_library()
        assert meals
        assert all(lib.recipe(m.recipe_id).is_vegetarian for m in meals)


async def test_swap_meal_updates_grocery(db, onboarded_user):
    async with db.session() as s:
        user = await _user(s, onboarded_user)
        plan = await services.create_week_plan(s, user, MONDAY)
        meal = (await services.meals_between(s, user, MONDAY, MONDAY + timedelta(days=1)))[0]
        before = meal.recipe_id
        swapped = await services.swap_meal(s, user, MONDAY)
        assert swapped.recipe_id != before
        names = {i["name"] for i in plan.grocery}
        new_recipe = get_library().recipe(swapped.recipe_id)
        assert any(i.name in names for i in new_recipe.ingredients if not i.basic)


async def test_swap_meal_refuses_excluded_recipe(db, onboarded_user):
    async with db.session() as s:
        user = await _user(s, onboarded_user)
        await services.create_week_plan(s, user, MONDAY)
        with pytest.raises(ServiceError):
            await services.swap_meal(s, user, MONDAY, "zucchini_ham_gratin")  # contains pork


async def test_safety_hold_switches_to_gentle_mobility(db, onboarded_user):
    async with db.session() as s:
        user = await _user(s, onboarded_user)
        await services.create_week_plan(s, user, MONDAY)
        await services.set_safety_hold(s, user, "pain")
        lib = get_library()
        for p in await services.upcoming_planned(s, user, MONDAY):
            assert p.focus == "mobility"
            assert all(lib.exercise(i["id"]).impact == "low" for i in p.exercises)
        await services.lift_safety_hold(s, user)
        assert not user.safety_hold
        assert {p.focus for p in await services.upcoming_planned(s, user, MONDAY)} != {"mobility"}


async def test_quick_session_replaces_todays_session(db, onboarded_user):
    async with db.session() as s:
        user = await _user(s, onboarded_user)
        await services.create_week_plan(s, user, MONDAY)
        ps = await services.quick_session(s, user, 20, MONDAY)
        today_sessions = await services.sessions_between(s, user, MONDAY, MONDAY + timedelta(days=1))
        assert [p.id for p in today_sessions] == [ps.id]
        assert ps.minutes == 20


async def test_onboarding_requires_goal_and_slots(db):
    async with db.session() as s:
        user = await services.get_or_create_user(s, 42, "Sam")
        with pytest.raises(ServiceError):
            await services.finish_onboarding(s, user)
        with pytest.raises(ServiceError):
            await services.update_profile(s, user, slots=[{"day": 9, "time": "18:00", "minutes": 30}])


async def test_export_and_delete(db, onboarded_user):
    async with db.session() as s:
        user = await _user(s, onboarded_user)
        await services.create_week_plan(s, user, MONDAY)
        data = await services.export_user_data(s, user)
        assert data["profile"]["telegram_id"] == 1001
        assert data["sessions"] and data["dinners"]
        await services.delete_user(s, user)
    async with db.session() as s:
        assert await services.get_user(s, 1001) is None
        for model in (PlannedSession, PlannedMeal):
            assert await s.scalar(select(func.count()).select_from(model)) == 0
        remaining = (await s.scalars(select(EventLog.user_id))).all()
        assert set(remaining) == {None}


async def test_comeback_lowers_load_when_planning(db, onboarded_user, monkeypatch):
    async with db.session() as s:
        user = await _user(s, onboarded_user)
        user.load_level = 6
        old = PlannedSession(user_id=user.id, day=MONDAY - timedelta(days=12), time="18:00", minutes=30,
                             status="done", feedback="ok", load_level=6)
        s.add(old)
        await s.flush()
        await services.create_week_plan(s, user, planner.week_start_of(MONDAY))
        assert user.load_level == 4

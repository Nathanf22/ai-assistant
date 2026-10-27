"""Deterministic progression rules (US16, US18).

The LLM never decides the training load: it receives it from these rules.

- Load ranges from 1 (very light) to 10.
- Two "hard" feedbacks in a row at the current load -> load - 1.
- Two "easy" feedbacks in a row at the current load -> load + 1.
- A skipped session changes nothing.
- 10 days or more without a completed session -> load - 2 (gentle comeback).
- Safety hold active -> no automatic increase.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

MIN_LOAD, MAX_LOAD = 1, 10
STREAK_TO_ADJUST = 2
BREAK_DAYS = 10
BREAK_DROP = 2


@dataclass(frozen=True)
class DoneSession:
    day: date
    load_level: int
    feedback: str | None  # easy | ok | hard | None


@dataclass(frozen=True)
class LoadDecision:
    load: int
    changed: bool
    reason: str  # stable | harder | easier | comeback


def clamp(value: int) -> int:
    return max(MIN_LOAD, min(MAX_LOAD, value))


def after_feedback(current: int, history: list[DoneSession], safety_hold: bool = False) -> LoadDecision:
    """Load to apply after a new feedback. `history` is sorted oldest first."""
    # Only the latest sessions count, and all of them must have been done at the current load:
    # a session from before the last change breaks the streak (no back-and-forth of the load).
    recent = [s for s in history if s.feedback][-STREAK_TO_ADJUST:]
    if len(recent) < STREAK_TO_ADJUST or any(s.load_level != current for s in recent):
        return LoadDecision(current, False, "stable")
    kinds = {s.feedback for s in recent}

    if kinds == {"hard"}:
        new = clamp(current - 1)
        return LoadDecision(new, new != current, "easier")
    if kinds == {"easy"} and not safety_hold:
        new = clamp(current + 1)
        return LoadDecision(new, new != current, "harder")
    return LoadDecision(current, False, "stable")


def before_planning(current: int, last_done: DoneSession | None, today: date) -> LoadDecision:
    """Load to use when planning, taking a possible break into account.

    The drop is computed from the load of the last completed session, so replanning several
    times during the same break does not lower the load again each time.
    """
    if last_done is not None and (today - last_done.day).days >= BREAK_DAYS:
        target = clamp(last_done.load_level - BREAK_DROP)
        if current > target:
            return LoadDecision(target, True, "comeback")
    return LoadDecision(current, False, "stable")


def volume(load: int) -> tuple[int, float]:
    """(number of sets, volume multiplier) for a given load. Load 5 -> x1.0."""
    load = clamp(load)
    sets = 2 if load <= 3 else 3 if load <= 7 else 4
    return sets, round(0.6 + 0.08 * load, 2)

"""Règles de progression déterministes (US16, US18).

Le LLM ne décide jamais de la charge : il la reçoit de ces règles.

- La charge va de 1 (très léger) à 10.
- Deux retours « dur » d'affilée à la charge actuelle → charge − 1.
- Deux retours « facile » d'affilée à la charge actuelle → charge + 1.
- Une séance sautée ne change rien.
- Après 10 jours ou plus sans séance faite → charge − 2 (reprise douce).
- Garde-fou actif → aucune hausse automatique.
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
    """Charge à appliquer après un nouveau retour. `history` est trié du plus ancien au plus récent."""
    # Seules comptent les dernières séances, toutes faites à la charge actuelle : une séance
    # d'avant le dernier changement interrompt la série (pas d'aller-retour de la charge).
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
    """Charge à utiliser au moment de planifier, en tenant compte d'une pause éventuelle.

    La baisse se calcule depuis la charge de la dernière séance faite : replanifier plusieurs
    fois pendant la même pause ne fait pas baisser la charge à chaque fois.
    """
    if last_done is not None and (today - last_done.day).days >= BREAK_DAYS:
        target = clamp(last_done.load_level - BREAK_DROP)
        if current > target:
            return LoadDecision(target, True, "comeback")
    return LoadDecision(current, False, "stable")


def volume(load: int) -> tuple[int, float]:
    """(nombre de séries, multiplicateur de volume) pour une charge donnée. Charge 5 → ×1,0."""
    load = clamp(load)
    sets = 2 if load <= 3 else 3 if load <= 7 else 4
    return sets, round(0.6 + 0.08 * load, 2)

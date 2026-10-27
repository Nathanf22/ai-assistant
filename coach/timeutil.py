"""Heure locale : toutes les dates de planning sont des dates locales (Europe/Paris par défaut)."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from coach.config import get_settings

WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MONTHS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def now() -> datetime:
    return datetime.now(tz())


def today() -> date:
    return now().date()


def parse_hhmm(value: str) -> time:
    hours, minutes = value.strip().replace("h", ":").split(":")[:2]
    return time(int(hours), int(minutes or 0))


def normalize_hhmm(value: str) -> str:
    t = parse_hhmm(value)
    return f"{t.hour:02d}:{t.minute:02d}"


def local_datetime(d: date, hhmm: str) -> datetime:
    return datetime.combine(d, parse_hhmm(hhmm), tzinfo=tz())


def day_label(d: date, ref: date | None = None) -> str:
    ref = ref or today()
    delta = (d - ref).days
    if delta == 0:
        return "aujourd'hui"
    if delta == 1:
        return "demain"
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"

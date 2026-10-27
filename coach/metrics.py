"""Pilot metrics (US22) and review of generated plans (US23).

Usage: `coach-metrics` (report) or `coach-metrics --plans 50` (latest generated plans).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coach.config import get_settings
from coach.db import Database, EventLog, PlannedSession, User
from coach.timeutil import today

RETENTION_WEEKS = (1, 2, 4, 8)


@dataclass
class PilotMetrics:
    registered: int = 0
    consented: int = 0
    onboarded: int = 0
    activation: float | None = None
    retention: dict[int, tuple[int, int]] = field(default_factory=dict)  # week -> (active, eligible)
    sessions_done: int = 0
    sessions_missed: int = 0
    adherence: float | None = None
    feedback_counts: dict[str, int] = field(default_factory=dict)
    nps_median: float | None = None
    nps_count: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    cost_30d_usd: float | None = None
    active_30d: int = 0
    safety_holds: int = 0


def _ratio(a: int, b: int) -> float | None:
    return a / b if b else None


async def compute(s: AsyncSession, now: datetime | None = None) -> PilotMetrics:
    now = now or datetime.now(UTC)
    m = PilotMetrics()
    users = list((await s.scalars(select(User))).all())
    m.registered = len(users)
    m.consented = sum(u.consent_at is not None for u in users)
    onboarded = [u for u in users if u.onboarded_at is not None]
    m.onboarded = len(onboarded)

    planned_users = set(
        (await s.scalars(select(EventLog.user_id).where(EventLog.kind == "week_planned").distinct())).all()
    )
    m.activation = _ratio(sum(u.id in planned_users for u in onboarded), m.registered)

    confirmations = (await s.execute(
        select(EventLog.user_id, EventLog.created_at).where(EventLog.kind == "planning_confirmed")
    )).all()
    by_user: dict[int, list[datetime]] = {}
    for uid, at in confirmations:
        by_user.setdefault(uid, []).append(_aware(at))
    for k in RETENTION_WEEKS:
        eligible = [u for u in onboarded if now - _aware(u.onboarded_at) >= timedelta(days=7 * k)]
        active = 0
        for u in eligible:
            start = _aware(u.onboarded_at) + timedelta(days=7 * (k - 1))
            end = start + timedelta(days=7)
            if any(start <= t < end for t in by_user.get(u.id, [])):
                active += 1
        m.retention[k] = (active, len(eligible))

    past = (await s.execute(
        select(PlannedSession.status, func.count())
        .where(PlannedSession.day < today(), PlannedSession.status != "cancelled")
        .group_by(PlannedSession.status)
    )).all()
    counts = dict(past)
    m.sessions_done = counts.get("done", 0)
    m.sessions_missed = counts.get("skipped", 0) + counts.get("planned", 0)
    m.adherence = _ratio(m.sessions_done, m.sessions_done + m.sessions_missed)

    fb = (await s.execute(
        select(PlannedSession.feedback, func.count())
        .where(PlannedSession.feedback.is_not(None))
        .group_by(PlannedSession.feedback)
    )).all()
    m.feedback_counts = {k: v for k, v in fb}

    nps = [e.payload.get("score") for e in (await s.scalars(select(EventLog).where(EventLog.kind == "nps"))).all()]
    nps = [x for x in nps if isinstance(x, int)]
    m.nps_count = len(nps)
    m.nps_median = statistics.median(nps) if nps else None

    since = now - timedelta(days=30)
    usage = (await s.scalars(
        select(EventLog).where(EventLog.kind == "llm_usage", EventLog.created_at >= since)
    )).all()
    keys = ("input_tokens", "output_tokens", "cache_read", "cache_write")
    m.tokens = {k: sum(int(e.payload.get(k, 0)) for e in usage) for k in keys}
    st = get_settings()
    prices = (st.llm_input_usd_per_mtok, st.llm_output_usd_per_mtok,
              st.llm_cache_read_usd_per_mtok, st.llm_cache_write_usd_per_mtok)
    if any(prices):
        m.cost_30d_usd = sum(m.tokens[k] * p / 1_000_000 for k, p in zip(keys, prices, strict=True))
    m.active_30d = len(set(
        (await s.scalars(
            select(EventLog.user_id).where(EventLog.created_at >= since, EventLog.user_id.is_not(None)).distinct()
        )).all()
    ))
    m.safety_holds = sum(u.safety_hold for u in users)
    return m


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def report(m: PilotMetrics) -> str:
    """Operator-facing report (English), used by `coach-metrics` and the /stats command."""
    lines = [
        "Pilot metrics",
        f"Registered {m.registered} · consented {m.consented} · onboarded {m.onboarded}",
        f"Activation (onboarding + first plan): {_pct(m.activation)}  (target >= 80%)",
    ]
    for k, (active, eligible) in m.retention.items():
        target = {4: "  (target >= 50%)", 8: "  (target >= 35%)"}.get(k, "")
        lines.append(f"Retention W{k} (planning done): {active}/{eligible} = {_pct(_ratio(active, eligible))}{target}")
    lines.append(f"Plan adherence: {m.sessions_done} done / {m.sessions_done + m.sessions_missed} past "
                 f"= {_pct(m.adherence)}  (target >= 60%)")
    total_fb = sum(m.feedback_counts.values())
    lines.append("Feedback: " + ", ".join(f"{k} {v}" for k, v in sorted(m.feedback_counts.items()))
                 + f" · share of \"ok\" {_pct(_ratio(m.feedback_counts.get('ok', 0), total_fb))}  (target >= 60%)")
    lines.append(f"Recommendation score (median of {m.nps_count}): "
                 f"{'n/a' if m.nps_median is None else m.nps_median}  (target >= 8)")
    t = m.tokens
    lines.append(f"Tokens, last 30 days: input {t.get('input_tokens', 0):,} · output {t.get('output_tokens', 0):,} · "
                 f"cache read {t.get('cache_read', 0):,} · cache write {t.get('cache_write', 0):,}")
    if m.cost_30d_usd is not None:
        per_user = m.cost_30d_usd / m.active_30d if m.active_30d else None
        lines.append(f"LLM cost, last 30 days: ${m.cost_30d_usd:.2f} · per active user: "
                     f"{'n/a' if per_user is None else f'${per_user:.2f}'}  (target <= EUR 5)")
    else:
        lines.append("LLM cost: set the LLM_*_USD_PER_MTOK prices to estimate it.")
    lines.append(f"Active users, last 30 days: {m.active_30d} · safety holds in progress: {m.safety_holds}")
    return "\n".join(lines)


async def recent_plans(s: AsyncSession, limit: int) -> str:
    """Operator-facing list of the latest generated plans (US23)."""
    events = (await s.scalars(
        select(EventLog).where(EventLog.kind == "week_planned").order_by(EventLog.id.desc()).limit(limit)
    )).all()
    out = []
    for e in events:
        p = e.payload
        sessions = ", ".join(f"{x['day']} {x['time']} {x['minutes']}min" for x in p.get("sessions", []))
        out.append(f"#{e.id} user {e.user_id} · week {p.get('week_start')} · load {p.get('load')}\n"
                   f"  sessions: {sessions or 'none'}\n  dinners: {', '.join(p.get('dinners', [])) or 'none'}")
    return "\n".join(out) or "No plan generated yet."


async def _main(args: argparse.Namespace) -> None:
    db = Database()
    await db.create_all()
    async with db.session() as s:
        print(await recent_plans(s, args.plans) if args.plans else report(await compute(s)))
    await db.dispose()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Pilot metrics")
    parser.add_argument("--plans", type=int, default=0, help="Show the N latest generated plans")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    cli()

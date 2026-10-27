"""Métriques du pilote (US22) et relecture des plans générés (US23).

Usage : `coach-metrics` (rapport) ou `coach-metrics --plans 50` (derniers plannings générés).
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
    retention: dict[int, tuple[int, int]] = field(default_factory=dict)  # semaine → (actifs, éligibles)
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
    return "n/d" if x is None else f"{100 * x:.0f} %"


def report(m: PilotMetrics) -> str:
    lines = [
        "Métriques du pilote",
        f"Inscrits {m.registered} · consentement {m.consented} · onboarding terminé {m.onboarded}",
        f"Activation (onboarding + premier plan) : {_pct(m.activation)}  (cible ≥ 80 %)",
    ]
    for k, (active, eligible) in m.retention.items():
        target = {4: "  (cible ≥ 50 %)", 8: "  (cible ≥ 35 %)"}.get(k, "")
        lines.append(f"Rétention S{k} (planning fait) : {active}/{eligible} = {_pct(_ratio(active, eligible))}{target}")
    lines.append(f"Suivi du plan : {m.sessions_done} faites / {m.sessions_done + m.sessions_missed} passées "
                 f"= {_pct(m.adherence)}  (cible ≥ 60 %)")
    total_fb = sum(m.feedback_counts.values())
    lines.append("Retours : " + ", ".join(f"{k} {v}" for k, v in sorted(m.feedback_counts.items()))
                 + f" · part « ok » {_pct(_ratio(m.feedback_counts.get('ok', 0), total_fb))}  (cible ≥ 60 %)")
    lines.append(f"Recommandation (médiane sur {m.nps_count}) : "
                 f"{'n/d' if m.nps_median is None else m.nps_median}  (cible ≥ 8)")
    t = m.tokens
    lines.append(f"Tokens 30 j : entrée {t.get('input_tokens', 0):,} · sortie {t.get('output_tokens', 0):,} · "
                 f"cache lu {t.get('cache_read', 0):,} · cache écrit {t.get('cache_write', 0):,}".replace(",", " "))
    if m.cost_30d_usd is not None:
        per_user = m.cost_30d_usd / m.active_30d if m.active_30d else None
        lines.append(f"Coût LLM 30 j : {m.cost_30d_usd:.2f} $ · par utilisateur actif : "
                     f"{'n/d' if per_user is None else f'{per_user:.2f} $'}  (cible ≤ 5 €)")
    else:
        lines.append("Coût LLM : renseigner les tarifs LLM_*_USD_PER_MTOK pour l'estimer.")
    lines.append(f"Utilisateurs actifs 30 j : {m.active_30d} · pauses de sécurité en cours : {m.safety_holds}")
    return "\n".join(lines)


async def recent_plans(s: AsyncSession, limit: int) -> str:
    events = (await s.scalars(
        select(EventLog).where(EventLog.kind == "week_planned").order_by(EventLog.id.desc()).limit(limit)
    )).all()
    out = []
    for e in events:
        p = e.payload
        sessions = ", ".join(f"{x['day']} {x['time']} {x['minutes']}min" for x in p.get("sessions", []))
        out.append(f"#{e.id} user {e.user_id} · semaine {p.get('week_start')} · charge {p.get('load')}\n"
                   f"  séances : {sessions or 'aucune'}\n  dîners : {', '.join(p.get('dinners', [])) or 'aucun'}")
    return "\n".join(out) or "Aucun planning généré."


async def _main(args: argparse.Namespace) -> None:
    db = Database()
    await db.create_all()
    async with db.session() as s:
        print(await recent_plans(s, args.plans) if args.plans else report(await compute(s)))
    await db.dispose()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Métriques du pilote")
    parser.add_argument("--plans", type=int, default=0, help="Afficher les N derniers plannings générés")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    cli()

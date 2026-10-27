"""Modèle de données et accès base (SQLAlchemy 2.0, async).

SQLite en développement, PostgreSQL en production : seul DATABASE_URL change.
Aucune donnée médicale n'est stockée : une mention de douleur ou de blessure
pose un drapeau `safety_hold`, sans conserver le détail.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from coach.config import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invite_code: Mapped[str] = mapped_column(String(40), default="")

    # Objectifs et contraintes
    goal_text: Mapped[str] = mapped_column(Text, default="")
    sessions_per_week: Mapped[int] = mapped_column(Integer, default=3)
    level: Mapped[str] = mapped_column(String(20), default="debutant")
    equipment: Mapped[list] = mapped_column(default=list)  # ["aucun", "elastique", ...]
    # Créneaux habituels : [{"day": 0..6 (lundi=0), "time": "18:30", "minutes": 30}]
    slots: Mapped[list] = mapped_column(default=list)

    # Alimentation
    food_exclusions: Mapped[list] = mapped_column(default=list)  # jamais proposés
    food_dislikes: Mapped[list] = mapped_column(default=list)  # évités si possible
    pantry: Mapped[list] = mapped_column(default=list)  # déjà en stock
    dinners_per_week: Mapped[int] = mapped_column(Integer, default=5)

    # Préférences d'exercices (US17)
    liked_exercises: Mapped[list] = mapped_column(default=list)
    avoided_exercises: Mapped[list] = mapped_column(default=list)

    # Messages (US12)
    brief_time: Mapped[str] = mapped_column(String(5), default="07:45")
    planning_time: Mapped[str] = mapped_column(String(5), default="18:00")
    quiet_dates: Mapped[list] = mapped_column(default=list)  # dates ISO sans message
    # Anti-doublons du planificateur
    last_brief_on: Mapped[date | None] = mapped_column(Date)
    last_planning_on: Mapped[date | None] = mapped_column(Date)
    survey_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Bot bloqué par l'utilisateur : plus aucun envoi automatique jusqu'à son prochain message
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Progression (US16, US18) : charge de 1 (très léger) à 10
    load_level: Mapped[int] = mapped_column(Integer, default=3)
    # Garde-fou (US20) : ajustements suspendus après une mention de douleur
    safety_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    safety_hold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sessions: Mapped[list[PlannedSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_onboarded(self) -> bool:
        return self.onboarded_at is not None


class WeekPlan(Base):
    __tablename__ = "week_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    week_start: Mapped[date] = mapped_column(Date, index=True)  # lundi
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # [{"name", "quantity", "unit", "aisle"}]
    grocery: Mapped[list] = mapped_column(default=list)
    constraints_text: Mapped[str] = mapped_column(Text, default="")


class PlannedSession(Base):
    __tablename__ = "planned_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    week_plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("week_plans.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date] = mapped_column(Date, index=True)
    time: Mapped[str] = mapped_column(String(5), default="18:30")
    minutes: Mapped[int] = mapped_column(Integer, default=30)
    focus: Mapped[str] = mapped_column(String(40), default="full_body")
    load_level: Mapped[int] = mapped_column(Integer, default=3)
    # [{"id", "name", "sets", "reps"|"seconds", "rest", "cue"}]
    exercises: Mapped[list] = mapped_column(default=list)
    # planned | done | skipped | cancelled
    status: Mapped[str] = mapped_column(String(12), default="planned")
    feedback: Mapped[str | None] = mapped_column(String(8))  # easy | ok | hard
    feedback_note: Mapped[str] = mapped_column(Text, default="")
    feedback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    feedback_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="sessions")


class PlannedMeal(Base):
    __tablename__ = "planned_meals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    week_plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("week_plans.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date] = mapped_column(Date, index=True)
    recipe_id: Mapped[str] = mapped_column(String(60))
    servings: Mapped[int] = mapped_column(Integer, default=1)


class ChatMessage(Base):
    """Historique court de conversation, pour le contexte de l'agent."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventLog(Base):
    """Journal de chaque plan, retour et ajustement (observabilité et métriques du pilote)."""

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Database:
    def __init__(self, url: str | None = None) -> None:
        url = url or get_settings().database_url
        self.engine = create_async_engine(url, future=True)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()


def log_event(session: AsyncSession, user_id: int | None, kind: str, **payload) -> None:
    session.add(EventLog(user_id=user_id, kind=kind, payload=payload))

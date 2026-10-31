"""Data model and database access (SQLAlchemy 2.0, async).

SQLite in development, PostgreSQL in production: only DATABASE_URL changes.
No medical data is stored: a mention of pain or injury sets a `safety_hold` flag
without keeping the details.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import NullPool

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

    # Goals and constraints
    goal_text: Mapped[str] = mapped_column(Text, default="")
    sessions_per_week: Mapped[int] = mapped_column(Integer, default=3)
    level: Mapped[str] = mapped_column(String(20), default="beginner")
    equipment: Mapped[list] = mapped_column(default=list)  # ["band", "dumbbells", "bench"]
    # Usual slots: [{"day": 0..6 (Monday=0), "time": "18:30", "minutes": 30}]
    slots: Mapped[list] = mapped_column(default=list)

    # Food
    food_exclusions: Mapped[list] = mapped_column(default=list)  # never suggested
    food_dislikes: Mapped[list] = mapped_column(default=list)  # avoided when possible
    pantry: Mapped[list] = mapped_column(default=list)  # already in stock
    dinners_per_week: Mapped[int] = mapped_column(Integer, default=5)

    # Exercise preferences (US17)
    liked_exercises: Mapped[list] = mapped_column(default=list)
    avoided_exercises: Mapped[list] = mapped_column(default=list)

    # Messages (US12)
    brief_time: Mapped[str] = mapped_column(String(5), default="07:45")
    planning_time: Mapped[str] = mapped_column(String(5), default="18:00")
    quiet_dates: Mapped[list] = mapped_column(default=list)  # ISO dates with no automatic message
    # Scheduler de-duplication markers
    last_brief_on: Mapped[date | None] = mapped_column(Date)
    last_planning_on: Mapped[date | None] = mapped_column(Date)
    survey_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Bot blocked by the user: no automatic message until they write again
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Progression (US16, US18): load from 1 (very light) to 10
    load_level: Mapped[int] = mapped_column(Integer, default=3)
    # Safety guardrail (US20): progression paused after a mention of pain
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
    week_start: Mapped[date] = mapped_column(Date, index=True)  # Monday
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
    """Short conversation history, used as context for the agent."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventLog(Base):
    """Log of every plan, feedback and adjustment (observability and pilot metrics)."""

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


def normalize_database_url(url: str) -> str:
    """Accept the plain `postgres://...?sslmode=require` URLs that hosted providers (Neon,
    Vercel) hand out, and turn them into what the asyncpg driver expects."""
    parsed = make_url(url)
    if parsed.drivername not in ("postgres", "postgresql"):
        return url
    query = dict(parsed.query)
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)  # libpq-only option, unknown to asyncpg
    if sslmode and "ssl" not in query:
        query["ssl"] = sslmode
    return parsed.set(drivername="postgresql+asyncpg", query=query).render_as_string(
        hide_password=False
    )


class Database:
    def __init__(self, url: str | None = None, serverless: bool = False) -> None:
        url = normalize_database_url(url or get_settings().database_url)
        # Serverless instances can be frozen between requests: pooled connections would go stale.
        options = {"poolclass": NullPool} if serverless else {}
        self.engine = create_async_engine(url, future=True, **options)
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

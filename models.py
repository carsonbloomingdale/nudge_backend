import uuid

from database import Base
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import mapped_column, relationship
from sqlalchemy.sql import func


class Journal(Base):
    """User-facing log entry; embeds multiple Task rows for analytics / personality matrix."""

    __tablename__ = "journals"

    journal_id = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = mapped_column(UUID(as_uuid=True), ForeignKey("person.user_id"), nullable=False, index=True)
    submitted_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    source = mapped_column(String(32), nullable=False, default="app")
    note = mapped_column(Text, nullable=True)
    tasks = relationship("Task", back_populates="journal", cascade="all, delete-orphan")
    attachments = relationship(
        "JournalAttachment",
        back_populates="journal",
        cascade="all, delete-orphan",
    )


class PersonalityChartCache(Base):
    """Server-side cache for GET /api/analytics/personality-traits-chart (per user, raw vs AI)."""

    __tablename__ = "personality_chart_cache"

    user_id = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("person.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    payload_raw = mapped_column(JSON, nullable=True)
    payload_ai = mapped_column(JSON, nullable=True)
    updated_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class JournalAttachment(Base):
    __tablename__ = "journal_attachments"

    attachment_id = mapped_column(Integer, primary_key=True, autoincrement=True)
    journal_id = mapped_column(
        Integer,
        ForeignKey("journals.journal_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    storage_key = mapped_column(String(512), nullable=False)
    content_type = mapped_column(String(128), nullable=False)
    byte_size = mapped_column(Integer, nullable=True)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    upload_completed_at = mapped_column(DateTime(timezone=True), nullable=True)
    journal = relationship("Journal", back_populates="attachments")


class PersonalityTrait(Base):
    __tablename__ = 'personality_traits'
    task_id=mapped_column(Integer,  ForeignKey("tasks.task_id"))
    trait_id= Column(Integer, primary_key=True, index=True)
    label= Column(String)


class PinnedPersonalityTrait(Base):
    """User-selected traits to keep visible and feed into enrichment context."""

    __tablename__ = "pinned_personality_traits"
    __table_args__ = (
        UniqueConstraint("user_id", "label", name="uq_pinned_personality_traits_user_label"),
    )

    pin_id = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id = mapped_column(UUID(as_uuid=True), ForeignKey("person.user_id", ondelete="CASCADE"), nullable=False, index=True)
    label = mapped_column(String(80), nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Task(Base):
    __tablename__ = 'tasks'

    task_id= mapped_column(Integer, primary_key=True, index=True)
    user_id= mapped_column(UUID, ForeignKey("person.user_id"))
    journal_id = mapped_column(
        Integer,
        ForeignKey("journals.journal_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    category=Column(String)
    label=Column(String)
    time_of_day=Column(String)
    amount_of_time=Column(String)
    day_of_week=Column(String)
    context= Column(String)
    sentiment=Column(String)
    journal = relationship("Journal", back_populates="tasks")
    personality_traits = relationship(
        "PersonalityTrait",
        primaryjoin="and_(PersonalityTrait.task_id==Task.task_id)",
    )

class Person(Base):
    __tablename__ = 'person'

    user_name= Column(String)
    user_id= mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email=Column(String)
    password_hash = Column(String, nullable=True)
    first_name = Column(String(128), nullable=True)
    last_name = Column(String(128), nullable=True)
    phone_e164 = Column(String(20), nullable=True)
    timezone = Column(String(64), nullable=True)
    sms_opt_in = Column(Boolean, nullable=False, default=False)
    phone_verified_at = Column(DateTime(timezone=True), nullable=True)
    enrichment_summary = Column(Text, nullable=True)
    person_tasks = relationship(
        "Task",
        primaryjoin="and_(Task.user_id==Person.user_id)",
    )


class SmsDailyCheckin(Base):
    """One outbound daily prompt per user per local calendar day (timezone on Person)."""

    __tablename__ = "sms_daily_checkin"
    __table_args__ = (UniqueConstraint("user_id", "local_date", name="uq_sms_checkin_user_local_date"),)

    id = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = mapped_column(UUID(as_uuid=True), ForeignKey("person.user_id"), nullable=False)
    local_date = mapped_column(String(10), nullable=False)
    outbound_message_sid = mapped_column(String(64), nullable=True)
    status = mapped_column(String(24), nullable=False, default="awaiting_reply")
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SmsInboundDedup(Base):
    """Twilio may retry webhooks; store processed SmsMessageSid once."""

    __tablename__ = "sms_inbound_dedup"

    message_sid = mapped_column(String(64), primary_key=True)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PhoneOtpChallenge(Base):
    """Short-lived SMS OTP to prove ownership of phone_e164 before outbound SMS (toll-free / compliance)."""

    __tablename__ = "phone_otp_challenge"

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id = mapped_column(UUID(as_uuid=True), ForeignKey("person.user_id"), nullable=False, index=True)
    code_hash = mapped_column(String(64), nullable=False)
    expires_at = mapped_column(DateTime(timezone=True), nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
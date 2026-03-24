import uuid

from database import Base
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
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
    goal_links = relationship(
        "TaskGrowthGoalLink",
        primaryjoin="and_(TaskGrowthGoalLink.task_id==Task.task_id)",
    )


class GrowthGoal(Base):
    __tablename__ = "growth_goals"
    __table_args__ = (UniqueConstraint("slug", name="uq_growth_goals_slug"),)

    goal_id = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug = mapped_column(String(120), nullable=False, index=True)
    label = mapped_column(String(160), nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PinnedGrowthGoal(Base):
    __tablename__ = "pinned_growth_goals"
    __table_args__ = (UniqueConstraint("user_id", "goal_id", name="uq_pinned_growth_goals_user_goal"),)

    pin_id = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id = mapped_column(UUID(as_uuid=True), ForeignKey("person.user_id", ondelete="CASCADE"), nullable=False, index=True)
    goal_id = mapped_column(Integer, ForeignKey("growth_goals.goal_id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TaskGrowthGoalLink(Base):
    __tablename__ = "task_growth_goal_links"
    __table_args__ = (UniqueConstraint("task_id", "goal_id", name="uq_task_growth_goal_task_goal"),)

    link_id = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id = mapped_column(Integer, ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False, index=True)
    goal_id = mapped_column(Integer, ForeignKey("growth_goals.goal_id", ondelete="CASCADE"), nullable=False, index=True)
    confidence = mapped_column(Float, nullable=False, default=1.0)
    source = mapped_column(String(24), nullable=False, default="heuristic")
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class GrowthGoalActivityRollup(Base):
    __tablename__ = "growth_goal_activity_rollups"
    __table_args__ = (
        UniqueConstraint("user_id", "goal_id", "grain", "period_start", name="uq_goal_rollup_user_goal_grain_date"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id = mapped_column(UUID(as_uuid=True), ForeignKey("person.user_id", ondelete="CASCADE"), nullable=False, index=True)
    goal_id = mapped_column(Integer, ForeignKey("growth_goals.goal_id", ondelete="CASCADE"), nullable=False, index=True)
    grain = mapped_column(String(12), nullable=False, index=True)  # day|week|month
    period_start = mapped_column(Date, nullable=False, index=True)
    total = mapped_column(Integer, nullable=False, default=0)
    updated_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class TraitActivityRollup(Base):
    __tablename__ = "trait_activity_rollups"
    __table_args__ = (
        UniqueConstraint("user_id", "trait_label", "grain", "period_start", name="uq_trait_rollup_user_trait_grain_date"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id = mapped_column(UUID(as_uuid=True), ForeignKey("person.user_id", ondelete="CASCADE"), nullable=False, index=True)
    trait_label = mapped_column(String(120), nullable=False, index=True)
    grain = mapped_column(String(12), nullable=False, index=True)  # day|week|month
    period_start = mapped_column(Date, nullable=False, index=True)
    total = mapped_column(Integer, nullable=False, default=0)
    updated_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

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
    role = Column(String(32), nullable=False, default="user")
    account_locked = Column(Boolean, nullable=False, default=False)
    admin_note = Column(Text, nullable=True)
    mfa_enabled = Column(Boolean, nullable=False, default=False)
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


class AdminAuditEvent(Base):
    __tablename__ = "admin_audit_events"

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_user_id = mapped_column(UUID(as_uuid=True), ForeignKey("person.user_id"), nullable=False, index=True)
    action = mapped_column(String(80), nullable=False, index=True)
    target_type = mapped_column(String(40), nullable=False, index=True)
    target_id = mapped_column(String(120), nullable=False, index=True)
    event_meta = mapped_column(JSON, nullable=True)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        UniqueConstraint("ticket_id", "requester_user_id", name="uq_support_ticket_requester"),
    )

    ticket_id = mapped_column(Integer, primary_key=True, autoincrement=True)
    requester_user_id = mapped_column(UUID(as_uuid=True), ForeignKey("person.user_id", ondelete="CASCADE"), nullable=False, index=True)
    subject = mapped_column(String(200), nullable=False)
    status = mapped_column(String(24), nullable=False, default="open", index=True)
    priority = mapped_column(String(16), nullable=False, default="normal", index=True)
    assigned_to_user_id = mapped_column(UUID(as_uuid=True), ForeignKey("person.user_id"), nullable=True, index=True)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(), index=True)
    requester = relationship("Person", foreign_keys=[requester_user_id])
    assignee = relationship("Person", foreign_keys=[assigned_to_user_id])
    messages = relationship("SupportTicketMessage", back_populates="ticket", cascade="all, delete-orphan")
    events = relationship("SupportTicketEvent", back_populates="ticket", cascade="all, delete-orphan")


class SupportTicketMessage(Base):
    __tablename__ = "support_ticket_messages"

    message_id = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id = mapped_column(Integer, ForeignKey("support_tickets.ticket_id", ondelete="CASCADE"), nullable=False, index=True)
    author_user_id = mapped_column(UUID(as_uuid=True), ForeignKey("person.user_id", ondelete="CASCADE"), nullable=False, index=True)
    body = mapped_column(Text, nullable=False)
    is_internal = mapped_column(Boolean, nullable=False, default=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    ticket = relationship("SupportTicket", back_populates="messages")
    author = relationship("Person")


class SupportTicketAttachment(Base):
    __tablename__ = "support_ticket_attachments"

    attachment_id = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id = mapped_column(Integer, ForeignKey("support_tickets.ticket_id", ondelete="CASCADE"), nullable=False, index=True)
    message_id = mapped_column(Integer, ForeignKey("support_ticket_messages.message_id", ondelete="CASCADE"), nullable=True, index=True)
    storage_key = mapped_column(String(512), nullable=False)
    content_type = mapped_column(String(128), nullable=False)
    byte_size = mapped_column(Integer, nullable=True)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SupportTicketEvent(Base):
    __tablename__ = "support_ticket_events"

    event_id = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id = mapped_column(Integer, ForeignKey("support_tickets.ticket_id", ondelete="CASCADE"), nullable=False, index=True)
    actor_user_id = mapped_column(UUID(as_uuid=True), ForeignKey("person.user_id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = mapped_column(String(32), nullable=False, index=True)  # created|status_changed|priority_changed|assigned|message
    old_value = mapped_column(String(120), nullable=True)
    new_value = mapped_column(String(120), nullable=True)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    ticket = relationship("SupportTicket", back_populates="events")
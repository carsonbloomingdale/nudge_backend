from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func

import models
from auth_deps import AdminUser, CurrentUser
from database import DbSession

router = APIRouter(tags=["support", "admin"])

TicketStatus = Literal["open", "in_progress", "waiting_on_customer", "resolved", "closed"]
TicketPriority = Literal["low", "normal", "high", "urgent"]


class TicketMessageItem(BaseModel):
    message_id: int
    author_user_id: UUID
    body: str
    is_internal: bool
    created_at: datetime


class TicketItem(BaseModel):
    ticket_id: int
    requester_user_id: UUID
    subject: str
    status: TicketStatus
    priority: TicketPriority
    assigned_to_user_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime


class TicketDetail(TicketItem):
    messages: list[TicketMessageItem] = Field(default_factory=list)


class TicketListResponse(BaseModel):
    tickets: list[TicketItem]
    total: int


class CreateTicketBody(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    message: str = Field(min_length=1, max_length=4000)
    priority: TicketPriority = "normal"


class AddTicketMessageBody(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    is_internal: bool = False


class AdminUpdateTicketBody(BaseModel):
    status: Optional[TicketStatus] = None
    priority: Optional[TicketPriority] = None
    assigned_to_user_id: Optional[UUID] = None


class AdminAssignTicketBody(BaseModel):
    assigned_to_user_id: UUID


class AdminUserSummary(BaseModel):
    user_id: UUID
    username: str
    email: str
    role: str
    account_locked: bool
    created_tickets: int
    total_tasks: int


class AdminUserLookupResponse(BaseModel):
    users: list[AdminUserSummary]
    customers: list[AdminUserSummary] = Field(default_factory=list)
    total: int


class AdminUserActionBody(BaseModel):
    lock_account: Optional[bool] = None
    admin_note: Optional[str] = Field(None, max_length=2000)
    mfa_enabled: Optional[bool] = None


def _audit(db: DbSession, admin_user: models.Person, action: str, target_type: str, target_id: str, metadata: dict | None = None) -> None:
    db.add(
        models.AdminAuditEvent(
            admin_user_id=admin_user.user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            event_meta=metadata or {},
        )
    )


def _to_ticket_item(ticket: models.SupportTicket) -> TicketItem:
    return TicketItem(
        ticket_id=ticket.ticket_id,
        requester_user_id=ticket.requester_user_id,
        subject=ticket.subject,
        status=ticket.status,
        priority=ticket.priority,
        assigned_to_user_id=ticket.assigned_to_user_id,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )


def _to_ticket_detail(ticket: models.SupportTicket, *, include_internal: bool) -> TicketDetail:
    messages = []
    for msg in sorted(ticket.messages or [], key=lambda x: x.message_id):
        if msg.is_internal and not include_internal:
            continue
        messages.append(
            TicketMessageItem(
                message_id=msg.message_id,
                author_user_id=msg.author_user_id,
                body=msg.body,
                is_internal=bool(msg.is_internal),
                created_at=msg.created_at,
            )
        )
    base = _to_ticket_item(ticket)
    data = base.model_dump()
    data["messages"] = messages
    return TicketDetail(**data)


@router.post("/api/support/tickets", response_model=TicketDetail)
def create_support_ticket(body: CreateTicketBody, db: DbSession, user: CurrentUser):
    if bool(user.account_locked):
        raise HTTPException(status_code=403, detail="Account is locked")
    ticket = models.SupportTicket(
        requester_user_id=user.user_id,
        subject=body.subject.strip(),
        status="open",
        priority=body.priority,
    )
    db.add(ticket)
    db.flush()
    db.add(
        models.SupportTicketMessage(
            ticket_id=ticket.ticket_id,
            author_user_id=user.user_id,
            body=body.message.strip(),
            is_internal=False,
        )
    )
    db.add(
        models.SupportTicketEvent(
            ticket_id=ticket.ticket_id,
            actor_user_id=user.user_id,
            event_type="created",
            old_value=None,
            new_value="open",
        )
    )
    db.commit()
    db.refresh(ticket)
    return _to_ticket_detail(ticket, include_internal=False)


@router.get("/api/support/tickets", response_model=TicketListResponse)
def list_my_support_tickets(
    db: DbSession,
    user: CurrentUser,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    q = db.query(models.SupportTicket).filter(models.SupportTicket.requester_user_id == user.user_id)
    total = q.count()
    rows = (
        q.order_by(models.SupportTicket.updated_at.desc(), models.SupportTicket.ticket_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return TicketListResponse(tickets=[_to_ticket_item(t) for t in rows], total=total)


@router.get("/api/support/tickets/{ticket_id}", response_model=TicketDetail)
def get_my_support_ticket(ticket_id: int, db: DbSession, user: CurrentUser):
    ticket = (
        db.query(models.SupportTicket)
        .filter(models.SupportTicket.ticket_id == ticket_id, models.SupportTicket.requester_user_id == user.user_id)
        .first()
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return _to_ticket_detail(ticket, include_internal=False)


@router.post("/api/support/tickets/{ticket_id}/messages", response_model=TicketDetail)
def add_my_ticket_message(ticket_id: int, body: AddTicketMessageBody, db: DbSession, user: CurrentUser):
    ticket = (
        db.query(models.SupportTicket)
        .filter(models.SupportTicket.ticket_id == ticket_id, models.SupportTicket.requester_user_id == user.user_id)
        .first()
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    db.add(
        models.SupportTicketMessage(
            ticket_id=ticket.ticket_id,
            author_user_id=user.user_id,
            body=body.body.strip(),
            is_internal=False,
        )
    )
    old_status = ticket.status
    if ticket.status in {"resolved", "closed"}:
        ticket.status = "waiting_on_customer"
    ticket.updated_at = datetime.now(timezone.utc)
    db.add(ticket)
    db.add(
        models.SupportTicketEvent(
            ticket_id=ticket.ticket_id,
            actor_user_id=user.user_id,
            event_type="message",
            old_value=old_status,
            new_value=ticket.status,
        )
    )
    db.commit()
    db.refresh(ticket)
    return _to_ticket_detail(ticket, include_internal=False)


@router.get("/api/admin/support/tickets", response_model=TicketListResponse)
def admin_list_tickets(
    db: DbSession,
    admin: AdminUser,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=300),
    status: Optional[TicketStatus] = None,
    priority: Optional[TicketPriority] = None,
    assigned_to: Optional[UUID] = None,
    q: Optional[str] = Query(None, max_length=200),
):
    query = db.query(models.SupportTicket)
    if status:
        query = query.filter(models.SupportTicket.status == status)
    if priority:
        query = query.filter(models.SupportTicket.priority == priority)
    if assigned_to:
        query = query.filter(models.SupportTicket.assigned_to_user_id == assigned_to)
    if q:
        needle = f"%{q.lower().strip()}%"
        query = query.filter(func.lower(models.SupportTicket.subject).like(needle))
    total = query.count()
    rows = (
        query.order_by(models.SupportTicket.updated_at.desc(), models.SupportTicket.ticket_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return TicketListResponse(tickets=[_to_ticket_item(t) for t in rows], total=total)


@router.get("/api/admin/support/tickets/{ticket_id}", response_model=TicketDetail)
def admin_get_ticket(ticket_id: int, db: DbSession, admin: AdminUser):
    ticket = db.query(models.SupportTicket).filter(models.SupportTicket.ticket_id == ticket_id).first()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return _to_ticket_detail(ticket, include_internal=True)


@router.patch("/api/admin/support/tickets/{ticket_id}", response_model=TicketDetail)
def admin_update_ticket(ticket_id: int, body: AdminUpdateTicketBody, db: DbSession, admin: AdminUser):
    ticket = db.query(models.SupportTicket).filter(models.SupportTicket.ticket_id == ticket_id).first()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    patch = body.model_dump(exclude_unset=True)
    old_status = ticket.status
    old_priority = ticket.priority
    old_assignee = str(ticket.assigned_to_user_id) if ticket.assigned_to_user_id else None
    if "status" in patch and patch["status"] is not None:
        ticket.status = patch["status"]
    if "priority" in patch and patch["priority"] is not None:
        ticket.priority = patch["priority"]
    if "assigned_to_user_id" in patch:
        ticket.assigned_to_user_id = patch["assigned_to_user_id"]
    db.add(ticket)
    if old_status != ticket.status:
        db.add(models.SupportTicketEvent(ticket_id=ticket.ticket_id, actor_user_id=admin.user_id, event_type="status_changed", old_value=old_status, new_value=ticket.status))
    if old_priority != ticket.priority:
        db.add(models.SupportTicketEvent(ticket_id=ticket.ticket_id, actor_user_id=admin.user_id, event_type="priority_changed", old_value=old_priority, new_value=ticket.priority))
    if old_assignee != (str(ticket.assigned_to_user_id) if ticket.assigned_to_user_id else None):
        db.add(models.SupportTicketEvent(ticket_id=ticket.ticket_id, actor_user_id=admin.user_id, event_type="assigned", old_value=old_assignee, new_value=str(ticket.assigned_to_user_id) if ticket.assigned_to_user_id else None))
    _audit(db, admin, "admin_ticket_update", "support_ticket", str(ticket.ticket_id), {"patch": patch})
    db.commit()
    db.refresh(ticket)
    return _to_ticket_detail(ticket, include_internal=True)


@router.post("/api/admin/support/tickets/{ticket_id}/messages", response_model=TicketDetail)
def admin_add_ticket_message(ticket_id: int, body: AddTicketMessageBody, db: DbSession, admin: AdminUser):
    ticket = db.query(models.SupportTicket).filter(models.SupportTicket.ticket_id == ticket_id).first()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    db.add(
        models.SupportTicketMessage(
            ticket_id=ticket.ticket_id,
            author_user_id=admin.user_id,
            body=body.body.strip(),
            is_internal=bool(body.is_internal),
        )
    )
    if not body.is_internal and ticket.status in {"open", "in_progress"}:
        ticket.status = "waiting_on_customer"
    db.add(ticket)
    db.add(
        models.SupportTicketEvent(
            ticket_id=ticket.ticket_id,
            actor_user_id=admin.user_id,
            event_type="message",
            old_value=None,
            new_value="internal" if body.is_internal else "public",
        )
    )
    _audit(db, admin, "admin_ticket_message", "support_ticket", str(ticket.ticket_id), {"is_internal": bool(body.is_internal)})
    db.commit()
    db.refresh(ticket)
    return _to_ticket_detail(ticket, include_internal=True)


@router.post("/api/admin/support/tickets/{ticket_id}/assign", response_model=TicketDetail)
def admin_assign_ticket(ticket_id: int, body: AdminAssignTicketBody, db: DbSession, admin: AdminUser):
    ticket = db.query(models.SupportTicket).filter(models.SupportTicket.ticket_id == ticket_id).first()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    old = ticket.assigned_to_user_id
    ticket.assigned_to_user_id = body.assigned_to_user_id
    db.add(ticket)
    db.add(
        models.SupportTicketEvent(
            ticket_id=ticket.ticket_id,
            actor_user_id=admin.user_id,
            event_type="assigned",
            old_value=str(old) if old else None,
            new_value=str(body.assigned_to_user_id),
        )
    )
    _audit(db, admin, "admin_ticket_assign", "support_ticket", str(ticket.ticket_id), {"assigned_to_user_id": str(body.assigned_to_user_id)})
    db.commit()
    db.refresh(ticket)
    return _to_ticket_detail(ticket, include_internal=True)


@router.get("/api/admin/customers", response_model=AdminUserLookupResponse)
def admin_lookup_customers(
    db: DbSession,
    admin: AdminUser,
    q: Optional[str] = Query(None, max_length=120),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    query = db.query(models.Person)
    if q:
        needle = f"%{q.lower().strip()}%"
        query = query.filter(
            (func.lower(models.Person.user_name).like(needle))
            | (func.lower(models.Person.email).like(needle))
        )
    total = query.count()
    users = query.order_by(models.Person.user_name.asc()).offset(skip).limit(limit).all()
    out: list[AdminUserSummary] = []
    for u in users:
        created_tickets = db.query(models.SupportTicket.ticket_id).filter(models.SupportTicket.requester_user_id == u.user_id).count()
        task_count = db.query(models.Task.task_id).filter(models.Task.user_id == u.user_id).count()
        out.append(
            AdminUserSummary(
                user_id=u.user_id,
                username=u.user_name,
                email=u.email or "",
                role=(u.role or "user"),
                account_locked=bool(u.account_locked),
                created_tickets=created_tickets,
                total_tasks=task_count,
            )
        )
    return AdminUserLookupResponse(users=out, customers=out, total=total)


@router.get("/api/admin/customers/{user_id}", response_model=AdminUserSummary)
def admin_customer_detail(user_id: UUID, db: DbSession, admin: AdminUser):
    user = db.query(models.Person).filter(models.Person.user_id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    created_tickets = db.query(models.SupportTicket.ticket_id).filter(models.SupportTicket.requester_user_id == user.user_id).count()
    task_count = db.query(models.Task.task_id).filter(models.Task.user_id == user.user_id).count()
    return AdminUserSummary(
        user_id=user.user_id,
        username=user.user_name,
        email=user.email or "",
        role=(user.role or "user"),
        account_locked=bool(user.account_locked),
        created_tickets=created_tickets,
        total_tasks=task_count,
    )


@router.patch("/api/admin/customers/{user_id}/actions", response_model=AdminUserSummary)
def admin_customer_actions(user_id: UUID, body: AdminUserActionBody, db: DbSession, admin: AdminUser):
    user = db.query(models.Person).filter(models.Person.user_id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    patch = body.model_dump(exclude_unset=True)
    if "lock_account" in patch and patch["lock_account"] is not None:
        user.account_locked = bool(patch["lock_account"])
    if "admin_note" in patch:
        user.admin_note = (patch["admin_note"] or "").strip()[:2000] or None
    if "mfa_enabled" in patch and patch["mfa_enabled"] is not None:
        user.mfa_enabled = bool(patch["mfa_enabled"])
    db.add(user)
    _audit(db, admin, "admin_customer_action", "person", str(user.user_id), {"patch": patch})
    db.commit()
    created_tickets = db.query(models.SupportTicket.ticket_id).filter(models.SupportTicket.requester_user_id == user.user_id).count()
    task_count = db.query(models.Task.task_id).filter(models.Task.user_id == user.user_id).count()
    return AdminUserSummary(
        user_id=user.user_id,
        username=user.user_name,
        email=user.email or "",
        role=(user.role or "user"),
        account_locked=bool(user.account_locked),
        created_tickets=created_tickets,
        total_tasks=task_count,
    )

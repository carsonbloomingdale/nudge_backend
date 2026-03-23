"""User-facing journal API: one journal embeds many task rows (analytics / personality)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session, joinedload

import journal_storage
import models
from auth_deps import CurrentUser
from database import DbSession
from journal_service import delete_journal_attachments_from_storage, insert_journal_with_tasks
from personality_analytics import invalidate_personality_chart_cache
from task_schemas import PersonalityTraitItem

router = APIRouter(prefix="/api/journals", tags=["journals"])


class JournalItemBody(BaseModel):
    """One embedded line (stored as Task)."""

    sentiment: str
    category: str
    label: str
    context: str
    time_of_day: str
    amount_of_time: str
    day_of_week: str
    personality_traits: List[str] = Field(default_factory=list, max_length=5)

    @field_validator("personality_traits", mode="before")
    @classmethod
    def _cap_journal_trait_strings(cls, v: object) -> object:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        out: List[str] = []
        for item in v[:5]:
            s = str(item).strip()[:80]
            if s:
                out.append(s)
        return out


class JournalCreateBody(BaseModel):
    items: List[JournalItemBody] = Field(min_length=1, max_length=50)
    note: Optional[str] = Field(
        None,
        max_length=8000,
        validation_alias=AliasChoices("note", "notes", "body", "content"),
    )
    source: str = Field(default="app", max_length=32)

    @field_validator("note", mode="before")
    @classmethod
    def _normalize_note(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return v


class TaskLinePublic(BaseModel):
    task_id: int
    journal_id: Optional[int]
    sentiment: str
    category: str
    label: str
    context: str
    time_of_day: str
    amount_of_time: str
    day_of_week: str
    personality_traits: List[PersonalityTraitItem] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class AttachmentPublic(BaseModel):
    attachment_id: int
    journal_id: int
    content_type: str
    byte_size: Optional[int] = None
    created_at: datetime
    upload_completed_at: Optional[datetime] = None
    download_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class JournalPublic(BaseModel):
    journal_id: int
    user_id: UUID
    submitted_at: datetime
    source: str
    note: Optional[str] = None
    tasks: List[TaskLinePublic] = Field(default_factory=list)
    attachments: List[AttachmentPublic] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class JournalPatchBody(BaseModel):
    note: Optional[str] = Field(
        None,
        max_length=8000,
        validation_alias=AliasChoices("note", "notes", "body", "content"),
    )

    @field_validator("note", mode="before")
    @classmethod
    def _normalize_patch_note(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return v


class PresignBody(BaseModel):
    content_type: str = Field(min_length=3, max_length=128)
    byte_size: int = Field(ge=1, le=journal_storage.max_upload_bytes())


class PresignResponse(BaseModel):
    attachment_id: int
    storage_key: str
    upload_url: str
    expires_in: int


class CompleteBody(BaseModel):
    byte_size: int = Field(ge=1, le=journal_storage.max_upload_bytes())


def _attachment_to_public(att: models.JournalAttachment) -> AttachmentPublic:
    download_url: Optional[str] = None
    if (
        att.upload_completed_at is not None
        and journal_storage.attachments_configured()
        and att.storage_key
    ):
        try:
            download_url = journal_storage.generate_presigned_get(att.storage_key)
        except Exception:
            download_url = None
    return AttachmentPublic(
        attachment_id=att.attachment_id,
        journal_id=att.journal_id,
        content_type=att.content_type,
        byte_size=att.byte_size,
        created_at=att.created_at,
        upload_completed_at=att.upload_completed_at,
        download_url=download_url,
    )


def _journal_to_public(j: models.Journal) -> JournalPublic:
    tasks = [TaskLinePublic.model_validate(t) for t in j.tasks]
    attachments = [_attachment_to_public(a) for a in j.attachments]
    return JournalPublic(
        journal_id=j.journal_id,
        user_id=j.user_id,
        submitted_at=j.submitted_at,
        source=j.source,
        note=j.note,
        tasks=tasks,
        attachments=attachments,
    )


def _get_journal_for_user(db: Session, journal_id: int, user_id: UUID) -> models.Journal:
    row = (
        db.query(models.Journal)
        .options(
            joinedload(models.Journal.tasks).joinedload(models.Task.personality_traits),
            joinedload(models.Journal.attachments),
        )
        .filter(models.Journal.journal_id == journal_id, models.Journal.user_id == user_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Journal not found")
    return row


@router.post("", response_model=JournalPublic)
@router.post("/", response_model=JournalPublic)
def create_journal(body: JournalCreateBody, db: DbSession, user: CurrentUser):
    items = [it.model_dump() for it in body.items]
    j = insert_journal_with_tasks(
        db,
        user_id=user.user_id,
        task_field_dicts=items,
        source=body.source.strip() or "app",
        note=body.note,
    )
    db.commit()
    db.refresh(j)
    invalidate_personality_chart_cache(db, user.user_id)
    db.commit()
    j = _get_journal_for_user(db, j.journal_id, user.user_id)
    return _journal_to_public(j)


@router.get("", response_model=List[JournalPublic])
@router.get("/", response_model=List[JournalPublic])
def list_journals(
    db: DbSession,
    user: CurrentUser,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    rows = (
        db.query(models.Journal)
        .options(
            joinedload(models.Journal.tasks).joinedload(models.Task.personality_traits),
            joinedload(models.Journal.attachments),
        )
        .filter(models.Journal.user_id == user.user_id)
        .order_by(models.Journal.submitted_at.desc(), models.Journal.journal_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [_journal_to_public(j) for j in rows]


@router.get("/{journal_id}", response_model=JournalPublic)
def get_journal(journal_id: int, db: DbSession, user: CurrentUser):
    j = _get_journal_for_user(db, journal_id, user.user_id)
    return _journal_to_public(j)


@router.patch("/{journal_id}", response_model=JournalPublic)
def patch_journal(journal_id: int, body: JournalPatchBody, db: DbSession, user: CurrentUser):
    j = _get_journal_for_user(db, journal_id, user.user_id)
    patch = body.model_dump(exclude_unset=True)
    if "note" in patch:
        n = patch["note"]
        j.note = n.strip() if isinstance(n, str) and n.strip() else None
    db.add(j)
    db.commit()
    db.refresh(j)
    invalidate_personality_chart_cache(db, user.user_id)
    db.commit()
    j = _get_journal_for_user(db, j.journal_id, user.user_id)
    return _journal_to_public(j)


@router.delete("/{journal_id}", status_code=204)
def delete_journal(journal_id: int, db: DbSession, user: CurrentUser):
    j = (
        db.query(models.Journal)
        .options(joinedload(models.Journal.attachments))
        .filter(models.Journal.journal_id == journal_id, models.Journal.user_id == user.user_id)
        .first()
    )
    if j is None:
        raise HTTPException(status_code=404, detail="Journal not found")
    delete_journal_attachments_from_storage(db, j)
    db.delete(j)
    db.commit()
    invalidate_personality_chart_cache(db, user.user_id)
    db.commit()
    return Response(status_code=204)


_ALLOWED_CT = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif"}
)


@router.post("/{journal_id}/attachments/presign", response_model=PresignResponse)
def presign_attachment_upload(journal_id: int, body: PresignBody, db: DbSession, user: CurrentUser):
    if not journal_storage.attachments_configured():
        raise HTTPException(
            status_code=503,
            detail="Photo attachments are not configured (set S3_ATTACHMENTS_BUCKET and AWS credentials).",
        )
    ct = body.content_type.lower().split(";")[0].strip()
    if ct not in _ALLOWED_CT:
        raise HTTPException(status_code=400, detail=f"Unsupported content_type (allowed: {sorted(_ALLOWED_CT)})")
    _get_journal_for_user(db, journal_id, user.user_id)
    uid = str(user.user_id)
    att = models.JournalAttachment(
        journal_id=journal_id,
        storage_key="",
        content_type=ct,
        byte_size=None,
        upload_completed_at=None,
    )
    db.add(att)
    db.flush()
    key = journal_storage.build_storage_key(uid, journal_id, att.attachment_id, ct)
    att.storage_key = key
    db.add(att)
    db.commit()
    db.refresh(att)
    try:
        url = journal_storage.generate_presigned_put(key, ct)
    except Exception as exc:
        db.delete(att)
        db.commit()
        raise HTTPException(status_code=503, detail=f"Could not create upload URL: {exc!s}") from exc
    meta = journal_storage.presign_put_meta()
    return PresignResponse(
        attachment_id=att.attachment_id,
        storage_key=key,
        upload_url=url,
        expires_in=meta["expires_in"],
    )


@router.post("/{journal_id}/attachments/{attachment_id}/complete", response_model=AttachmentPublic)
def complete_attachment_upload(
    journal_id: int,
    attachment_id: int,
    body: CompleteBody,
    db: DbSession,
    user: CurrentUser,
):
    _get_journal_for_user(db, journal_id, user.user_id)
    att = (
        db.query(models.JournalAttachment)
        .filter(
            models.JournalAttachment.attachment_id == attachment_id,
            models.JournalAttachment.journal_id == journal_id,
        )
        .first()
    )
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if att.upload_completed_at is not None:
        raise HTTPException(status_code=409, detail="Attachment already completed")

    att.byte_size = body.byte_size
    att.upload_completed_at = datetime.now(timezone.utc)
    db.add(att)
    db.commit()
    db.refresh(att)
    return _attachment_to_public(att)

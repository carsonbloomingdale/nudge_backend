"""Aggregated personality-trait counts + optional AI grouping for chart UIs."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from auth_deps import CurrentUser
from database import DbSession
from openai_client import OPENAI_MODEL, openai_chat_completion

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


class RawTraitAggregate(BaseModel):
    label: str
    count: int


class ChartSegment(BaseModel):
    """One slice/bar for the UI; counts sum task–trait links grouped under this segment."""

    id: str
    display_label: str
    count: int
    percentage: float = Field(description="Share of all trait associations (0-100).")
    member_labels: List[str] = Field(default_factory=list)


class PersonalityTraitChartResponse(BaseModel):
    total_associations: int
    raw_aggregates: List[RawTraitAggregate]
    segments: List[ChartSegment]
    chart_mode: Literal["ai", "raw_only"]
    meta: Optional[dict[str, Any]] = None
    aggregate_source: Literal["personality_traits", "task_category", "none"] = "none"


class PinnedTraitItem(BaseModel):
    pin_id: int
    label: str
    created_at: datetime


class PinTraitRequest(BaseModel):
    label: str = Field(min_length=1, max_length=120)


class SyncPinnedTraitsRequest(BaseModel):
    labels: List[str] = Field(default_factory=list, max_length=50)


class PinnedTraitsResponse(BaseModel):
    traits: List[PinnedTraitItem]


def _slug_id(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (label or "").lower().strip()).strip("_")
    h = hashlib.md5((label or "").encode("utf-8")).hexdigest()[:8]
    return (s[:40] or "trait") + "_" + h


def _query_raw_aggregates(db: Session, user_id: UUID) -> List[RawTraitAggregate]:
    rows = (
        db.query(models.PersonalityTrait.label, func.count(models.PersonalityTrait.trait_id))
        .join(models.Task, models.PersonalityTrait.task_id == models.Task.task_id)
        .filter(models.Task.user_id == user_id)
        .filter(models.PersonalityTrait.label.isnot(None))
        .filter(models.PersonalityTrait.label != "")
        .group_by(models.PersonalityTrait.label)
        .order_by(func.count(models.PersonalityTrait.trait_id).desc())
        .all()
    )
    out: List[RawTraitAggregate] = []
    for label, cnt in rows:
        out.append(RawTraitAggregate(label=str(label).strip(), count=int(cnt)))
    return out


def _query_task_category_fallback(db: Session, user_id: UUID) -> List[RawTraitAggregate]:
    """When personality_traits rows are missing (e.g. legacy journals), group by task.category."""
    rows = (
        db.query(models.Task.category, func.count(models.Task.task_id))
        .filter(models.Task.user_id == user_id)
        .filter(models.Task.category.isnot(None))
        .filter(models.Task.category != "")
        .group_by(models.Task.category)
        .order_by(func.count(models.Task.task_id).desc())
        .all()
    )
    out: List[RawTraitAggregate] = []
    for cat, cnt in rows:
        lab = str(cat).strip()
        if lab:
            out.append(RawTraitAggregate(label=lab, count=int(cnt)))
    return out


def trait_snapshot_for_user(db: Session, user_id: UUID, *, limit: int = 15) -> List[Dict[str, Any]]:
    """Compact trait/category counts for enrichment summary and prompts (same source as personality chart raw aggregates)."""
    raw = _query_raw_aggregates(db, user_id)
    if not raw:
        raw = _query_task_category_fallback(db, user_id)
    return [{"label": r.label, "count": r.count} for r in raw[:limit]]


def _normalize_pinned_trait_label(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "").strip())
    s = re.sub(r"\btraits?\b$", "", s, flags=re.IGNORECASE).strip(" -_.,;:()[]{}")
    if not s:
        raise ValueError("Trait label cannot be blank")
    if re.search(r"(?:,|/|&|\band\b)", s, flags=re.IGNORECASE):
        raise ValueError("Pinned trait must be a single trait (no commas, '&', '/', or 'and').")
    return s[:80]


def pinned_trait_labels_for_user(db: Session, user_id: UUID, *, limit: int = 20) -> List[str]:
    rows = (
        db.query(models.PinnedPersonalityTrait.label)
        .filter(models.PinnedPersonalityTrait.user_id == user_id)
        .order_by(models.PinnedPersonalityTrait.created_at.desc(), models.PinnedPersonalityTrait.pin_id.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    out: List[str] = []
    for (label,) in rows:
        s = str(label or "").strip()
        if s:
            out.append(s)
    return out


def _segments_raw_only(raw: List[RawTraitAggregate]) -> List[ChartSegment]:
    total = sum(r.count for r in raw)
    segs: List[ChartSegment] = []
    for r in raw:
        pct = (100.0 * r.count / total) if total else 0.0
        segs.append(
            ChartSegment(
                id=_slug_id(r.label),
                display_label=r.label[:120] or "Unknown",
                count=r.count,
                percentage=round(pct, 2),
                member_labels=[r.label],
            )
        )
    return segs


def _clean_segment_label(label: str) -> str:
    """Keep chart labels short and avoid redundant 'Trait' wording."""
    s = re.sub(r"\s+", " ", (label or "").strip())
    s = re.sub(r"\btraits?\b$", "", s, flags=re.IGNORECASE).strip(" -_.,;:()[]{}")
    if re.search(r"[,&/]", s) or re.search(r"\band\b", s, flags=re.IGNORECASE):
        token = re.split(r"\s*(?:,|/|&|\band\b)\s*", s, maxsplit=1, flags=re.IGNORECASE)[0]
        s = token.strip()
    return (s or "Unknown")[:120]


def _build_ai_prompt(traits_payload: list[dict[str, Any]]) -> tuple[str, str]:
    system = (
        "You group free-form personality trait labels for a chart. Return strict JSON only. "
        'Shape: {"segments":[{"id":"snake_case","display_label":"Short UI Title",'
        '"members":["exact input label", ...]}]}. '
        "Rules: (1) Every input label must appear in exactly one segment's members, "
        "using the EXACT same string as provided (case-sensitive). "
        "(2) Merge synonyms and near-duplicates. "
        "(3) Prefer 4–12 segments when there are many labels; fewer is fine if inputs are distinct. "
        "(4) id uses lowercase letters, digits, underscores only. "
        "(5) display_label must be a single concise trait phrase, not a combined list. "
        "(6) Do not include the words 'trait' or 'traits' in display_label."
    )
    user = json.dumps({"traits": traits_payload})
    return system, user


def _segments_from_ai(
    raw: List[RawTraitAggregate], ai_payload: dict[str, Any]
) -> tuple[List[ChartSegment], bool]:
    raw_map = {r.label: r.count for r in raw}
    total = sum(raw_map.values())
    covered: set[str] = set()
    segments_out: List[ChartSegment] = []

    for seg in ai_payload.get("segments", []):
        if not isinstance(seg, dict):
            continue
        sid = str(seg.get("id", "")).strip() or "segment"
        disp = str(seg.get("display_label", "")).strip() or sid
        members_in = seg.get("members", [])
        if not isinstance(members_in, list):
            continue
        members: List[str] = []
        count = 0
        for m in members_in:
            if not isinstance(m, str):
                continue
            key = m.strip()
            if key in raw_map:
                members.append(key)
                count += raw_map[key]
                covered.add(key)
        if count <= 0 and not members:
            continue
        pct = (100.0 * count / total) if total else 0.0
        segments_out.append(
            ChartSegment(
                id=re.sub(r"[^a-z0-9_]", "_", sid.lower())[:64] or _slug_id(disp),
                display_label=_clean_segment_label(disp),
                count=count,
                percentage=round(pct, 2),
                member_labels=members,
            )
        )

    missing = [r for r in raw if r.label not in covered]
    for r in missing:
        pct = (100.0 * r.count / total) if total else 0.0
        segments_out.append(
            ChartSegment(
                id=_slug_id(r.label),
                display_label=r.label[:120],
                count=r.count,
                percentage=round(pct, 2),
                member_labels=[r.label],
            )
        )
    repaired = len(missing) > 0
    return segments_out, repaired


def get_chart_cache(db: Session, user_id: UUID, use_ai: bool) -> Optional[dict[str, Any]]:
    row = db.query(models.PersonalityChartCache).filter(models.PersonalityChartCache.user_id == user_id).first()
    if row is None:
        return None
    blob = row.payload_ai if use_ai else row.payload_raw
    return blob if isinstance(blob, dict) else None


def save_chart_cache(db: Session, user_id: UUID, use_ai: bool, payload: dict[str, Any]) -> None:
    row = db.query(models.PersonalityChartCache).filter(models.PersonalityChartCache.user_id == user_id).first()
    if row is None:
        row = models.PersonalityChartCache(user_id=user_id)
        db.add(row)
    if use_ai:
        row.payload_ai = payload
    else:
        row.payload_raw = payload
    db.commit()


def invalidate_personality_chart_cache(db: Session, user_id: UUID) -> None:
    """Drop cached chart(s) for a user (call after journal/task changes). Caller may commit."""
    db.query(models.PersonalityChartCache).filter(models.PersonalityChartCache.user_id == user_id).delete()


async def compute_personality_traits_chart(
    db: Session, user_id: UUID, use_ai: bool
) -> PersonalityTraitChartResponse:
    """Full aggregation + optional OpenAI (no cache read/write)."""
    raw = _query_raw_aggregates(db, user_id)
    aggregate_source: Literal["personality_traits", "task_category", "none"] = "personality_traits"
    if not raw:
        raw = _query_task_category_fallback(db, user_id)
        aggregate_source = "task_category" if raw else "none"

    total = sum(r.count for r in raw)

    if total == 0:
        return PersonalityTraitChartResponse(
            total_associations=0,
            raw_aggregates=[],
            segments=[],
            chart_mode="raw_only",
            aggregate_source="none",
            meta={
                "message": "No tasks yet, or no personality_traits and no categories to aggregate.",
            },
        )

    base_meta: dict[str, Any] = {}
    if aggregate_source == "task_category":
        base_meta["note"] = (
            "Counts are grouped by task.category because there are no rows in personality_traits yet. "
            "Send personality_traits on POST /tasks/ or journal items to populate trait-level data."
        )

    traits_payload = [{"label": r.label, "count": r.count} for r in raw]

    if not use_ai:
        segs = _segments_raw_only(raw)
        return PersonalityTraitChartResponse(
            total_associations=total,
            raw_aggregates=raw,
            segments=segs,
            chart_mode="raw_only",
            aggregate_source=aggregate_source,
            meta={**base_meta, "model": None},
        )

    try:
        system_prompt, user_prompt = _build_ai_prompt(traits_payload)
        ai_payload, retries_used = await openai_chat_completion(system_prompt, user_prompt, temperature=0.2)
    except HTTPException:
        segs = _segments_raw_only(raw)
        return PersonalityTraitChartResponse(
            total_associations=total,
            raw_aggregates=raw,
            segments=segs,
            chart_mode="raw_only",
            aggregate_source=aggregate_source,
            meta={**base_meta, "fallback": "openai_unavailable", "model": OPENAI_MODEL},
        )

    segs, repaired = _segments_from_ai(raw, ai_payload if isinstance(ai_payload, dict) else {})
    return PersonalityTraitChartResponse(
        total_associations=total,
        raw_aggregates=raw,
        segments=segs,
        chart_mode="ai",
        aggregate_source=aggregate_source,
        meta={
            **base_meta,
            "model": OPENAI_MODEL,
            "retries_used": retries_used,
            "repaired_missing_members": repaired,
        },
    )


@router.get("/personality-traits-chart", response_model=PersonalityTraitChartResponse)
async def personality_traits_chart(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    use_ai: bool = Query(True, description="If true, cluster labels with OpenAI; otherwise one segment per raw label."),
):
    from main import _enforce_rate_limit

    client_key = f"{request.client.host if request.client else 'unknown'}:/api/analytics/personality-traits-chart"
    _enforce_rate_limit(client_key)

    cached = get_chart_cache(db, user.user_id, use_ai)
    if cached is not None:
        data = dict(cached)
        meta = dict(data.get("meta") or {})
        meta["cached"] = True
        data["meta"] = meta
        return PersonalityTraitChartResponse.model_validate(data)

    response = await compute_personality_traits_chart(db, user.user_id, use_ai)
    save_chart_cache(db, user.user_id, use_ai, response.model_dump())
    return response


@router.get("/pinned-traits", response_model=PinnedTraitsResponse)
async def list_pinned_traits(db: DbSession, user: CurrentUser):
    rows = (
        db.query(models.PinnedPersonalityTrait)
        .filter(models.PinnedPersonalityTrait.user_id == user.user_id)
        .order_by(models.PinnedPersonalityTrait.created_at.desc(), models.PinnedPersonalityTrait.pin_id.desc())
        .all()
    )
    return PinnedTraitsResponse(traits=[PinnedTraitItem.model_validate(r, from_attributes=True) for r in rows])


@router.post("/pinned-traits", response_model=PinnedTraitItem)
async def pin_trait(body: PinTraitRequest, db: DbSession, user: CurrentUser):
    try:
        label = _normalize_pinned_trait_label(body.label)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing = (
        db.query(models.PinnedPersonalityTrait)
        .filter(models.PinnedPersonalityTrait.user_id == user.user_id, models.PinnedPersonalityTrait.label == label)
        .first()
    )
    if existing is not None:
        return PinnedTraitItem.model_validate(existing, from_attributes=True)

    row = models.PinnedPersonalityTrait(user_id=user.user_id, label=label)
    db.add(row)
    db.commit()
    db.refresh(row)
    return PinnedTraitItem.model_validate(row, from_attributes=True)


@router.delete("/pinned-traits/{label}", response_model=PinnedTraitsResponse)
async def unpin_trait(label: str, db: DbSession, user: CurrentUser):
    try:
        normalized = _normalize_pinned_trait_label(label)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    (
        db.query(models.PinnedPersonalityTrait)
        .filter(models.PinnedPersonalityTrait.user_id == user.user_id, models.PinnedPersonalityTrait.label == normalized)
        .delete()
    )
    db.commit()
    rows = (
        db.query(models.PinnedPersonalityTrait)
        .filter(models.PinnedPersonalityTrait.user_id == user.user_id)
        .order_by(models.PinnedPersonalityTrait.created_at.desc(), models.PinnedPersonalityTrait.pin_id.desc())
        .all()
    )
    return PinnedTraitsResponse(traits=[PinnedTraitItem.model_validate(r, from_attributes=True) for r in rows])


@router.put("/pinned-traits", response_model=PinnedTraitsResponse)
async def sync_pinned_traits(body: SyncPinnedTraitsRequest, db: DbSession, user: CurrentUser):
    normalized: List[str] = []
    seen: set[str] = set()
    for raw in body.labels:
        try:
            label = _normalize_pinned_trait_label(raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(label)

    db.query(models.PinnedPersonalityTrait).filter(models.PinnedPersonalityTrait.user_id == user.user_id).delete()
    for label in normalized:
        db.add(models.PinnedPersonalityTrait(user_id=user.user_id, label=label))
    db.commit()

    rows = (
        db.query(models.PinnedPersonalityTrait)
        .filter(models.PinnedPersonalityTrait.user_id == user.user_id)
        .order_by(models.PinnedPersonalityTrait.created_at.desc(), models.PinnedPersonalityTrait.pin_id.desc())
        .all()
    )
    return PinnedTraitsResponse(traits=[PinnedTraitItem.model_validate(r, from_attributes=True) for r in rows])

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Literal, Optional
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

import models
from auth_deps import CurrentUser
from database import DbSession

router = APIRouter(tags=["growth-goals", "analytics"])

Grain = Literal["day", "week", "month"]


class GoalSourceTask(BaseModel):
    task_id: int
    label: str
    category: str


class GrowthGoalSuggestionItem(BaseModel):
    goal_id: int
    slug: str
    label: str
    score: int
    reason: str
    goal_type: str = "short_term_action"
    horizon: str = "this_week"
    related_traits: list[str] = Field(default_factory=list)
    source_tasks: list[GoalSourceTask] = Field(default_factory=list)


class GrowthGoalSuggestionResponse(BaseModel):
    suggestions: list[GrowthGoalSuggestionItem]


class PinnedGrowthGoalItem(BaseModel):
    pin_id: int
    goal_id: int
    slug: str
    label: str
    created_at: datetime


class PinnedGrowthGoalsResponse(BaseModel):
    goals: list[PinnedGrowthGoalItem]


class RollupBucket(BaseModel):
    period_start: date
    total: int


class ActivitySeriesResponse(BaseModel):
    grain: Grain
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    total: int
    buckets: list[RollupBucket]


class TraitTotalItem(BaseModel):
    trait_label: str
    total: int


class TraitTotalsByLabelResponse(BaseModel):
    grain: Grain
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    traits: list[TraitTotalItem]


NormalizationMode = Literal["preview", "apply"]
NormalizationStrategy = Literal["casefold", "alias_map", "pinned_priority"]


class TraitNormalizationRequest(BaseModel):
    mode: NormalizationMode = "preview"
    strategy: NormalizationStrategy = "pinned_priority"
    alias_map: dict[str, str] = Field(default_factory=dict)
    target_labels: list[str] = Field(default_factory=list, max_length=200)


class TraitMergeItem(BaseModel):
    from_label: str
    to_label: str
    rows: int


class TraitNormalizationResponse(BaseModel):
    mode: NormalizationMode
    strategy: NormalizationStrategy
    updated_rows: int = 0
    pinned_rows_deleted: int = 0
    merges: list[TraitMergeItem] = Field(default_factory=list)
    before_top: list[TraitTotalItem] = Field(default_factory=list)
    after_top: list[TraitTotalItem] = Field(default_factory=list)
    rollup_stats: Optional[dict[str, int]] = None


def _normalize_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return slug[:120] or "goal"


def _normalize_goal_label(text: str) -> str:
    s = re.sub(r"\s+", " ", (text or "").strip())
    return s[:160] or "Growth Goal"


def _normalize_trait_label(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "").strip())
    s = re.sub(r"\btraits?\b$", "", s, flags=re.IGNORECASE).strip(" -_.,;:()[]{}")
    return s[:120]


def _trait_key(raw: str) -> str:
    s = _normalize_trait_label(raw).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _goal_blueprints_for_task(task: models.Task) -> list[tuple[str, str, list[str]]]:
    """Return tangible short-term goals: (goal_label, reason, related_traits)."""
    items: list[tuple[str, str, list[str]]] = []
    category = str(task.category or "").strip()
    text = f"{task.label or ''} {task.context or ''}".lower()
    c = category.lower()

    # Category-first mapping: short-term actionable goals, not personality labels.
    category_map: dict[str, tuple[str, str, list[str]]] = {
        "leisure": ("Schedule two leisure blocks this week", "Recent tasks show recovery/play activity.", ["Balanced", "Mindful"]),
        "health": ("Complete three 20-minute movement sessions this week", "Recent tasks show health momentum.", ["Disciplined", "Wellness Focused"]),
        "self-care": ("Do one intentional self-care block per day this week", "Recent tasks show self-care intent.", ["Mindful", "Self-aware"]),
        "work": ("Finish one priority work deliverable today", "Recent tasks show ongoing work threads.", ["Focused", "Organized"]),
        "social": ("Reach out to one person each day this week", "Recent tasks show social engagement.", ["Supportive", "Connected"]),
        "reflection": ("Write a 10-minute reflection on three days this week", "Recent tasks show reflection patterns.", ["Reflective", "Self-aware"]),
        "self-reflection": ("Write a 10-minute reflection on three days this week", "Recent tasks show reflection patterns.", ["Reflective", "Self-aware"]),
        "exercise": ("Complete three 20-minute movement sessions this week", "Recent tasks show exercise behavior.", ["Disciplined", "Wellness Focused"]),
        "administration": ("Complete one admin task each workday this week", "Recent tasks include recurring admin chores.", ["Organized", "Reliable"]),
        "cooking": ("Prep two simple home meals this week", "Recent tasks show home cooking behavior.", ["Practical", "Wellness Focused"]),
    }
    if c in category_map:
        items.append(category_map[c])

    keyword_map: dict[str, tuple[str, str, list[str]]] = {
        "exercise": ("Complete three 20-minute movement sessions this week", "Keyword signal: exercise/workout movement.", ["Disciplined", "Wellness Focused"]),
        "workout": ("Complete three 20-minute movement sessions this week", "Keyword signal: exercise/workout movement.", ["Disciplined", "Wellness Focused"]),
        "run": ("Complete three 20-minute movement sessions this week", "Keyword signal: running/movement.", ["Disciplined", "Wellness Focused"]),
        "sleep": ("Keep a consistent bedtime on five nights this week", "Keyword signal: sleep routine.", ["Wellness Focused", "Disciplined"]),
        "focus": ("Run two distraction-free 45-minute focus blocks this week", "Keyword signal: focus/deep work.", ["Focused", "Organized"]),
        "study": ("Complete two focused learning sessions this week", "Keyword signal: study/learning.", ["Curious", "Disciplined"]),
        "learn": ("Complete two focused learning sessions this week", "Keyword signal: study/learning.", ["Curious", "Disciplined"]),
        "journal": ("Write one reflection entry each evening for three days", "Keyword signal: journaling.", ["Reflective", "Self-aware"]),
        "plan": ("Create a weekly plan and review it mid-week", "Keyword signal: planning behavior.", ["Organized", "Focused"]),
    }
    for kw, item in keyword_map.items():
        if kw in text:
            items.append(item)

    # Generic fallback for uncategorized items.
    if not items and c and c not in {"other", "unspecified", "none", "n/a"}:
        items.append(
            (
                f"Complete one concrete {category.title()} action each day this week",
                "Derived from repeated category activity.",
                [],
            )
        )
    return items


def _infer_goal_labels_for_task(task: models.Task, pinned_goal_labels: Optional[list[str]] = None) -> list[str]:
    labels = [_normalize_goal_label(lbl) for (lbl, _reason, _traits) in _goal_blueprints_for_task(task)]

    # Prefer pinned goals when task text/category aligns with them.
    if pinned_goal_labels:
        category = str(task.category or "").strip()
        text = f"{task.label or ''} {task.context or ''}".lower()
        category_token = category.lower()
        text_tokens = set(re.findall(r"[a-z0-9]+", text))
        for pinned in pinned_goal_labels:
            pinned_norm = _normalize_goal_label(pinned)
            pinned_tokens = {
                t
                for t in re.findall(r"[a-z0-9]+", pinned_norm.lower())
                if t not in {"improve", "build", "consistency", "goal", "goals"}
            }
            if not pinned_tokens:
                continue
            if category_token and category_token in pinned_tokens:
                labels.append(pinned_norm)
                continue
            if text_tokens.intersection(pinned_tokens):
                labels.append(pinned_norm)

    deduped: list[str] = []
    seen: set[str] = set()
    for item in labels:
        norm = _normalize_goal_label(item)
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(norm)
    return deduped[:3]


def _ensure_goal(db: Session, label: str) -> models.GrowthGoal:
    norm_label = _normalize_goal_label(label)
    slug = _normalize_slug(norm_label)
    row = db.query(models.GrowthGoal).filter(models.GrowthGoal.slug == slug).first()
    if row is not None:
        return row
    row = models.GrowthGoal(slug=slug, label=norm_label)
    db.add(row)
    db.flush()
    return row


def _local_date_for_task(task: models.Task, timezone_name: str) -> Optional[date]:
    if task.journal is None or task.journal.submitted_at is None:
        return None
    ts = task.journal.submitted_at
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return ts.astimezone(tz).date() if ts.tzinfo else ts.date()


def _period_start(d: date, grain: Grain) -> date:
    if grain == "day":
        return d
    if grain == "week":
        return d - timedelta(days=d.weekday())
    return d.replace(day=1)


def refresh_user_goal_trait_rollups(db: Session, user_id: UUID) -> dict[str, int]:
    pinned_goal_labels = (
        db.query(models.GrowthGoal.label)
        .join(models.PinnedGrowthGoal, models.PinnedGrowthGoal.goal_id == models.GrowthGoal.goal_id)
        .filter(models.PinnedGrowthGoal.user_id == user_id)
        .all()
    )
    pinned_goal_labels = [str(label) for (label,) in pinned_goal_labels]

    user = db.query(models.Person).filter(models.Person.user_id == user_id).first()
    timezone_name = user.timezone if user and user.timezone else "UTC"

    tasks = (
        db.query(models.Task)
        .options(
            joinedload(models.Task.personality_traits),
            joinedload(models.Task.journal),
        )
        .filter(models.Task.user_id == user_id)
        .all()
    )

    task_ids = [t.task_id for t in tasks]
    if task_ids:
        (
            db.query(models.TaskGrowthGoalLink)
            .filter(models.TaskGrowthGoalLink.task_id.in_(task_ids), models.TaskGrowthGoalLink.source == "heuristic")
            .delete(synchronize_session=False)
        )
        db.flush()

    for task in tasks:
        for goal_label in _infer_goal_labels_for_task(task, pinned_goal_labels):
            goal = _ensure_goal(db, goal_label)
            db.add(
                models.TaskGrowthGoalLink(
                    task_id=task.task_id,
                    goal_id=goal.goal_id,
                    confidence=0.65,
                    source="heuristic",
                )
            )
    db.flush()

    tasks = (
        db.query(models.Task)
        .options(
            joinedload(models.Task.personality_traits),
            joinedload(models.Task.journal),
        )
        .filter(models.Task.user_id == user_id)
        .all()
    )
    goal_links = (
        db.query(models.TaskGrowthGoalLink.task_id, models.TaskGrowthGoalLink.goal_id)
        .join(models.Task, models.Task.task_id == models.TaskGrowthGoalLink.task_id)
        .filter(models.Task.user_id == user_id)
        .all()
    )
    goal_ids_by_task: dict[int, list[int]] = defaultdict(list)
    for task_id, goal_id in goal_links:
        goal_ids_by_task[int(task_id)].append(int(goal_id))

    goal_counts: dict[tuple[int, Grain, date], int] = defaultdict(int)
    trait_counts: dict[tuple[str, Grain, date], int] = defaultdict(int)
    skipped_without_journal = 0

    for task in tasks:
        local_d = _local_date_for_task(task, timezone_name)
        if local_d is None:
            skipped_without_journal += 1
            continue
        for grain in ("day", "week", "month"):
            bucket = _period_start(local_d, grain)
            for goal_id in goal_ids_by_task.get(task.task_id, []):
                goal_counts[(goal_id, grain, bucket)] += 1
            for trait in task.personality_traits or []:
                label = _normalize_trait_label(str(trait.label or ""))
                if label:
                    trait_counts[(label, grain, bucket)] += 1

    db.query(models.GrowthGoalActivityRollup).filter(models.GrowthGoalActivityRollup.user_id == user_id).delete()
    db.query(models.TraitActivityRollup).filter(models.TraitActivityRollup.user_id == user_id).delete()

    for (goal_id, grain, period), total in goal_counts.items():
        db.add(
            models.GrowthGoalActivityRollup(
                user_id=user_id,
                goal_id=goal_id,
                grain=grain,
                period_start=period,
                total=total,
            )
        )
    for (trait_label, grain, period), total in trait_counts.items():
        db.add(
            models.TraitActivityRollup(
                user_id=user_id,
                trait_label=trait_label,
                grain=grain,
                period_start=period,
                total=total,
            )
        )
    db.commit()

    return {
        "tasks_scanned": len(tasks),
        "goal_buckets": len(goal_counts),
        "trait_buckets": len(trait_counts),
        "tasks_skipped_without_journal": skipped_without_journal,
    }


def _ensure_rollups_if_missing(db: Session, user_id: UUID) -> None:
    goal_rows = (
        db.query(models.GrowthGoalActivityRollup.id)
        .filter(models.GrowthGoalActivityRollup.user_id == user_id)
        .limit(1)
        .all()
    )
    trait_rows = (
        db.query(models.TraitActivityRollup.id)
        .filter(models.TraitActivityRollup.user_id == user_id)
        .limit(1)
        .all()
    )
    has_tasks = (
        db.query(models.Task.task_id)
        .filter(models.Task.user_id == user_id)
        .limit(1)
        .all()
    )
    if goal_rows and trait_rows:
        return
    if not has_tasks:
        return
    refresh_user_goal_trait_rollups(db, user_id)


@router.get("/api/growth-goals/suggestions", response_model=GrowthGoalSuggestionResponse)
def suggest_growth_goals(
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(8, ge=1, le=20),
    lookback_tasks: int = Query(150, ge=20, le=500),
):
    tasks = (
        db.query(models.Task)
        .filter(models.Task.user_id == user.user_id)
        .order_by(models.Task.task_id.desc())
        .limit(lookback_tasks)
        .all()
    )
    score_by_goal: dict[str, int] = defaultdict(int)
    reason_by_goal: dict[str, str] = {}
    related_traits_by_goal: dict[str, set[str]] = defaultdict(set)
    source_by_goal: dict[str, list[GoalSourceTask]] = defaultdict(list)
    for t in tasks:
        blueprints = _goal_blueprints_for_task(t)
        if not blueprints:
            blueprints = [(_normalize_goal_label(x), "Derived from recent behavior.", []) for x in _infer_goal_labels_for_task(t)]
        for goal_label, reason, related_traits in blueprints:
            norm_goal = _normalize_goal_label(goal_label)
            score_by_goal[norm_goal] += 1
            if norm_goal not in reason_by_goal:
                reason_by_goal[norm_goal] = reason
            for trait in related_traits:
                s = _normalize_trait_label(trait)
                if s:
                    related_traits_by_goal[norm_goal].add(s)
            src = source_by_goal[norm_goal]
            if len(src) < 3:
                src.append(
                    GoalSourceTask(
                        task_id=t.task_id,
                        label=str(t.label or "")[:160],
                        category=str(t.category or "")[:80],
                    )
                )
    ordered = sorted(score_by_goal.items(), key=lambda x: (-x[1], x[0]))[:limit]
    suggestions: list[GrowthGoalSuggestionItem] = []
    for label, score in ordered:
        goal = _ensure_goal(db, label)
        reason = reason_by_goal.get(label) or f"Based on {score} recent task(s) with similar patterns."
        suggestions.append(
            GrowthGoalSuggestionItem(
                goal_id=goal.goal_id,
                slug=goal.slug,
                label=goal.label,
                score=score,
                reason=reason,
                related_traits=sorted(related_traits_by_goal.get(label, set()))[:6],
                source_tasks=source_by_goal[label],
            )
        )
    db.commit()
    return GrowthGoalSuggestionResponse(suggestions=suggestions)


@router.get("/api/growth-goals/pinned", response_model=PinnedGrowthGoalsResponse)
def list_pinned_growth_goals(db: DbSession, user: CurrentUser):
    rows = (
        db.query(models.PinnedGrowthGoal, models.GrowthGoal)
        .join(models.GrowthGoal, models.PinnedGrowthGoal.goal_id == models.GrowthGoal.goal_id)
        .filter(models.PinnedGrowthGoal.user_id == user.user_id)
        .order_by(models.PinnedGrowthGoal.created_at.desc(), models.PinnedGrowthGoal.pin_id.desc())
        .all()
    )
    return PinnedGrowthGoalsResponse(
        goals=[
            PinnedGrowthGoalItem(
                pin_id=pin.pin_id,
                goal_id=goal.goal_id,
                slug=goal.slug,
                label=goal.label,
                created_at=pin.created_at,
            )
            for pin, goal in rows
        ]
    )


@router.post("/api/growth-goals/{goal_id}/pin", response_model=PinnedGrowthGoalsResponse)
def pin_growth_goal(goal_id: int, db: DbSession, user: CurrentUser):
    goal = db.query(models.GrowthGoal).filter(models.GrowthGoal.goal_id == goal_id).first()
    if goal is None:
        raise HTTPException(status_code=404, detail="Growth goal not found")
    existing = (
        db.query(models.PinnedGrowthGoal)
        .filter(models.PinnedGrowthGoal.user_id == user.user_id, models.PinnedGrowthGoal.goal_id == goal_id)
        .first()
    )
    if existing is None:
        db.add(models.PinnedGrowthGoal(user_id=user.user_id, goal_id=goal_id))
        db.commit()
    return list_pinned_growth_goals(db, user)


@router.delete("/api/growth-goals/{goal_id}/pin", response_model=PinnedGrowthGoalsResponse)
def unpin_growth_goal(goal_id: int, db: DbSession, user: CurrentUser):
    (
        db.query(models.PinnedGrowthGoal)
        .filter(models.PinnedGrowthGoal.user_id == user.user_id, models.PinnedGrowthGoal.goal_id == goal_id)
        .delete()
    )
    db.commit()
    return list_pinned_growth_goals(db, user)


@router.post("/api/analytics/rollups/backfill")
def trigger_rollup_backfill(db: DbSession, user: CurrentUser):
    stats = refresh_user_goal_trait_rollups(db, user.user_id)
    return {"ok": True, "stats": stats}


@router.get("/api/analytics/growth-goals/{goal_id}/activity", response_model=ActivitySeriesResponse)
def growth_goal_activity(
    goal_id: int,
    db: DbSession,
    user: CurrentUser,
    grain: Grain = Query("day"),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
):
    _ensure_rollups_if_missing(db, user.user_id)
    q = (
        db.query(models.GrowthGoalActivityRollup)
        .filter(
            models.GrowthGoalActivityRollup.user_id == user.user_id,
            models.GrowthGoalActivityRollup.goal_id == goal_id,
            models.GrowthGoalActivityRollup.grain == grain,
        )
        .order_by(models.GrowthGoalActivityRollup.period_start.asc())
    )
    if from_date is not None:
        q = q.filter(models.GrowthGoalActivityRollup.period_start >= from_date)
    if to_date is not None:
        q = q.filter(models.GrowthGoalActivityRollup.period_start <= to_date)
    rows = q.all()
    buckets = [RollupBucket(period_start=r.period_start, total=r.total) for r in rows]
    return ActivitySeriesResponse(
        grain=grain,
        from_date=from_date,
        to_date=to_date,
        total=sum(x.total for x in buckets),
        buckets=buckets,
    )


@router.get("/api/analytics/traits/{trait_label}/activity", response_model=ActivitySeriesResponse)
def trait_activity(
    trait_label: str,
    db: DbSession,
    user: CurrentUser,
    grain: Grain = Query("day"),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
):
    _ensure_rollups_if_missing(db, user.user_id)
    normalized = _normalize_trait_label(trait_label)
    if not normalized:
        raise HTTPException(status_code=400, detail="Trait label is required")
    q = (
        db.query(models.TraitActivityRollup)
        .filter(
            models.TraitActivityRollup.user_id == user.user_id,
            func.lower(models.TraitActivityRollup.trait_label) == normalized.lower(),
            models.TraitActivityRollup.grain == grain,
        )
        .order_by(models.TraitActivityRollup.period_start.asc())
    )
    if from_date is not None:
        q = q.filter(models.TraitActivityRollup.period_start >= from_date)
    if to_date is not None:
        q = q.filter(models.TraitActivityRollup.period_start <= to_date)
    rows = q.all()
    buckets = [RollupBucket(period_start=r.period_start, total=r.total) for r in rows]
    return ActivitySeriesResponse(
        grain=grain,
        from_date=from_date,
        to_date=to_date,
        total=sum(x.total for x in buckets),
        buckets=buckets,
    )


@router.get("/api/analytics/traits/activity/by-label", response_model=TraitTotalsByLabelResponse)
def trait_totals_by_label(
    db: DbSession,
    user: CurrentUser,
    grain: Grain = Query("day"),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    limit: int = Query(500, ge=1, le=500),
):
    _ensure_rollups_if_missing(db, user.user_id)
    q = (
        db.query(
            models.TraitActivityRollup.trait_label,
            func.sum(models.TraitActivityRollup.total).label("total_sum"),
        )
        .filter(
            models.TraitActivityRollup.user_id == user.user_id,
            models.TraitActivityRollup.grain == grain,
        )
    )
    if from_date is not None:
        q = q.filter(models.TraitActivityRollup.period_start >= from_date)
    if to_date is not None:
        q = q.filter(models.TraitActivityRollup.period_start <= to_date)
    rows = (
        q.group_by(models.TraitActivityRollup.trait_label)
        .order_by(func.sum(models.TraitActivityRollup.total).desc(), models.TraitActivityRollup.trait_label.asc())
        .limit(limit)
        .all()
    )
    traits = [TraitTotalItem(trait_label=str(label), total=int(total_sum or 0)) for label, total_sum in rows]
    return TraitTotalsByLabelResponse(
        grain=grain,
        from_date=from_date,
        to_date=to_date,
        traits=traits,
    )


def _trait_counts_for_user(db: Session, user_id: UUID) -> list[tuple[str, int]]:
    rows = (
        db.query(models.PersonalityTrait.label, func.count(models.PersonalityTrait.trait_id))
        .join(models.Task, models.Task.task_id == models.PersonalityTrait.task_id)
        .filter(models.Task.user_id == user_id)
        .group_by(models.PersonalityTrait.label)
        .order_by(func.count(models.PersonalityTrait.trait_id).desc(), models.PersonalityTrait.label.asc())
        .all()
    )
    return [(str(label), int(cnt)) for label, cnt in rows]


@router.post("/api/analytics/traits/normalize", response_model=TraitNormalizationResponse)
def normalize_traits(body: TraitNormalizationRequest, db: DbSession, user: CurrentUser):
    from personality_analytics import invalidate_personality_chart_cache, pinned_trait_labels_for_user

    counts = _trait_counts_for_user(db, user.user_id)
    count_by_label: dict[str, int] = {label: cnt for label, cnt in counts}
    target = {_normalize_trait_label(x) for x in body.target_labels if _normalize_trait_label(x)} if body.target_labels else None
    pinned = pinned_trait_labels_for_user(db, user.user_id, limit=50)
    pinned_by_key: dict[str, str] = {_trait_key(p): _normalize_trait_label(p) for p in pinned}

    merge_map: dict[str, str] = {}

    if body.strategy == "alias_map":
        for src, dst in body.alias_map.items():
            src_n = _normalize_trait_label(src)
            dst_n = _normalize_trait_label(dst)
            if not src_n or not dst_n or src_n == dst_n:
                continue
            if target is not None and src_n not in target:
                continue
            if src_n in count_by_label:
                merge_map[src_n] = dst_n

    elif body.strategy == "casefold":
        by_key: dict[str, list[str]] = defaultdict(list)
        for label in count_by_label.keys():
            if target is not None and label not in target:
                continue
            by_key[_trait_key(label)].append(label)
        for k, labels in by_key.items():
            if len(labels) < 2:
                continue
            canonical = pinned_by_key.get(k)
            if canonical is None:
                canonical = sorted(labels, key=lambda x: (-count_by_label.get(x, 0), x))[0]
            for label in labels:
                if label != canonical:
                    merge_map[label] = canonical

    else:  # pinned_priority
        for label in count_by_label.keys():
            if target is not None and label not in target:
                continue
            key = _trait_key(label)
            pin_canonical = pinned_by_key.get(key)
            if pin_canonical and label != pin_canonical:
                merge_map[label] = pin_canonical
        for src, dst in body.alias_map.items():
            src_n = _normalize_trait_label(src)
            dst_n = _normalize_trait_label(dst)
            if src_n and dst_n and src_n != dst_n and src_n in count_by_label:
                if target is None or src_n in target:
                    merge_map[src_n] = dst_n

    merges: list[TraitMergeItem] = []
    for src, dst in sorted(merge_map.items()):
        merges.append(TraitMergeItem(from_label=src, to_label=dst, rows=count_by_label.get(src, 0)))

    response = TraitNormalizationResponse(
        mode=body.mode,
        strategy=body.strategy,
        merges=merges,
        before_top=[TraitTotalItem(trait_label=l, total=c) for l, c in counts[:15]],
    )

    if body.mode == "preview" or not merge_map:
        return response

    rows = (
        db.query(models.PersonalityTrait)
        .join(models.Task, models.Task.task_id == models.PersonalityTrait.task_id)
        .filter(models.Task.user_id == user.user_id)
        .all()
    )
    updated = 0
    for row in rows:
        old = _normalize_trait_label(str(row.label or ""))
        new = merge_map.get(old)
        if new and old != new:
            row.label = new
            updated += 1

    pins = (
        db.query(models.PinnedPersonalityTrait)
        .filter(models.PinnedPersonalityTrait.user_id == user.user_id)
        .order_by(models.PinnedPersonalityTrait.created_at.asc(), models.PinnedPersonalityTrait.pin_id.asc())
        .all()
    )
    deleted_pins = 0
    seen_labels: set[str] = set()
    for pin in pins:
        old = _normalize_trait_label(str(pin.label or ""))
        new = merge_map.get(old, old)
        if new in seen_labels:
            db.delete(pin)
            deleted_pins += 1
            continue
        pin.label = new
        seen_labels.add(new)

    db.commit()
    invalidate_personality_chart_cache(db, user.user_id)
    db.commit()
    rollup_stats = refresh_user_goal_trait_rollups(db, user.user_id)

    after = _trait_counts_for_user(db, user.user_id)
    response.updated_rows = updated
    response.pinned_rows_deleted = deleted_pins
    response.after_top = [TraitTotalItem(trait_label=l, total=c) for l, c in after[:15]]
    response.rollup_stats = rollup_stats
    return response


@router.get("/api/analytics/growth-goals/activity/totals", response_model=ActivitySeriesResponse)
def growth_goal_totals(
    db: DbSession,
    user: CurrentUser,
    grain: Grain = Query("day"),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
):
    _ensure_rollups_if_missing(db, user.user_id)
    q = (
        db.query(models.GrowthGoalActivityRollup.period_start, models.GrowthGoalActivityRollup.total)
        .filter(
            models.GrowthGoalActivityRollup.user_id == user.user_id,
            models.GrowthGoalActivityRollup.grain == grain,
        )
        .all()
    )
    agg: dict[date, int] = defaultdict(int)
    for period_start, total in q:
        if from_date is not None and period_start < from_date:
            continue
        if to_date is not None and period_start > to_date:
            continue
        agg[period_start] += int(total)
    buckets = [RollupBucket(period_start=d, total=agg[d]) for d in sorted(agg.keys())]
    return ActivitySeriesResponse(
        grain=grain,
        from_date=from_date,
        to_date=to_date,
        total=sum(x.total for x in buckets),
        buckets=buckets,
    )


@router.get("/api/analytics/traits/activity/totals", response_model=ActivitySeriesResponse)
def trait_totals(
    db: DbSession,
    user: CurrentUser,
    grain: Grain = Query("day"),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
):
    _ensure_rollups_if_missing(db, user.user_id)
    q = (
        db.query(models.TraitActivityRollup.period_start, models.TraitActivityRollup.total)
        .filter(
            models.TraitActivityRollup.user_id == user.user_id,
            models.TraitActivityRollup.grain == grain,
        )
        .all()
    )
    agg: dict[date, int] = defaultdict(int)
    for period_start, total in q:
        if from_date is not None and period_start < from_date:
            continue
        if to_date is not None and period_start > to_date:
            continue
        agg[period_start] += int(total)
    buckets = [RollupBucket(period_start=d, total=agg[d]) for d in sorted(agg.keys())]
    return ActivitySeriesResponse(
        grain=grain,
        from_date=from_date,
        to_date=to_date,
        total=sum(x.total for x in buckets),
        buckets=buckets,
    )

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from auth_deps import CurrentUser
from database import DbSession, SessionLocal
from openai_client import OPENAI_API_KEY, openai_chat_completion

router = APIRouter(prefix="/api/finances", tags=["finances"])
logger = logging.getLogger(__name__)

_JOB_TASKS: Dict[int, asyncio.Task] = {}
_ALLOWED_CATEGORIES = {
    "food",
    "transport",
    "housing",
    "health",
    "entertainment",
    "income",
    "utilities",
    "shopping",
    "travel",
    "debt_payments",
    "transfers",
    "other",
    "uncategorized",
}


def _normalize_merchant(raw: str) -> str:
    lowered = (raw or "").strip().lower()
    lowered = re.sub(r"\s+", " ", lowered)
    return re.sub(r"[^a-z0-9 ]+", "", lowered).strip()[:200]


def _heuristic_category(label: str) -> str:
    s = (label or "").lower()
    if any(
        k in s
        for k in (
            "applecard",
            "capital one",
            "chase credit",
            "credit card",
            "sofi bank pl",
            "pennymac",
            "mortgage",
            "loan",
        )
    ):
        return "debt_payments"
    if any(
        k in s
        for k in (
            "zelle",
            "venmo",
            "cash app",
            "wire transfer",
            "to savings",
            "from savings",
            "apple cash sent money",
            "apple cash",
            "transfer",
        )
    ):
        return "transfers"
    if any(k in s for k in ("uber", "lyft", "gas", "shell", "chevron", "train", "metro", "flight", "airbnb")):
        return "transport"
    if any(k in s for k in ("coffee", "cafe", "restaurant", "dinner", "lunch", "doordash", "uber eats", "grocery")):
        return "food"
    if any(k in s for k in ("netflix", "spotify", "hulu", "disney", "apple music")):
        return "entertainment"
    if any(k in s for k in ("rent", "mortgage", "hoa", "landlord", "apartment")):
        return "housing"
    if any(k in s for k in ("doctor", "pharmacy", "hospital", "medical", "therapy")):
        return "health"
    if any(k in s for k in ("salary", "payroll", "deposit", "refund")):
        return "income"
    return "other"


def _normalize_category(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    cleaned = str(raw).strip().lower()[:80]
    if not cleaned:
        return None
    return cleaned if cleaned in _ALLOWED_CATEGORIES else "other"


async def _ai_categories_for_transactions(items: List[dict[str, Any]]) -> Dict[int, str]:
    if not items:
        return {}
    if not OPENAI_API_KEY:
        return {int(x["transaction_id"]): _heuristic_category(x.get("label", "")) for x in items}

    system_prompt = (
        "Categorize personal finance transactions. Return strict JSON only "
        'as {"items":[{"transaction_id":number,"category":"string"}]}. '
        "Use concise lowercase categories like food, transport, housing, health, entertainment, income, utilities, shopping, travel, other."
    )
    user_prompt = json.dumps({"transactions": items})
    try:
        payload, _ = await openai_chat_completion(system_prompt, user_prompt, temperature=0.1)
    except HTTPException:
        return {int(x["transaction_id"]): _heuristic_category(x.get("label", "")) for x in items}
    mapped: Dict[int, str] = {}
    for item in payload.get("items", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        try:
            tid = int(item.get("transaction_id"))
        except Exception:
            continue
        category = _normalize_category(str(item.get("category", "")))
        if category:
            mapped[tid] = category
    for it in items:
        tid = int(it["transaction_id"])
        if tid not in mapped:
            mapped[tid] = _heuristic_category(it.get("label", ""))
    return mapped


async def _ai_map_import_headers(headers: List[str]) -> Dict[str, str]:
    """
    Ask AI to map unknown import headers to canonical finance import keys.
    Returns mapping: {raw_header: canonical_key}.
    """
    if not headers or not OPENAI_API_KEY:
        return {}
    system_prompt = (
        "Map CSV transaction column names to canonical keys. Return strict JSON only as "
        '{"mapping":{"raw_header":"canonical_key"}}. '
        "Allowed canonical keys only: occurred_on, amount_minor, currency, merchant, description, source_external_id, account_label. "
        "Only map when confident; otherwise omit."
    )
    user_prompt = json.dumps({"headers": headers})
    try:
        payload, _ = await openai_chat_completion(system_prompt, user_prompt, temperature=0.0)
    except HTTPException:
        return {}
    mapping_raw = payload.get("mapping", {}) if isinstance(payload, dict) else {}
    if not isinstance(mapping_raw, dict):
        return {}
    out: Dict[str, str] = {}
    for raw_header, canonical in mapping_raw.items():
        raw = str(raw_header).strip()
        can = str(canonical).strip()
        if raw and can in _IMPORT_HEADER_SYNONYMS:
            out[raw] = can
    return out


class FinanceTransactionCreate(BaseModel):
    occurred_on: date
    amount_minor: int = Field(
        description="Signed amount in minor currency units, e.g. cents.",
        json_schema_extra={"example": 1599},
    )
    currency: str = Field(default="USD", min_length=3, max_length=12)
    merchant: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=300)
    category: Optional[str] = Field(default=None, max_length=80)
    source: str = Field(default="manual", max_length=32)
    source_external_id: Optional[str] = Field(default=None, max_length=128)
    account_label: Optional[str] = Field(default=None, max_length=120)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.strip().upper()[:12] or "USD"


class FinanceTransactionPatch(BaseModel):
    occurred_on: Optional[date] = None
    amount_minor: Optional[int] = None
    currency: Optional[str] = Field(default=None, min_length=3, max_length=12)
    merchant: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=300)
    category: Optional[str] = Field(default=None, max_length=80)
    is_hidden_from_charts: Optional[bool] = None
    account_label: Optional[str] = Field(default=None, max_length=120)


class FinanceTransactionImportItem(BaseModel):
    occurred_on: date
    amount_minor: int
    currency: str = Field(default="USD", min_length=3, max_length=12)
    merchant: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=300)
    source_external_id: Optional[str] = Field(default=None, max_length=128)
    account_label: Optional[str] = Field(default=None, max_length=120)


class FinanceTransactionImportBody(BaseModel):
    items: List[FinanceTransactionImportItem | dict[str, Any]] = Field(default_factory=list, max_length=4000)
    rows: List[dict[str, Any]] = Field(default_factory=list, max_length=4000)
    source: str = Field(default="import", max_length=32)
    fuzzy_days: int = Field(default=2, ge=0, le=90)

    @field_validator("rows", mode="before")
    @classmethod
    def _rows_must_be_list(cls, v: object) -> object:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("rows must be a list of objects")
        return v

    @model_validator(mode="after")
    def _require_items_or_rows(self) -> "FinanceTransactionImportBody":
        if not self.items and not self.rows:
            raise ValueError("Provide at least one import row via items or rows")
        return self


class FinanceTransactionPublic(BaseModel):
    transaction_id: int
    occurred_on: date
    amount_minor: int
    currency: str
    merchant: str
    description: str
    category: Optional[str] = None
    source: str
    source_external_id: Optional[str] = None
    account_label: Optional[str] = None
    is_hidden_from_charts: bool
    deleted_at: Optional[datetime] = None


class FinanceImportSummary(BaseModel):
    rows_total: int
    duplicates_skipped: int
    net_added: int
    categorization_job_id: Optional[int] = None
    categorization_status: Optional[str] = None


class CategorizationJobCreateBody(BaseModel):
    transaction_ids: List[int] = Field(default_factory=list, max_length=2000)
    uncategorized_only: bool = True


class CategorizationJobPublic(BaseModel):
    job_id: int
    status: str
    requested_count: int
    processed_count: int
    failed_count: int
    created_at: datetime
    completed_at: Optional[datetime] = None
    error_detail: Optional[str] = None


class PieSlice(BaseModel):
    category: str
    amount_minor: int


class PieResponse(BaseModel):
    from_date: date
    to_date: date
    mode: Literal["spend", "income"]
    total_minor: int
    slices: List[PieSlice]
    income_minor: Optional[int] = None
    spend_minor: Optional[int] = None
    unspent_income_minor: Optional[int] = None


class BudgetCreateBody(BaseModel):
    category: str = Field(min_length=1, max_length=80)
    period: Literal["monthly", "weekly", "custom"] = "monthly"
    period_start: date
    period_end: date
    amount_minor: int = Field(gt=0)


class BudgetPatchBody(BaseModel):
    category: Optional[str] = Field(default=None, min_length=1, max_length=80)
    period: Optional[Literal["monthly", "weekly", "custom"]] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    amount_minor: Optional[int] = Field(default=None, gt=0)


class BudgetPublic(BaseModel):
    budget_id: int
    category: str
    period: str
    period_start: date
    period_end: date
    amount_minor: int


class BudgetUtilizationItem(BaseModel):
    budget_id: int
    category: str
    period_start: date
    period_end: date
    budget_amount_minor: int
    spent_minor: int
    remaining_minor: int
    used_percent: float


def _tx_to_public(tx: models.FinanceTransaction) -> FinanceTransactionPublic:
    return FinanceTransactionPublic(
        transaction_id=tx.transaction_id,
        occurred_on=tx.occurred_on,
        amount_minor=tx.amount_minor,
        currency=tx.currency,
        merchant=tx.merchant_raw or "",
        description=tx.description or "",
        category=tx.category,
        source=tx.source,
        source_external_id=tx.source_external_id,
        account_label=tx.account_label,
        is_hidden_from_charts=bool(tx.is_hidden_from_charts),
        deleted_at=tx.deleted_at,
    )


def _same_fuzzy(existing: models.FinanceTransaction, incoming: FinanceTransactionImportItem, fuzzy_days: int) -> bool:
    if existing.amount_minor != incoming.amount_minor:
        return False
    if (existing.currency or "").upper() != (incoming.currency or "").upper():
        return False
    if _normalize_merchant(existing.merchant_raw or "") != _normalize_merchant(incoming.merchant):
        return False
    return abs((existing.occurred_on - incoming.occurred_on).days) <= fuzzy_days


def _is_duplicate_candidate(existing_date: date, incoming_date: date, fuzzy_days: int) -> bool:
    return abs((existing_date - incoming_date).days) <= fuzzy_days


_IMPORT_HEADER_SYNONYMS: Dict[str, List[str]] = {
    "occurred_on": ["occurred_on", "date", "transaction_date", "posted_date", "timestamp", "time", "day"],
    "amount_minor": ["amount_minor", "amount", "value", "cost", "price", "total", "debit", "credit"],
    "currency": ["currency", "ccy", "currency_code"],
    "merchant": ["merchant", "vendor", "payee", "name", "merchant_name"],
    "description": ["description", "memo", "note", "details", "transaction_description"],
    "source_external_id": ["source_external_id", "external_id", "transaction_id", "id", "reference", "ref"],
    "account_label": ["account_label", "account", "account_name", "account_id", "wallet"],
}


def _normalized_header_key(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")


def _best_fuzzy_key(payload: dict[str, Any], canonical_key: str) -> Optional[str]:
    if not payload:
        return None
    keys = list(payload.keys())
    normalized_to_actual: Dict[str, str] = {_normalized_header_key(k): k for k in keys}
    wanted = _IMPORT_HEADER_SYNONYMS.get(canonical_key, [canonical_key])
    # Exact normalized synonym match first.
    for w in wanted:
        wk = _normalized_header_key(w)
        if wk in normalized_to_actual:
            return normalized_to_actual[wk]
    # Then fuzzy closest match over normalized keys.
    candidates = list(normalized_to_actual.keys())
    for w in wanted:
        wk = _normalized_header_key(w)
        best = difflib.get_close_matches(wk, candidates, n=1, cutoff=0.82)
        if best:
            return normalized_to_actual[best[0]]
    return None


def _to_minor_amount(raw: Any) -> int:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(round(raw * 100))
    s = str(raw or "").strip()
    if not s:
        raise ValueError("amount is required")
    s = s.replace(",", "")
    if "." in s:
        return int(round(float(s) * 100))
    return int(s)


def _coerce_to_date(raw: Any) -> date:
    # Excel serial date support (e.g. 46106.833333333336)
    if isinstance(raw, (int, float)):
        serial = float(raw)
        base = datetime(1899, 12, 30)
        return (base + timedelta(days=serial)).date()
    s = str(raw or "").strip()
    if not s:
        raise ValueError("date is required")
    iso = s[:10]
    try:
        return date.fromisoformat(iso)
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError("Unsupported date format")


def _coerce_import_rows_to_items(rows: List[dict[str, Any]]) -> List[FinanceTransactionImportItem]:
    out: List[FinanceTransactionImportItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date_key = _best_fuzzy_key(row, "occurred_on")
        amount_key = _best_fuzzy_key(row, "amount_minor")
        if not date_key or not amount_key:
            raise HTTPException(status_code=422, detail="Import row missing recognizable date/amount columns")
        currency_key = _best_fuzzy_key(row, "currency")
        merchant_key = _best_fuzzy_key(row, "merchant")
        description_key = _best_fuzzy_key(row, "description")
        ext_key = _best_fuzzy_key(row, "source_external_id")
        account_key = _best_fuzzy_key(row, "account_label")

        item = FinanceTransactionImportItem(
            occurred_on=_coerce_to_date(row.get(date_key)),
            amount_minor=_to_minor_amount(row.get(amount_key)),
            currency=str(row.get(currency_key, "USD")).strip().upper()[:12] if currency_key else "USD",
            merchant=str(row.get(merchant_key, "")).strip()[:200] if merchant_key else "",
            description=str(row.get(description_key, "")).strip()[:300] if description_key else "",
            source_external_id=(str(row.get(ext_key, "")).strip()[:128] if ext_key else "") or None,
            account_label=(str(row.get(account_key, "")).strip()[:120] if account_key else "") or None,
        )
        out.append(item)
    return out


async def _coerce_import_rows_to_items_with_ai(rows: List[dict[str, Any]]) -> List[FinanceTransactionImportItem]:
    """
    Deterministic fuzzy mapping first; if required columns are unresolved, AI maps headers.
    """
    if not rows:
        return []

    # Gather all distinct headers to allow one AI call for the batch.
    all_headers: List[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row.keys():
            s = str(key)
            if s not in seen:
                seen.add(s)
                all_headers.append(s)
    ai_map = await _ai_map_import_headers(all_headers)

    def _best_key_with_ai(payload: dict[str, Any], canonical: str) -> Optional[str]:
        found = _best_fuzzy_key(payload, canonical)
        if found:
            return found
        # AI returned raw header -> canonical key; find first key in this row
        for raw_header, canonical_key in ai_map.items():
            if canonical_key != canonical:
                continue
            if raw_header in payload:
                return raw_header
        return None

    out: List[FinanceTransactionImportItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date_key = _best_key_with_ai(row, "occurred_on")
        amount_key = _best_key_with_ai(row, "amount_minor")
        if not date_key or not amount_key:
            raise HTTPException(status_code=422, detail="Import row missing recognizable date/amount columns")
        currency_key = _best_key_with_ai(row, "currency")
        merchant_key = _best_key_with_ai(row, "merchant")
        description_key = _best_key_with_ai(row, "description")
        ext_key = _best_key_with_ai(row, "source_external_id")
        account_key = _best_key_with_ai(row, "account_label")

        item = FinanceTransactionImportItem(
            occurred_on=_coerce_to_date(row.get(date_key)),
            amount_minor=_to_minor_amount(row.get(amount_key)),
            currency=str(row.get(currency_key, "USD")).strip().upper()[:12] if currency_key else "USD",
            merchant=str(row.get(merchant_key, "")).strip()[:200] if merchant_key else "",
            description=str(row.get(description_key, "")).strip()[:300] if description_key else "",
            source_external_id=(str(row.get(ext_key, "")).strip()[:128] if ext_key else "") or None,
            account_label=(str(row.get(account_key, "")).strip()[:120] if account_key else "") or None,
        )
        out.append(item)
    return out


@router.post("/transactions", response_model=FinanceTransactionPublic)
def create_transaction(body: FinanceTransactionCreate, db: DbSession, user: CurrentUser):
    tx = models.FinanceTransaction(
        user_id=user.user_id,
        occurred_on=body.occurred_on,
        amount_minor=body.amount_minor,
        currency=body.currency,
        merchant_raw=body.merchant.strip(),
        merchant_normalized=_normalize_merchant(body.merchant),
        description=body.description.strip(),
        category=_normalize_category(body.category),
        source=body.source.strip() or "manual",
        source_external_id=(body.source_external_id or "").strip() or None,
        account_label=(body.account_label or "").strip() or None,
        is_hidden_from_charts=False,
        deleted_at=None,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return _tx_to_public(tx)


@router.get("/transactions", response_model=List[FinanceTransactionPublic])
def list_transactions(
    db: DbSession,
    user: CurrentUser,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    include_deleted: bool = False,
):
    q = db.query(models.FinanceTransaction).filter(models.FinanceTransaction.user_id == user.user_id)
    if not include_deleted:
        q = q.filter(models.FinanceTransaction.deleted_at.is_(None))
    rows = q.order_by(models.FinanceTransaction.occurred_on.desc(), models.FinanceTransaction.transaction_id.desc()).offset(skip).limit(limit).all()
    return [_tx_to_public(x) for x in rows]


@router.patch("/transactions/{transaction_id}", response_model=FinanceTransactionPublic)
def patch_transaction(transaction_id: int, body: FinanceTransactionPatch, db: DbSession, user: CurrentUser):
    tx = (
        db.query(models.FinanceTransaction)
        .filter(models.FinanceTransaction.transaction_id == transaction_id, models.FinanceTransaction.user_id == user.user_id)
        .first()
    )
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    patch = body.model_dump(exclude_unset=True)
    if "occurred_on" in patch:
        tx.occurred_on = patch["occurred_on"]
    if "amount_minor" in patch:
        tx.amount_minor = int(patch["amount_minor"])
    if "currency" in patch and patch["currency"] is not None:
        tx.currency = str(patch["currency"]).strip().upper()[:12] or "USD"
    if "merchant" in patch and patch["merchant"] is not None:
        raw = str(patch["merchant"]).strip()[:200]
        tx.merchant_raw = raw
        tx.merchant_normalized = _normalize_merchant(raw)
    if "description" in patch and patch["description"] is not None:
        tx.description = str(patch["description"]).strip()[:300]
    if "category" in patch:
        tx.category = _normalize_category(patch["category"])
    if "is_hidden_from_charts" in patch:
        tx.is_hidden_from_charts = bool(patch["is_hidden_from_charts"])
    if "account_label" in patch:
        tx.account_label = (str(patch["account_label"]).strip()[:120] if patch["account_label"] else None)
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return _tx_to_public(tx)


@router.delete("/transactions/{transaction_id}", status_code=204)
def delete_transaction(transaction_id: int, db: DbSession, user: CurrentUser):
    tx = (
        db.query(models.FinanceTransaction)
        .filter(models.FinanceTransaction.transaction_id == transaction_id, models.FinanceTransaction.user_id == user.user_id)
        .first()
    )
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    tx.deleted_at = datetime.now(timezone.utc)
    db.add(tx)
    db.commit()
    return None


@router.post("/transactions/import", response_model=FinanceImportSummary)
async def import_transactions(body: FinanceTransactionImportBody, db: DbSession, user: CurrentUser):
    items: List[FinanceTransactionImportItem] = []
    raw_rows_from_items: List[dict[str, Any]] = []
    for it in body.items:
        if isinstance(it, FinanceTransactionImportItem):
            items.append(it)
            continue
        if isinstance(it, dict):
            # If canonical keys are present, validate directly; otherwise treat as raw row.
            if "occurred_on" in it and "amount_minor" in it:
                try:
                    items.append(FinanceTransactionImportItem.model_validate(it))
                    continue
                except Exception:
                    pass
            raw_rows_from_items.append(it)
            continue
        raise HTTPException(status_code=422, detail="Each import item must be an object")

    if raw_rows_from_items:
        items.extend(await _coerce_import_rows_to_items_with_ai(raw_rows_from_items))
    if body.rows:
        items.extend(await _coerce_import_rows_to_items_with_ai(body.rows))
    if not items:
        raise HTTPException(status_code=422, detail="Provide at least one import row via items or rows")

    t0 = time.perf_counter()
    incoming_min_date = min(x.occurred_on for x in items)
    incoming_max_date = max(x.occurred_on for x in items)
    min_date = incoming_min_date
    max_date = incoming_max_date
    if body.fuzzy_days:
        min_date = min_date.fromordinal(min_date.toordinal() - body.fuzzy_days)
        max_date = max_date.fromordinal(max_date.toordinal() + body.fuzzy_days)
    incoming_amounts = sorted({int(x.amount_minor) for x in items})
    incoming_currencies = sorted({(x.currency or "USD").strip().upper()[:12] or "USD" for x in items})

    existing = (
        db.query(models.FinanceTransaction)
        .filter(
            models.FinanceTransaction.user_id == user.user_id,
            models.FinanceTransaction.deleted_at.is_(None),
            models.FinanceTransaction.occurred_on >= min_date,
            models.FinanceTransaction.occurred_on <= max_date,
            models.FinanceTransaction.amount_minor.in_(incoming_amounts),
            models.FinanceTransaction.currency.in_(incoming_currencies),
        )
        .all()
    )
    existing_by_key: Dict[tuple[int, str, str], List[date]] = {}
    for tx in existing:
        key = (int(tx.amount_minor), (tx.currency or "").upper(), _normalize_merchant(tx.merchant_raw or ""))
        existing_by_key.setdefault(key, []).append(tx.occurred_on)

    duplicates_skipped = 0
    net_added = 0
    inserted_ids: List[int] = []
    for item in items:
        key = (
            int(item.amount_minor),
            (item.currency or "USD").strip().upper()[:12] or "USD",
            _normalize_merchant(item.merchant),
        )
        existing_dates = existing_by_key.get(key, [])
        if any(_is_duplicate_candidate(existing_date, item.occurred_on, body.fuzzy_days) for existing_date in existing_dates):
            duplicates_skipped += 1
            continue
        tx = models.FinanceTransaction(
            user_id=user.user_id,
            occurred_on=item.occurred_on,
            amount_minor=item.amount_minor,
            currency=item.currency.strip().upper()[:12] or "USD",
            merchant_raw=item.merchant.strip()[:200],
            merchant_normalized=_normalize_merchant(item.merchant),
            description=item.description.strip()[:300],
            category=None,
            source=body.source.strip() or "import",
            source_external_id=(item.source_external_id or "").strip() or None,
            account_label=(item.account_label or "").strip() or None,
            is_hidden_from_charts=False,
            deleted_at=None,
        )
        db.add(tx)
        db.flush()
        inserted_ids.append(int(tx.transaction_id))
        existing_by_key.setdefault(key, []).append(tx.occurred_on)
        net_added += 1
    db.commit()
    job_id: Optional[int] = None
    job_status: Optional[str] = None
    if inserted_ids:
        job = _enqueue_categorization_job(db, user_id=user.user_id, tx_ids=inserted_ids)
        job_id = int(job.job_id)
        job_status = job.status
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "finance_import user=%s rows_total=%s duplicates_skipped=%s net_added=%s job_id=%s elapsed_ms=%s",
        user.user_id,
        len(items),
        duplicates_skipped,
        net_added,
        job_id,
        elapsed_ms,
    )
    return FinanceImportSummary(
        rows_total=len(items),
        duplicates_skipped=duplicates_skipped,
        net_added=net_added,
        categorization_job_id=job_id,
        categorization_status=job_status,
    )


async def _run_categorization_job(job_id: int) -> None:
    db: Session = SessionLocal()
    try:
        job = db.query(models.FinanceCategorizationJob).filter(models.FinanceCategorizationJob.job_id == job_id).first()
        if job is None:
            return
        t0 = time.perf_counter()
        job.status = "running"
        db.add(job)
        db.commit()

        tx_ids = [int(x) for x in (job.transaction_ids or [])]
        if not tx_ids:
            job.status = "completed"
            job.completed_at = datetime.now(timezone.utc)
            db.add(job)
            db.commit()
            return

        rows = (
            db.query(models.FinanceTransaction)
            .filter(
                models.FinanceTransaction.user_id == job.user_id,
                models.FinanceTransaction.transaction_id.in_(tx_ids),
                models.FinanceTransaction.deleted_at.is_(None),
            )
            .all()
        )
        items = [
            {
                "transaction_id": tx.transaction_id,
                "label": f"{tx.merchant_raw} {tx.description}".strip(),
                "amount_minor": tx.amount_minor,
            }
            for tx in rows
        ]
        categorized = await _ai_categories_for_transactions(items)
        processed = 0
        failed = 0
        for tx in rows:
            try:
                tx.category = categorized.get(tx.transaction_id) or _heuristic_category(f"{tx.merchant_raw} {tx.description}")
                db.add(tx)
                processed += 1
            except Exception:
                failed += 1
        job.processed_count = processed
        job.failed_count = failed
        job.status = "completed" if failed == 0 else ("partial" if processed > 0 else "failed")
        job.completed_at = datetime.now(timezone.utc)
        db.add(job)
        db.commit()
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "finance_categorization_job_done job_id=%s user=%s status=%s requested=%s processed=%s failed=%s elapsed_ms=%s",
            job_id,
            job.user_id,
            job.status,
            job.requested_count,
            job.processed_count,
            job.failed_count,
            elapsed_ms,
        )
    except Exception as exc:
        job = db.query(models.FinanceCategorizationJob).filter(models.FinanceCategorizationJob.job_id == job_id).first()
        if job is not None:
            job.status = "failed"
            job.error_detail = str(exc)[:300]
            job.completed_at = datetime.now(timezone.utc)
            db.add(job)
            db.commit()
            logger.exception("finance_categorization_job_failed job_id=%s", job_id)
    finally:
        _JOB_TASKS.pop(job_id, None)
        db.close()


def _enqueue_categorization_job(db: Session, *, user_id: Any, tx_ids: List[int]) -> models.FinanceCategorizationJob:
    job = models.FinanceCategorizationJob(
        user_id=user_id,
        status="queued",
        requested_count=len(tx_ids),
        processed_count=0,
        failed_count=0,
        transaction_ids=[int(x) for x in tx_ids],
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    _JOB_TASKS[job.job_id] = asyncio.create_task(_run_categorization_job(job.job_id))
    return job


def start_finance_job_workers() -> None:
    """
    Recover queued/running jobs on process boot.
    This keeps job state durable in DB and prevents jobs from being orphaned after restarts.
    """
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(models.FinanceCategorizationJob)
            .filter(models.FinanceCategorizationJob.status.in_(["queued", "running"]))
            .order_by(models.FinanceCategorizationJob.created_at.asc(), models.FinanceCategorizationJob.job_id.asc())
            .all()
        )
        for row in rows:
            if row.job_id in _JOB_TASKS:
                continue
            _JOB_TASKS[row.job_id] = asyncio.create_task(_run_categorization_job(row.job_id))
        if rows:
            logger.info("finance_job_recovery recovered_jobs=%s", len(rows))
    finally:
        db.close()


@router.post("/categorization-jobs", response_model=CategorizationJobPublic)
async def create_categorization_job(body: CategorizationJobCreateBody, db: DbSession, user: CurrentUser):
    q = db.query(models.FinanceTransaction).filter(
        models.FinanceTransaction.user_id == user.user_id,
        models.FinanceTransaction.deleted_at.is_(None),
    )
    if body.transaction_ids:
        q = q.filter(models.FinanceTransaction.transaction_id.in_(body.transaction_ids))
    if body.uncategorized_only:
        q = q.filter((models.FinanceTransaction.category.is_(None)) | (models.FinanceTransaction.category == ""))
    rows = q.order_by(models.FinanceTransaction.transaction_id.asc()).all()
    tx_ids = [int(r.transaction_id) for r in rows]

    job = _enqueue_categorization_job(db, user_id=user.user_id, tx_ids=tx_ids)
    return CategorizationJobPublic(
        job_id=job.job_id,
        status=job.status,
        requested_count=job.requested_count,
        processed_count=job.processed_count,
        failed_count=job.failed_count,
        created_at=job.created_at,
        completed_at=job.completed_at,
        error_detail=job.error_detail,
    )


@router.get("/categorization-jobs/{job_id}", response_model=CategorizationJobPublic)
def get_categorization_job(job_id: int, db: DbSession, user: CurrentUser):
    job = (
        db.query(models.FinanceCategorizationJob)
        .filter(models.FinanceCategorizationJob.job_id == job_id, models.FinanceCategorizationJob.user_id == user.user_id)
        .first()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Categorization job not found")
    return CategorizationJobPublic(
        job_id=job.job_id,
        status=job.status,
        requested_count=job.requested_count,
        processed_count=job.processed_count,
        failed_count=job.failed_count,
        created_at=job.created_at,
        completed_at=job.completed_at,
        error_detail=job.error_detail,
    )


@router.get("/analytics/pie", response_model=PieResponse)
def spend_pie(
    db: DbSession,
    user: CurrentUser,
    from_date: date = Query(...),
    to_date: date = Query(...),
    mode: Literal["spend", "income"] = Query(default="spend"),
    include_unspent_income: bool = Query(default=False),
):
    if to_date < from_date:
        raise HTTPException(status_code=400, detail="to_date must be on or after from_date")
    amount_expr = (
        -models.FinanceTransaction.amount_minor if mode == "spend" else models.FinanceTransaction.amount_minor
    )
    sign_filter = models.FinanceTransaction.amount_minor < 0 if mode == "spend" else models.FinanceTransaction.amount_minor > 0
    rows = (
        db.query(
            func.coalesce(models.FinanceTransaction.category, "uncategorized"),
            func.coalesce(func.sum(amount_expr), 0),
        )
        .filter(
            models.FinanceTransaction.user_id == user.user_id,
            models.FinanceTransaction.deleted_at.is_(None),
            models.FinanceTransaction.is_hidden_from_charts.is_(False),
            models.FinanceTransaction.occurred_on >= from_date,
            models.FinanceTransaction.occurred_on <= to_date,
            sign_filter,
        )
        .group_by(models.FinanceTransaction.category)
        .all()
    )
    slices = [PieSlice(category=str(category or "uncategorized"), amount_minor=int(total or 0)) for category, total in rows]
    income_minor: Optional[int] = None
    spend_minor: Optional[int] = None
    unspent_income_minor: Optional[int] = None
    if mode == "spend" and include_unspent_income:
        income_minor = int(
            db.query(func.coalesce(func.sum(models.FinanceTransaction.amount_minor), 0))
            .filter(
                models.FinanceTransaction.user_id == user.user_id,
                models.FinanceTransaction.deleted_at.is_(None),
                models.FinanceTransaction.is_hidden_from_charts.is_(False),
                models.FinanceTransaction.occurred_on >= from_date,
                models.FinanceTransaction.occurred_on <= to_date,
                models.FinanceTransaction.amount_minor > 0,
            )
            .scalar()
            or 0
        )
        spend_minor = int(
            db.query(func.coalesce(func.sum(-models.FinanceTransaction.amount_minor), 0))
            .filter(
                models.FinanceTransaction.user_id == user.user_id,
                models.FinanceTransaction.deleted_at.is_(None),
                models.FinanceTransaction.is_hidden_from_charts.is_(False),
                models.FinanceTransaction.occurred_on >= from_date,
                models.FinanceTransaction.occurred_on <= to_date,
                models.FinanceTransaction.amount_minor < 0,
            )
            .scalar()
            or 0
        )
        unspent_income_minor = max(0, income_minor - spend_minor)
        if unspent_income_minor > 0:
            slices.append(PieSlice(category="unspent_income", amount_minor=unspent_income_minor))
    total_minor = sum(x.amount_minor for x in slices)
    return PieResponse(
        from_date=from_date,
        to_date=to_date,
        mode=mode,
        total_minor=total_minor,
        slices=slices,
        income_minor=income_minor,
        spend_minor=spend_minor,
        unspent_income_minor=unspent_income_minor,
    )


@router.post("/budgets", response_model=BudgetPublic)
def create_budget(body: BudgetCreateBody, db: DbSession, user: CurrentUser):
    if body.period_end < body.period_start:
        raise HTTPException(status_code=400, detail="period_end must be on or after period_start")
    row = models.FinanceBudget(
        user_id=user.user_id,
        category=_normalize_category(body.category) or "other",
        period=body.period,
        period_start=body.period_start,
        period_end=body.period_end,
        amount_minor=body.amount_minor,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return BudgetPublic(
        budget_id=row.budget_id,
        category=row.category,
        period=row.period,
        period_start=row.period_start,
        period_end=row.period_end,
        amount_minor=row.amount_minor,
    )


@router.get("/budgets", response_model=List[BudgetPublic])
def list_budgets(db: DbSession, user: CurrentUser):
    rows = (
        db.query(models.FinanceBudget)
        .filter(models.FinanceBudget.user_id == user.user_id)
        .order_by(models.FinanceBudget.period_start.desc(), models.FinanceBudget.budget_id.desc())
        .all()
    )
    return [
        BudgetPublic(
            budget_id=x.budget_id,
            category=x.category,
            period=x.period,
            period_start=x.period_start,
            period_end=x.period_end,
            amount_minor=x.amount_minor,
        )
        for x in rows
    ]


@router.patch("/budgets/{budget_id}", response_model=BudgetPublic)
def patch_budget(budget_id: int, body: BudgetPatchBody, db: DbSession, user: CurrentUser):
    row = (
        db.query(models.FinanceBudget)
        .filter(models.FinanceBudget.user_id == user.user_id, models.FinanceBudget.budget_id == budget_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Budget not found")
    patch = body.model_dump(exclude_unset=True)
    if "category" in patch and patch["category"] is not None:
        row.category = _normalize_category(patch["category"]) or "other"
    if "period" in patch and patch["period"] is not None:
        row.period = str(patch["period"])
    if "period_start" in patch and patch["period_start"] is not None:
        row.period_start = patch["period_start"]
    if "period_end" in patch and patch["period_end"] is not None:
        row.period_end = patch["period_end"]
    if "amount_minor" in patch and patch["amount_minor"] is not None:
        row.amount_minor = int(patch["amount_minor"])
    if row.period_end < row.period_start:
        raise HTTPException(status_code=400, detail="period_end must be on or after period_start")
    db.add(row)
    db.commit()
    db.refresh(row)
    return BudgetPublic(
        budget_id=row.budget_id,
        category=row.category,
        period=row.period,
        period_start=row.period_start,
        period_end=row.period_end,
        amount_minor=row.amount_minor,
    )


@router.delete("/budgets/{budget_id}", status_code=204)
def delete_budget(budget_id: int, db: DbSession, user: CurrentUser):
    row = (
        db.query(models.FinanceBudget)
        .filter(models.FinanceBudget.user_id == user.user_id, models.FinanceBudget.budget_id == budget_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Budget not found")
    db.delete(row)
    db.commit()
    return None


@router.get("/analytics/budgets-utilization", response_model=List[BudgetUtilizationItem])
def budgets_utilization(
    db: DbSession,
    user: CurrentUser,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
):
    q = db.query(models.FinanceBudget).filter(models.FinanceBudget.user_id == user.user_id)
    if from_date is not None:
        q = q.filter(models.FinanceBudget.period_end >= from_date)
    if to_date is not None:
        q = q.filter(models.FinanceBudget.period_start <= to_date)
    rows = q.order_by(models.FinanceBudget.period_start.desc(), models.FinanceBudget.budget_id.desc()).all()
    out: List[BudgetUtilizationItem] = []
    for b in rows:
        spent = (
            db.query(func.coalesce(func.sum(models.FinanceTransaction.amount_minor), 0))
            .filter(
                models.FinanceTransaction.user_id == user.user_id,
                models.FinanceTransaction.deleted_at.is_(None),
                models.FinanceTransaction.is_hidden_from_charts.is_(False),
                models.FinanceTransaction.category == b.category,
                models.FinanceTransaction.amount_minor > 0,
                models.FinanceTransaction.occurred_on >= b.period_start,
                models.FinanceTransaction.occurred_on <= b.period_end,
            )
            .scalar()
        )
        spent_minor = int(spent or 0)
        remaining = b.amount_minor - spent_minor
        used_percent = round((spent_minor / b.amount_minor) * 100.0, 2) if b.amount_minor > 0 else 0.0
        out.append(
            BudgetUtilizationItem(
                budget_id=b.budget_id,
                category=b.category,
                period_start=b.period_start,
                period_end=b.period_end,
                budget_amount_minor=b.amount_minor,
                spent_minor=spent_minor,
                remaining_minor=remaining,
                used_percent=used_percent,
            )
        )
    return out

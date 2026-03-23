"""Persistence helpers for journals (user log) and embedded task rows."""

from __future__ import annotations

from typing import Any, Iterable
from uuid import UUID

from sqlalchemy.orm import Session

import models


def replace_personality_traits_for_task(db: Session, task_id: int, labels: list[str], *, max_traits: int = 10) -> None:
    """Replace all personality_traits rows for a task with up to `max_traits` non-empty labels."""
    db.query(models.PersonalityTrait).filter(models.PersonalityTrait.task_id == task_id).delete()
    n = 0
    for lab in labels:
        if n >= max_traits:
            break
        s = str(lab).strip()[:80]
        if not s:
            continue
        db.add(models.PersonalityTrait(task_id=task_id, label=s))
        n += 1


def insert_journal_with_tasks(
    db: Session,
    *,
    user_id: UUID,
    task_field_dicts: Iterable[dict[str, Any]],
    source: str,
    note: str | None,
) -> models.Journal:
    journal = models.Journal(user_id=user_id, source=source, note=note)
    db.add(journal)
    db.flush()
    for raw in task_field_dicts:
        data = dict(raw)
        traits = data.pop("personality_traits", None) or []
        if not isinstance(traits, list):
            traits = []
        row = models.Task(user_id=user_id, journal_id=journal.journal_id, **data)
        db.add(row)
        db.flush()
        replace_personality_traits_for_task(db, row.task_id, traits)
    return journal


def delete_journal_attachments_from_storage(db: Session, journal: models.Journal) -> None:
    """Remove S3 objects for completed uploads; no-op if attachments not configured."""
    import journal_storage

    if not journal_storage.attachments_configured():
        return
    for att in journal.attachments:
        if att.upload_completed_at is not None and att.storage_key:
            try:
                journal_storage.delete_object(att.storage_key)
            except Exception:
                pass

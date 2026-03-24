#!/usr/bin/env python3
from __future__ import annotations

import argparse
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Optional

import models
from database import SessionLocal
from journal_service import replace_personality_traits_for_task


@dataclass
class TaskTemplate:
    category: str
    label: str
    context: str
    sentiment: str
    time_of_day: str
    amount_of_time: str
    day_of_week: str
    traits: list[str]


def _month_bounds_utc(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)
    return start, end


def _build_templates(db, user_id) -> list[TaskTemplate]:
    rows = (
        db.query(models.Task)
        .filter(models.Task.user_id == user_id)
        .order_by(models.Task.task_id.desc())
        .limit(300)
        .all()
    )
    traits_by_task: dict[int, list[str]] = defaultdict(list)
    if rows:
        task_ids = [t.task_id for t in rows]
        links = (
            db.query(models.PersonalityTrait.task_id, models.PersonalityTrait.label)
            .filter(models.PersonalityTrait.task_id.in_(task_ids))
            .all()
        )
        for task_id, label in links:
            s = str(label or "").strip()
            if s:
                traits_by_task[int(task_id)].append(s[:80])

    out: list[TaskTemplate] = []
    for t in rows:
        out.append(
            TaskTemplate(
                category=str(t.category or "other")[:80] or "other",
                label=str(t.label or "task")[:200] or "task",
                context=str(t.context or "")[:300],
                sentiment=str(t.sentiment or "neutral")[:20] or "neutral",
                time_of_day=str(t.time_of_day or "unspecified")[:40] or "unspecified",
                amount_of_time=str(t.amount_of_time or "unspecified")[:40] or "unspecified",
                day_of_week=str(t.day_of_week or "unspecified")[:40] or "unspecified",
                traits=traits_by_task.get(t.task_id, [])[:5],
            )
        )
    return out


def _pick_weighted_labels(templates: list[TaskTemplate], k: int) -> list[TaskTemplate]:
    if not templates:
        return []
    counts = Counter((t.category, t.label) for t in templates)
    weighted: list[tuple[TaskTemplate, int]] = []
    first_by_key: dict[tuple[str, str], TaskTemplate] = {}
    for t in templates:
        key = (t.category, t.label)
        if key not in first_by_key:
            first_by_key[key] = t
    for key, cnt in counts.items():
        weighted.append((first_by_key[key], cnt))
    population = [tpl for tpl, _ in weighted]
    weights = [w for _, w in weighted]
    out: list[TaskTemplate] = []
    for _ in range(max(1, k)):
        out.append(random.choices(population, weights=weights, k=1)[0])
    return out


def _journals_in_month(db, user_id, start: datetime, end: datetime) -> int:
    return (
        db.query(models.Journal)
        .filter(
            models.Journal.user_id == user_id,
            models.Journal.submitted_at >= start,
            models.Journal.submitted_at < end,
        )
        .count()
    )


def _random_march_timestamp(day: int, year: int, month: int) -> datetime:
    hour = random.choice([7, 9, 12, 15, 18, 20, 22])
    minute = random.choice([0, 5, 10, 15, 20, 25, 30, 40, 45, 50, 55])
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def seed_month(
    *,
    username: str,
    year: int,
    month: int,
    journals_to_add: int,
    max_tasks_per_journal: int,
    apply: bool,
    seed: int,
) -> int:
    random.seed(seed)
    db = SessionLocal()
    try:
        user = db.query(models.Person).filter(models.Person.user_name == username).first()
        if user is None:
            print(f"user_not_found username={username}")
            return 1

        start, end = _month_bounds_utc(year, month)
        existing = _journals_in_month(db, user.user_id, start, end)
        templates = _build_templates(db, user.user_id)
        if not templates:
            print("no_templates_found_for_user")
            return 1

        print(
            f"user={username} user_id={user.user_id} month={year}-{month:02d} "
            f"existing_journals={existing} templates={len(templates)}"
        )

        day_pool = list(range(1, 32))
        random.shuffle(day_pool)
        chosen_days = day_pool[:journals_to_add]

        create_plan: list[tuple[datetime, list[TaskTemplate]]] = []
        for day in chosen_days:
            ts = _random_march_timestamp(day, year, month)
            n_tasks = random.randint(1, max(1, max_tasks_per_journal))
            picks = _pick_weighted_labels(templates, n_tasks)
            create_plan.append((ts, picks))

        print(f"planned_new_journals={len(create_plan)}")
        print(f"planned_new_tasks={sum(len(tasks) for _, tasks in create_plan)}")
        if not apply:
            print("dry_run=true (pass --apply to write)")
            return 0

        created_journals = 0
        created_tasks = 0
        for submitted_at, tasks in create_plan:
            j = models.Journal(
                user_id=user.user_id,
                submitted_at=submitted_at,
                source="mock_seed",
                note="Seeded mock journal for March trend testing.",
            )
            db.add(j)
            db.flush()
            created_journals += 1

            for tpl in tasks:
                t = models.Task(
                    user_id=user.user_id,
                    journal_id=j.journal_id,
                    category=tpl.category,
                    label=tpl.label,
                    context=tpl.context,
                    sentiment=tpl.sentiment,
                    time_of_day=tpl.time_of_day,
                    amount_of_time=tpl.amount_of_time,
                    day_of_week=tpl.day_of_week,
                )
                db.add(t)
                db.flush()
                replace_personality_traits_for_task(db, t.task_id, tpl.traits)
                created_tasks += 1

        db.commit()
        print(f"created_journals={created_journals}")
        print(f"created_tasks={created_tasks}")
        return 0
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed realistic March mock data from an existing user's history.")
    parser.add_argument("--username", default="princessbuna3")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--month", type=int, default=3)
    parser.add_argument("--journals", type=int, default=22, help="How many new journals to add in month.")
    parser.add_argument("--max-tasks-per-journal", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--apply", action="store_true", help="Actually write data (default is dry-run).")
    args = parser.parse_args()

    if args.month != 3:
        print("warning: script is intended for March seeding, but continuing.")

    return seed_month(
        username=args.username,
        year=args.year,
        month=args.month,
        journals_to_add=max(1, args.journals),
        max_tasks_per_journal=max(1, args.max_tasks_per_journal),
        apply=bool(args.apply),
        seed=args.seed,
    )


if __name__ == "__main__":
    raise SystemExit(main())

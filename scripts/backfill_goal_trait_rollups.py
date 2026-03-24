#!/usr/bin/env python3
from __future__ import annotations

import argparse
from uuid import UUID

import models
from database import SessionLocal
from growth_analytics import refresh_user_goal_trait_rollups


def _run(user_id: UUID | None) -> int:
    db = SessionLocal()
    try:
        users = (
            db.query(models.Person)
            .filter(models.Person.user_id == user_id)
            .all()
            if user_id is not None
            else db.query(models.Person).all()
        )
        total_users = 0
        for user in users:
            stats = refresh_user_goal_trait_rollups(db, user.user_id)
            total_users += 1
            print(f"user={user.user_id} stats={stats}")
        print(f"done users_processed={total_users}")
        return 0
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill growth-goal and trait activity rollups from tasks/journals."
    )
    parser.add_argument(
        "--user-id",
        type=str,
        default=None,
        help="Optional UUID to backfill one user only.",
    )
    args = parser.parse_args()

    uid: UUID | None = None
    if args.user_id:
        uid = UUID(args.user_id)
    return _run(uid)


if __name__ == "__main__":
    raise SystemExit(main())

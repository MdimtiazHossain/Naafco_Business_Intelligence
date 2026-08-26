"""Sweep stored conversations for questions the agent handled badly.

Usage::

    python scripts/mine_agent_signals.py --dry-run
    python scripts/mine_agent_signals.py
    python scripts/mine_agent_signals.py --limit 500

From here on the agent mines each turn as it happens, so this exists for the
conversations that were already stored when that hook did not. Run it once after
deploying the learning tables; after that the live hook keeps the queue current.

**Not idempotent.** Running it twice counts every historical turn twice.
``occurrences`` ranks a review queue rather than reporting a business figure, so
a second run distorts an ordering rather than corrupting a record — but there is
no reason to do it, and ``--dry-run`` shows what would be written first.
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401  (side effect: sys.path)

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import mining
from app.database.connection import get_engine
from app.database.models_learning import AgentLearningSignal

SEPARATOR = "=" * 78


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="Only sweep this many assistant turns.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be recorded, then roll back.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    with Session(get_engine(), future=True) as session:
        before = session.execute(
            select(func.count()).select_from(AgentLearningSignal)
        ).scalar_one()

        counts = mining.mine_history(session, limit=args.limit)

        after = session.execute(
            select(func.count()).select_from(AgentLearningSignal)
        ).scalar_one()

        print(SEPARATOR)
        print("Agent learning signals mined from stored conversations")
        print(SEPARATOR)
        if not counts:
            print("  Nothing to record: no stored turn failed in a way worth "
                  "reviewing.")
        for signal_type, count in sorted(counts.items()):
            print(f"  {signal_type:20} {count:6d} occurrence(s) recorded")
        print(f"  {'distinct phrases':20} {before:6d} -> {after}")

        if args.dry_run:
            session.rollback()
            print("\n  --dry-run: rolled back, nothing was written.")
        else:
            session.commit()
            print("\n  Committed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

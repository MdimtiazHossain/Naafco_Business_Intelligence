"""Ask the agent a question from the command line.

Usage::

    python scripts/ask_agent.py "আজকের sales কত?" --user ceo
    python scripts/ask_agent.py "Region-wise sales দেখাও" --user dhaka_rm --json
    python scripts/ask_agent.py --interactive --user ceo

Useful for checking the agent without running the API, and for confirming what a
particular user is allowed to see. It goes through exactly the same pipeline as
``POST /api/chat``: same permission filtering, same tools, same validation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

import _bootstrap  # noqa: F401  (side effect: sys.path)

from sqlalchemy.orm import Session

from app.ai.agent import BusinessIntelligenceAgent
from app.ai.llm import build_llm_client
from app.ai.permission_filter import load_user_context
from app.database.connection import get_engine

SEPARATOR = "=" * 78


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask the BI agent a question")
    parser.add_argument("question", nargs="?", help="The question to ask.")
    parser.add_argument("--user", required=True, help="Username to ask as.")
    parser.add_argument("--conversation", default=None, help="Continue a conversation.")
    parser.add_argument("--today", type=dt.date.fromisoformat, default=None,
                        help="Override today's date (for reproducible demos).")
    parser.add_argument("--json", action="store_true", help="Print the raw response.")
    parser.add_argument("--interactive", action="store_true",
                        help="Keep asking in one conversation until you type 'exit'.")
    parser.add_argument("--database-url", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.question and not args.interactive:
        print("ERROR: give a question, or use --interactive.", file=sys.stderr)
        return 2

    engine = get_engine(args.database_url)
    llm = build_llm_client()

    try:
        with Session(engine, expire_on_commit=False) as session:
            user = load_user_context(session, args.user)
            if user is None:
                print(f"ERROR: unknown or inactive user {args.user!r}.", file=sys.stderr)
                print("List users with: python scripts/manage_users.py list",
                      file=sys.stderr)
                return 2

            agent = BusinessIntelligenceAgent(session, user, llm=llm, today=args.today)

            if not args.json:
                print(SEPARATOR)
                print(f"Asking as {user.username} ({user.role}) — access: "
                      f"{user.describe_scope()}")
                engine_name = ("OpenAI" if llm.available
                               else "deterministic planner (no OPENAI_API_KEY)")
                print(f"Model: {engine_name}")
                print(SEPARATOR)
                print()

            conversation_id = args.conversation
            questions = [args.question] if args.question else []

            while True:
                if not questions:
                    if not args.interactive:
                        break
                    try:
                        typed = input("> ").strip()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        break
                    if typed.lower() in {"exit", "quit", "q", ""}:
                        break
                    questions.append(typed)

                question = questions.pop(0)
                response = agent.chat(question, conversation_id)
                session.commit()
                conversation_id = response.conversation_id

                if args.json:
                    print(json.dumps(response.model_dump(mode="json"), indent=2,
                                     ensure_ascii=False))
                else:
                    print(response.answer)
                    print()
                    print(f"— intent {response.intent.value} | tools "
                          f"{', '.join(response.tools_used) or '-'} | "
                          f"{response.elapsed_ms} ms")
                    print()

                if not args.interactive:
                    break
    except Exception as exc:  # noqa: BLE001 - reported, never raised at the user
        print(f"ERROR: {exc}", file=sys.stderr)
        print("Check DATABASE_URL and run 'cd backend && alembic upgrade head'.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

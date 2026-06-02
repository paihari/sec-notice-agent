"""CLI entrypoint: discover and backfill a company's filings.

Usage:
    python -m sec_notice.discover <CIK> [options]

Options:
    --forms 8-K,10-K     only these form types (default: all)
    --limit N            download documents for at most N new filings (default: 10)
    --metadata-only      record filing metadata only; download no documents
    --all                download documents for every new filing (no limit)

Examples:
    python -m sec_notice.discover 1050446 --forms 8-K --limit 5
    python -m sec_notice.discover 1050446 --metadata-only
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import func, select

from .pipeline import backfill_company
from .store.db import session_scope
from .store.models import Document, Filing


def _print_db_summary(cik: int) -> None:
    with session_scope() as session:
        total = session.scalar(
            select(func.count()).select_from(Filing).where(Filing.cik == cik)
        )
        by_status = session.execute(
            select(Filing.status, func.count())
            .where(Filing.cik == cik)
            .group_by(Filing.status)
        ).all()
        docs = session.scalar(
            select(func.count())
            .select_from(Document)
            .join(Filing)
            .where(Filing.cik == cik)
        )
        top_forms = session.execute(
            select(Filing.form_type, func.count())
            .where(Filing.cik == cik)
            .group_by(Filing.form_type)
            .order_by(func.count().desc())
            .limit(8)
        ).all()

    print(f"\n  DB now holds {total} filings for CIK {cik}  ({docs} documents)")
    print("  by status :", ", ".join(f"{s}={n}" for s, n in by_status))
    print("  top forms :", ", ".join(f"{f or '?'}={n}" for f, n in top_forms))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m sec_notice.discover")
    parser.add_argument("cik", type=int, help="company CIK, e.g. 1050446")
    parser.add_argument("--forms", default=None, help="comma-separated form types")
    parser.add_argument("--limit", type=int, default=10, help="max filings to fetch docs for")
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--all", action="store_true", help="fetch docs for all new filings")
    args = parser.parse_args(argv[1:])

    forms = (
        {f.strip() for f in args.forms.split(",") if f.strip()} if args.forms else None
    )
    limit = None if args.all else args.limit

    print(f"Discovering filings for CIK {args.cik}"
          + (f" (forms: {', '.join(sorted(forms))})" if forms else " (all forms)"))
    if args.metadata_only:
        print("Mode: metadata-only (no document downloads)")
    else:
        print(f"Mode: fetch docs for up to {limit if limit is not None else 'ALL'} new filings")

    stats = backfill_company(
        args.cik, forms=forms, limit=limit, metadata_only=args.metadata_only
    )

    print(f"\n  Company    : {stats.company or '-'}")
    print(f"  Discovered : {stats.discovered} filings")
    print(f"  New rows   : {stats.new_filings}")
    print(f"  Docs fetch : {stats.docs_fetched} filings downloaded")
    if not args.metadata_only and stats.new_filings > stats.docs_fetched:
        remaining = stats.new_filings - stats.docs_fetched
        print(f"  Note       : {remaining} new filings still metadata-only "
              f"(raise --limit or use --all to fetch their documents)")

    _print_db_summary(args.cik)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

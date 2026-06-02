"""CLI entrypoint: ingest one filing.

Usage:
    python -m sec_notice.ingest <edgar-filing-url>
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from .pipeline import ingest_filing
from .store.db import session_scope
from .store.models import Document, Filing


def _print_summary(filing_id: int) -> None:
    with session_scope() as session:
        filing = session.get(Filing, filing_id)
        docs = session.scalars(
            select(Document).where(Document.filing_id == filing_id)
        ).all()
        print(f"\n  Filing #{filing.id}  {filing.accession_no}")
        print(f"  Company CIK : {filing.cik}")
        print(f"  Form        : {filing.form_type or '-'}")
        print(f"  Filed       : {filing.filing_date or '-'}")
        print(f"  Status      : {filing.status}")
        print(f"  Documents   : {len(docs)}")
        for d in docs:
            chars = len(d.extracted_text) if d.extracted_text else 0
            text_note = f"{chars:,} chars text" if chars else "no text"
            print(f"    - {d.filename:40s} {d.doc_type or '':12s} {text_note}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    url = argv[1]
    print(f"Ingesting: {url}")
    filing_id = ingest_filing(url)
    _print_summary(filing_id)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

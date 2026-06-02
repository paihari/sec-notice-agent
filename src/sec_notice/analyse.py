"""CLI entrypoint: run the AI materiality analyst.

Usage:
    python -m sec_notice.analyse <CIK> [--forms 8-K,10-K] [--limit N]
    python -m sec_notice.analyse --filing-id <ID>

Only filings in status 'fetched' (documents downloaded) are analysed, and by
default only material forms (configurable via MATERIAL_FORMS). Each run advances
analysed filings to status 'analysed' and writes an `analyses` row.
"""

from __future__ import annotations

import argparse
import sys

from .config import config
from .pipeline import analyse_filing, analyse_pending
from .store.db import init_db, session_scope
from .store.models import Analysis, Company, Filing

_SEV_ICON = {"info": "·", "notable": "•", "high": "▲", "critical": "■"}


def _filing_header(filing_id: int) -> str:
    """One-line filing identity: form, date, company/ticker, accession."""
    with session_scope() as s:
        f = s.get(Filing, filing_id)
        if f is None:
            return f"filing #{filing_id}"
        company = s.get(Company, f.cik)
        who = ""
        if company and company.name:
            who = f"  {company.name}"
            if company.ticker:
                who += f" ({company.ticker})"
        return (f"filing #{f.id}  {f.form_type or '-'}  "
                f"filed {f.filing_date or '-'}{who}\n"
                f"    accession: {f.accession_no}")


def _print_verdict(a: Analysis) -> None:
    # Error case: no score was produced (e.g. API billing / rate limit).
    if a.materiality_score is None:
        print(f"\n  ✗ {_filing_header(a.filing_id)}")
        print(f"    NOT ANALYSED: {a.summary}")
        return
    icon = _SEV_ICON.get(a.severity or "", "?")
    print(f"\n  {icon} {_filing_header(a.filing_id)}")
    print(f"    score={a.materiality_score}  severity={a.severity}  "
          f"action={a.recommended_action}")
    if a.summary:
        print(f"    summary: {a.summary}")
    for r in (a.reasons or []):
        print(f"      - {r}")
    if a.tags:
        print(f"    tags   : {', '.join(a.tags)}")
    if a.sources_used:
        print(f"    sources: {', '.join(a.sources_used)}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m sec_notice.analyse")
    parser.add_argument("cik", type=int, nargs="?", help="company CIK")
    parser.add_argument("--filing-id", type=int, help="analyse a single filing by id")
    parser.add_argument("--forms", default=None, help="comma-separated form types")
    parser.add_argument("--limit", type=int, default=5, help="max filings to analyse")
    args = parser.parse_args(argv[1:])

    if not config.anthropic_api_key:
        print("ERROR: ANTHROPIC_API_KEY not set. Add it to .env.")
        return 1

    init_db()

    if args.filing_id is not None:
        print(f"Analysing filing #{args.filing_id} with {config.analyst_model} ...")
        _print_verdict(analyse_filing(args.filing_id))
        print("\nDone.")
        return 0

    if args.cik is None:
        parser.print_help()
        return 2

    forms = (
        {f.strip() for f in args.forms.split(",") if f.strip()}
        if args.forms else set(config.material_forms)
    )
    print(f"Analysing up to {args.limit} fetched filings for CIK {args.cik} "
          f"(forms: {', '.join(sorted(forms))}) with {config.analyst_model} ...")
    results = analyse_pending(args.cik, forms=forms, limit=args.limit)
    if not results:
        print("\n  No fetched+unanalysed filings matched. "
              "Run `discover` to fetch documents first.")
    for a in results:
        _print_verdict(a)
    print(f"\nDone. {len(results)} filing(s) analysed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

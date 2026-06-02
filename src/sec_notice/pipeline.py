"""Ingest pipeline.

Phase 1: ingest a single filing from a URL (``ingest_filing``).
Phase 2: discover a whole company's filings and backfill documents
(``record_company_filings`` + ``fetch_pending_documents``).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import config
from .edgar.client import EdgarClient
from .edgar.discover import CompanyMeta, FilingMeta, discover_company
from .edgar.fetch import (
    DocumentRef,
    FilingRef,
    archive_base_url,
    fetch_documents,
    fetch_filing,
)
from .store.db import init_db, session_scope
from .store.models import Analysis, Company, Document, Filing


# --------------------------------------------------------------------------- #
# Upsert helpers (shared by single-filing ingest and company backfill)
# --------------------------------------------------------------------------- #
def _upsert_company(
    session: Session,
    cik: int,
    *,
    name: str | None = None,
    ticker: str | None = None,
    sic: str | None = None,
) -> Company:
    company = session.get(Company, cik)
    if company is None:
        company = Company(cik=cik)
        session.add(company)
    company.name = name or company.name
    company.ticker = ticker or company.ticker
    company.sic = sic or company.sic
    return company


def _upsert_filing(
    session: Session,
    cik: int,
    accession_no: str,
    *,
    form_type: str | None,
    filing_date: str | None,
    period_of_report: str | None,
    primary_doc_url: str | None,
    status: str | None = None,
    status_on_create: str = "new",
) -> tuple[Filing, bool]:
    """Insert or update a filing row. Returns (filing, created).

    ``status`` (if given) is applied always. ``status_on_create`` is used only
    when inserting a new row, so re-discovering a company never resets an
    already-fetched filing back to 'new'.
    """
    filing = session.scalar(
        select(Filing).where(Filing.accession_no == accession_no)
    )
    created = filing is None
    if filing is None:
        filing = Filing(accession_no=accession_no, cik=cik, status=status_on_create)
        session.add(filing)
    filing.form_type = form_type
    filing.filing_date = filing_date
    filing.period_of_report = period_of_report
    filing.primary_doc_url = primary_doc_url
    if status is not None:
        filing.status = status
    session.flush()  # assign filing.id
    return filing, created


def _upsert_documents(
    session: Session,
    filing: Filing,
    docs: list[DocumentRef],
    paths: dict[str, str],
) -> None:
    existing = {d.filename: d for d in filing.documents}
    for d in docs:
        doc = existing.get(d.filename)
        if doc is None:
            doc = Document(filing_id=filing.id, filename=d.filename, url=d.url)
            session.add(doc)
        doc.doc_type = d.doc_type
        doc.size = d.size
        doc.url = d.url
        doc.local_path = paths.get(d.filename)
        doc.extracted_text = d.text


def _save_documents_to_disk(
    cik: int, accession_nodash: str, docs: list[DocumentRef]
) -> dict[str, str]:
    dest = config.data_dir / str(cik) / accession_nodash
    dest.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for doc in docs:
        if doc.content is None:
            continue
        path = dest / doc.filename
        path.write_bytes(doc.content)
        paths[doc.filename] = str(path)
    return paths


# --------------------------------------------------------------------------- #
# Phase 1: single filing from a URL
# --------------------------------------------------------------------------- #
def ingest_filing(url: str) -> int:
    """Fetch one filing and persist company/filing/documents. Returns filing id.

    Idempotent: re-running for the same accession updates rows in place.
    """
    init_db()
    ref: FilingRef = fetch_filing(url)
    paths = _save_documents_to_disk(ref.cik, ref.accession_nodash, ref.documents)

    with session_scope() as session:
        _upsert_company(session, ref.cik, name=ref.company_name, ticker=ref.ticker)
        filing, _ = _upsert_filing(
            session,
            ref.cik,
            ref.accession_no,
            form_type=ref.form_type,
            filing_date=ref.filing_date,
            period_of_report=ref.period_of_report,
            primary_doc_url=(
                f"{ref.base_url}/{ref.primary_document}"
                if ref.primary_document
                else None
            ),
            status="fetched",
        )
        _upsert_documents(session, filing, ref.documents, paths)
        return filing.id


# --------------------------------------------------------------------------- #
# Phase 2: company backfill
# --------------------------------------------------------------------------- #
@dataclass
class BackfillStats:
    cik: int
    company: str | None
    discovered: int
    new_filings: int
    docs_fetched: int


def record_company_filings(
    cik: int,
    *,
    forms: set[str] | None = None,
    client: EdgarClient | None = None,
) -> tuple[CompanyMeta, list[FilingMeta], int]:
    """Discover all filings for a CIK and upsert metadata rows (no documents).

    Returns (company_meta, all_discovered_filings, new_count). New filings are
    recorded with status="new"; existing ones keep their current status.
    """
    init_db()
    company, filings = discover_company(cik, forms=forms, client=client)

    new_count = 0
    with session_scope() as session:
        _upsert_company(
            session, cik, name=company.name, ticker=company.ticker, sic=company.sic
        )
        for f in filings:
            primary_url = (
                f"{archive_base_url(cik, f.accession_nodash)}/{f.primary_document}"
                if f.primary_document
                else None
            )
            _, created = _upsert_filing(
                session,
                cik,
                f.accession_no,
                form_type=f.form_type,
                filing_date=f.filing_date,
                period_of_report=f.period_of_report,
                primary_doc_url=primary_url,
                status_on_create="new",
            )
            if created:
                new_count += 1
    return company, filings, new_count


def fetch_pending_documents(
    cik: int,
    *,
    forms: set[str] | None = None,
    limit: int | None = None,
    client: EdgarClient | None = None,
) -> int:
    """Download documents for filings still in status='new'. Returns count fetched.

    Processes newest filings first; ``limit`` caps how many filings are fetched.
    ``forms`` restricts to matching form types (case-insensitive) so a filtered
    discover only downloads documents for the forms the caller asked about.
    """
    owns_client = client is None
    client = client or EdgarClient()
    fetched = 0
    try:
        # Collect the pending accessions first (short transaction).
        with session_scope() as session:
            stmt = (
                select(Filing)
                .where(Filing.cik == cik, Filing.status == "new")
                .order_by(Filing.filing_date.desc())
            )
            if forms:
                stmt = stmt.where(
                    func.upper(Filing.form_type).in_({f.upper() for f in forms})
                )
            pending = session.scalars(stmt).all()
            targets = [(f.accession_no, f.accession_no.replace("-", "")) for f in pending]
        if limit is not None:
            targets = targets[:limit]

        for accession_no, accession_nodash in targets:
            docs = fetch_documents(client, cik, accession_nodash)
            paths = _save_documents_to_disk(cik, accession_nodash, docs)
            with session_scope() as session:
                filing = session.scalar(
                    select(Filing).where(Filing.accession_no == accession_no)
                )
                _upsert_documents(session, filing, docs, paths)
                filing.status = "fetched"
            fetched += 1
    finally:
        if owns_client:
            client.close()
    return fetched


def backfill_company(
    cik: int,
    *,
    forms: set[str] | None = None,
    limit: int | None = None,
    metadata_only: bool = False,
) -> BackfillStats:
    """Discover a company's filings, record metadata, and optionally fetch docs."""
    with EdgarClient() as client:
        company, filings, new_count = record_company_filings(
            cik, forms=forms, client=client
        )
        docs_fetched = 0
        if not metadata_only:
            docs_fetched = fetch_pending_documents(
                cik, forms=forms, limit=limit, client=client
            )
    return BackfillStats(
        cik=cik,
        company=company.name,
        discovered=len(filings),
        new_filings=new_count,
        docs_fetched=docs_fetched,
    )


# --------------------------------------------------------------------------- #
# Phase 3: AI materiality analysis
# --------------------------------------------------------------------------- #
def analyse_filing(filing_id: int) -> Analysis:
    """Run the AI analyst on one fetched filing and persist the verdict.

    Advances the filing from 'fetched' to 'analysed'. Imported lazily so the
    rest of the pipeline doesn't require the Agent SDK.
    """
    from .agent.analyst import analyse as run_analyst

    with session_scope() as session:
        filing = session.get(Filing, filing_id)
        if filing is None:
            raise ValueError(f"filing {filing_id} not found")
        company = session.get(Company, filing.cik)
        ctx = {
            "filing_id": filing.id,
            "cik": filing.cik,
            "form_type": filing.form_type,
            "filing_date": filing.filing_date,
            "company": company.name if company else None,
            "ticker": company.ticker if company else None,
        }

    result = run_analyst(ctx)
    v = result.verdict
    analysis = Analysis(
        filing_id=filing_id,
        model=result.model,
        materiality_score=v.get("materiality_score"),
        severity=v.get("severity"),
        recommended_action=v.get("recommended_action"),
        summary=v.get("summary") or v.get("error"),
        reasons=v.get("reasons"),
        tags=v.get("tags"),
        sources_used=result.sources_used,
    )
    # Only persist + advance status on a real verdict; error results (billing,
    # rate limit, etc.) are returned for display but not stored.
    if result.ok:
        with session_scope() as session:
            filing = session.get(Filing, filing_id)
            session.add(analysis)
            filing.status = "analysed"
            session.flush()
            session.refresh(analysis)
            session.expunge(analysis)
    return analysis


def analyse_pending(
    cik: int,
    *,
    forms: set[str] | None = None,
    limit: int | None = None,
) -> list[Analysis]:
    """Analyse fetched-but-not-analysed filings for a CIK (material forms only).

    ``forms`` defaults to the configured material forms. Newest first.
    """
    forms = forms or set(config.material_forms)
    with session_scope() as session:
        stmt = (
            select(Filing.id)
            .where(Filing.cik == cik, Filing.status == "fetched")
            .order_by(Filing.filing_date.desc())
        )
        if forms:
            stmt = stmt.where(
                func.upper(Filing.form_type).in_({f.upper() for f in forms})
            )
        ids = list(session.scalars(stmt).all())
    if limit is not None:
        ids = ids[:limit]

    results: list[Analysis] = []
    for fid in ids:
        analysis = analyse_filing(fid)
        results.append(analysis)
        # Stop early on a failed analysis — errors here (billing, rate limit)
        # are almost always global, so continuing would just burn API calls.
        if analysis.materiality_score is None:
            break
    return results

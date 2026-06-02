"""Phase 1 pipeline: ingest a single filing into the store."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from .config import config
from .edgar.fetch import FilingRef, fetch_filing
from .store.db import init_db, session_scope
from .store.models import Company, Document, Filing


def _save_documents_to_disk(ref: FilingRef) -> dict[str, str]:
    """Write downloaded document bytes to DATA_DIR; return {filename: path}."""
    dest = config.data_dir / str(ref.cik) / ref.accession_nodash
    dest.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for doc in ref.documents:
        if doc.content is None:
            continue
        path = dest / doc.filename
        path.write_bytes(doc.content)
        paths[doc.filename] = str(path)
    return paths


def ingest_filing(url: str) -> int:
    """Fetch one filing and persist company/filing/documents. Returns filing id.

    Idempotent: re-running for the same accession updates the existing rows
    rather than duplicating them.
    """
    init_db()
    ref = fetch_filing(url)
    paths = _save_documents_to_disk(ref)

    with session_scope() as session:
        company = session.get(Company, ref.cik)
        if company is None:
            company = Company(cik=ref.cik)
            session.add(company)
        company.name = ref.company_name or company.name
        company.ticker = ref.ticker or company.ticker

        filing = session.scalar(
            select(Filing).where(Filing.accession_no == ref.accession_no)
        )
        if filing is None:
            filing = Filing(accession_no=ref.accession_no, cik=ref.cik)
            session.add(filing)
        filing.form_type = ref.form_type
        filing.filing_date = ref.filing_date
        filing.period_of_report = ref.period_of_report
        filing.primary_doc_url = (
            f"{ref.base_url}/{ref.primary_document}" if ref.primary_document else None
        )
        filing.status = "fetched"
        session.flush()  # assign filing.id

        existing = {d.filename: d for d in filing.documents}
        for d in ref.documents:
            doc = existing.get(d.filename)
            if doc is None:
                doc = Document(filing_id=filing.id, filename=d.filename, url=d.url)
                session.add(doc)
            doc.doc_type = d.doc_type
            doc.size = d.size
            doc.url = d.url
            doc.local_path = paths.get(d.filename)
            doc.extracted_text = d.text

        session.flush()
        return filing.id

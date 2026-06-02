"""Discover every filing for a company from the EDGAR submissions JSON.

    https://data.sec.gov/submissions/CIK##########.json

The `filings.recent` block holds the most recent ~1000 filings as parallel
arrays. Companies with more history reference additional files under
`filings.files`, each served at data.sec.gov/submissions/<name> with the same
parallel-array layout at top level. We stitch them together.
"""

from __future__ import annotations

from dataclasses import dataclass

from .client import EdgarClient

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_FILE_URL = "https://data.sec.gov/submissions/{name}"


@dataclass
class CompanyMeta:
    cik: int
    name: str | None
    ticker: str | None
    sic: str | None


@dataclass
class FilingMeta:
    cik: int
    accession_no: str  # dashed, e.g. 0001193125-26-249768
    accession_nodash: str
    form_type: str | None
    filing_date: str | None
    period_of_report: str | None
    primary_document: str | None


def _rows_from_arrays(cik: int, block: dict) -> list[FilingMeta]:
    """Turn a parallel-array filings block into FilingMeta rows."""
    accessions = block.get("accessionNumber") or []
    forms = block.get("form") or []
    dates = block.get("filingDate") or []
    reports = block.get("reportDate") or []
    primaries = block.get("primaryDocument") or []

    rows: list[FilingMeta] = []
    for i, acc in enumerate(accessions):
        rows.append(
            FilingMeta(
                cik=cik,
                accession_no=acc,
                accession_nodash=acc.replace("-", ""),
                form_type=_idx(forms, i),
                filing_date=_idx(dates, i),
                period_of_report=_idx(reports, i),
                primary_document=_idx(primaries, i),
            )
        )
    return rows


def _idx(seq, i):
    return seq[i] if seq and i < len(seq) else None


def discover_company(
    cik: int,
    *,
    forms: set[str] | None = None,
    client: EdgarClient | None = None,
) -> tuple[CompanyMeta, list[FilingMeta]]:
    """Return company metadata and all matching filings (newest first).

    forms: optional set of form types to keep (case-insensitive, e.g.
    {"8-K", "10-K"}). None means all forms.
    """
    owns_client = client is None
    client = client or EdgarClient()
    try:
        data = client.get_json(SUBMISSIONS_URL.format(cik=cik))

        company = CompanyMeta(
            cik=cik,
            name=data.get("name"),
            ticker=(data.get("tickers") or [None])[0],
            sic=data.get("sicDescription") or (str(data["sic"]) if data.get("sic") else None),
        )

        filings_block = data.get("filings") or {}
        rows = _rows_from_arrays(cik, filings_block.get("recent") or {})

        # Older history lives in separate files.
        for extra in filings_block.get("files") or []:
            name = extra.get("name")
            if not name:
                continue
            block = client.get_json(SUBMISSIONS_FILE_URL.format(name=name))
            rows.extend(_rows_from_arrays(cik, block))
    finally:
        if owns_client:
            client.close()

    if forms:
        wanted = {f.upper() for f in forms}
        rows = [r for r in rows if (r.form_type or "").upper() in wanted]

    return company, rows

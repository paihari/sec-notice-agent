"""Discover and download the documents that make up a single EDGAR filing.

EDGAR archive layout:
    https://www.sec.gov/Archives/edgar/data/<CIK>/<ACCESSION>/<DOCUMENT>

We resolve a pasted document URL back to its (CIK, accession), list the
filing's documents via `index.json`, and enrich form/date metadata from the
company submissions JSON (`data.sec.gov/submissions/CIK##########.json`).
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from .client import EdgarClient

# Some filing documents are XML served with an .htm/.html-ish name; we extract
# text from them with the HTML parser deliberately, so silence the heuristic.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Matches .../edgar/data/<cik>/<accession>[/<document>]
_URL_RE = re.compile(
    r"/edgar/data/(?P<cik>\d+)/(?P<accession>\d{18})(?:/(?P<doc>[^/?#]+))?"
)

# Documents we extract plain text from in Phase 1.
_TEXT_EXTENSIONS = (".htm", ".html", ".txt")


@dataclass
class DocumentRef:
    filename: str
    url: str
    doc_type: str | None = None
    size: int | None = None
    content: bytes | None = None
    text: str | None = None


@dataclass
class FilingRef:
    cik: int
    accession_no: str  # dashed form, e.g. 0001193125-26-249768
    accession_nodash: str
    base_url: str
    form_type: str | None = None
    filing_date: str | None = None
    period_of_report: str | None = None
    primary_document: str | None = None
    company_name: str | None = None
    ticker: str | None = None
    documents: list[DocumentRef] = field(default_factory=list)


def parse_filing_url(url: str) -> tuple[int, str]:
    """Return (cik, accession_nodash) from any EDGAR archive URL."""
    m = _URL_RE.search(url)
    if not m:
        raise ValueError(f"Not a recognizable EDGAR archive URL: {url}")
    return int(m.group("cik")), m.group("accession")


def _dash_accession(nodash: str) -> str:
    return f"{nodash[:10]}-{nodash[10:12]}-{nodash[12:]}"


def _enrich_from_submissions(client: EdgarClient, ref: FilingRef) -> None:
    """Best-effort: fill form/date/primary-doc + company name from submissions."""
    try:
        data = client.get_json(SUBMISSIONS_URL.format(cik=ref.cik))
    except Exception:
        return  # metadata is optional in Phase 1

    ref.company_name = data.get("name")
    tickers = data.get("tickers") or []
    ref.ticker = tickers[0] if tickers else None

    recent = (data.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    for i, acc in enumerate(accessions):
        if acc == ref.accession_no:
            ref.form_type = _idx(recent.get("form"), i)
            ref.filing_date = _idx(recent.get("filingDate"), i)
            ref.period_of_report = _idx(recent.get("reportDate"), i)
            ref.primary_document = _idx(recent.get("primaryDocument"), i)
            break


def _idx(seq, i):
    return seq[i] if seq and i < len(seq) else None


def extract_text(content: bytes, filename: str) -> str | None:
    """Plain text from an HTML/text document; None for binary docs."""
    if not filename.lower().endswith(_TEXT_EXTENSIONS):
        return None
    if filename.lower().endswith(".txt"):
        return content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(content, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def fetch_filing(url: str, *, download_text_only: bool = True) -> FilingRef:
    """Resolve a filing URL to a fully-populated FilingRef with documents.

    download_text_only: when True, only HTML/text documents are downloaded and
    text-extracted (Phase 1 default). Other documents are recorded as metadata.
    """
    cik, accession_nodash = parse_filing_url(url)
    accession_no = _dash_accession(accession_nodash)
    base_url = f"{ARCHIVES_BASE}/{cik}/{accession_nodash}"

    ref = FilingRef(
        cik=cik,
        accession_no=accession_no,
        accession_nodash=accession_nodash,
        base_url=base_url,
    )

    with EdgarClient() as client:
        _enrich_from_submissions(client, ref)

        index = client.get_json(f"{base_url}/index.json")
        items = (index.get("directory") or {}).get("item") or []
        for item in items:
            name = item.get("name")
            if not name or name.endswith("/"):
                continue
            # index.json's "type" is the row-icon filename (e.g. "text.gif"),
            # not a document type. Derive a useful type from the extension.
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            doc = DocumentRef(
                filename=name,
                url=f"{base_url}/{name}",
                doc_type=ext or None,
                size=int(item["size"]) if item.get("size") else None,
            )
            is_text = name.lower().endswith(_TEXT_EXTENSIONS)
            if is_text or not download_text_only:
                doc.content = client.get_bytes(doc.url)
                doc.text = extract_text(doc.content, name)
            ref.documents.append(doc)

    return ref

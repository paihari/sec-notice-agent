"""Data-layer backing the analyst's cross-check tools.

Plain synchronous functions returning JSON-able dicts. Every function is
defensive: on failure it returns a dict with an ``error`` key rather than
raising, so a flaky external source never breaks the agent loop.

Sources:
- internal DB        : filing text, prior filings, insider (Form 4) activity
- SEC XBRL API       : company fundamentals (free, rate-limited)
- yfinance           : price/volume reaction with a market baseline
- CoinGecko (keyless): bitcoin context for crypto-linked issuers
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select

from ..edgar.client import EdgarClient
from ..store.db import session_scope
from ..store.models import Company, Document, Filing

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
COINGECKO_HISTORY = "https://api.coingecko.com/api/v3/coins/bitcoin/history"

_FILING_TEXT_MAX = 12_000

# Markers where the human-readable filing body begins (after any inline-XBRL
# token soup the HTML parser dumps at the top of the document).
_BODY_MARKERS = (
    "UNITED STATES SECURITIES AND EXCHANGE COMMISSION",
    "SECURITIES AND EXCHANGE COMMISSION",
    "FORM 8-K",
    "FORM 10-K",
    "FORM 10-Q",
)
_XBRL_HINTS = ("us-gaap:", "dei:", "xbrli", "Member ", "iso4217")


def _strip_xbrl_prefix(text: str) -> str:
    """Drop the leading inline-XBRL tag dump that precedes the document body.

    Only strips when the leading chunk actually looks like XBRL, so plain-text
    filings are left untouched.
    """
    for marker in _BODY_MARKERS:
        idx = text.find(marker)
        if idx > 0:
            prefix = text[:idx]
            if any(h in prefix for h in _XBRL_HINTS):
                return text[idx:]
    return text


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Internal DB
# --------------------------------------------------------------------------- #
def get_filing_text(filing_id: int) -> dict:
    """Readable text of a filing's primary document (truncated)."""
    with session_scope() as s:
        f = s.get(Filing, filing_id)
        if f is None:
            return {"error": f"filing {filing_id} not found"}
        docs = list(f.documents)
        primary = f.primary_doc_url.rsplit("/", 1)[-1] if f.primary_doc_url else None

        text, chosen = None, None
        # 1) the primary document
        for d in docs:
            if primary and d.filename == primary and d.extracted_text:
                text, chosen = d.extracted_text, d.filename
                break
        # 2) largest non-index, non-full-submission text doc
        if text is None:
            cands = [
                d for d in docs
                if d.extracted_text
                and not d.filename.lower().endswith(".txt")
                and "index" not in d.filename.lower()
            ]
            if cands:
                d = max(cands, key=lambda d: len(d.extracted_text))
                text, chosen = d.extracted_text, d.filename
        # 3) anything with text
        if text is None:
            cands = [d for d in docs if d.extracted_text]
            if cands:
                d = max(cands, key=lambda d: len(d.extracted_text))
                text, chosen = d.extracted_text, d.filename
        if text is None:
            return {"error": "no extracted text for this filing (run fetch first)"}

        text = _strip_xbrl_prefix(text)
        return {
            "filing_id": f.id,
            "form_type": f.form_type,
            "filing_date": f.filing_date,
            "document": chosen,
            "truncated": len(text) > _FILING_TEXT_MAX,
            "text": text[:_FILING_TEXT_MAX],
        }


def get_prior_filings(cik: int, form_type: str | None = None, n: int = 5,
                      before_date: str | None = None) -> dict:
    """Recent prior filings for context ('is this a change?')."""
    with session_scope() as s:
        stmt = select(Filing).where(Filing.cik == cik)
        if form_type:
            stmt = stmt.where(Filing.form_type == form_type)
        if before_date:
            stmt = stmt.where(Filing.filing_date < before_date)
        stmt = stmt.order_by(Filing.filing_date.desc()).limit(n)
        rows = s.scalars(stmt).all()
        return {
            "cik": cik,
            "form_type": form_type,
            "filings": [
                {"accession_no": r.accession_no, "form_type": r.form_type,
                 "filing_date": r.filing_date}
                for r in rows
            ],
        }


def get_insider_activity(cik: int, before_date: str | None = None,
                         days: int = 30) -> dict:
    """Count of Form 4 (insider) filings in a window before a date.

    A proxy signal: we count insider filings; we don't parse buy/sell direction
    (most Form 4 docs aren't downloaded). Clustering still indicates activity.
    """
    end = _parse_date(before_date) or date.today()
    start = end - timedelta(days=days)
    with session_scope() as s:
        rows = s.scalars(
            select(Filing).where(
                Filing.cik == cik,
                Filing.form_type.in_(["4", "4/A"]),
                Filing.filing_date >= start.isoformat(),
                Filing.filing_date <= end.isoformat(),
            ).order_by(Filing.filing_date.desc())
        ).all()
        return {
            "cik": cik,
            "window_days": days,
            "as_of": end.isoformat(),
            "form4_count": len(rows),
            "recent": [
                {"accession_no": r.accession_no, "filing_date": r.filing_date}
                for r in rows[:10]
            ],
        }


# --------------------------------------------------------------------------- #
# SEC XBRL fundamentals (for scaling magnitude)
# --------------------------------------------------------------------------- #
def get_company_facts(cik: int) -> dict:
    """Latest revenue / assets / equity / shares outstanding from SEC XBRL."""
    try:
        with EdgarClient() as client:
            data = client.get_json(COMPANYFACTS_URL.format(cik=cik))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"companyfacts fetch failed: {exc}"}

    facts = data.get("facts", {})
    usgaap = facts.get("us-gaap", {})

    def latest_usd(concepts: list[str]) -> dict | None:
        for cn in concepts:
            units = (usgaap.get(cn) or {}).get("units", {}).get("USD")
            if units:
                v = max(units, key=lambda u: u.get("end", ""))
                return {"concept": cn, "value": v["val"],
                        "period_end": v.get("end"), "form": v.get("form")}
        return None

    shares = None
    sc = (facts.get("dei", {}).get("EntityCommonStockSharesOutstanding") or {})
    units = sc.get("units", {}).get("shares")
    if units:
        v = max(units, key=lambda u: u.get("end", ""))
        shares = {"value": v["val"], "period_end": v.get("end")}

    return {
        "cik": cik,
        "entity": data.get("entityName"),
        "revenue": latest_usd([
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
        ]),
        "total_assets": latest_usd(["Assets"]),
        "stockholders_equity": latest_usd(["StockholdersEquity"]),
        "shares_outstanding": shares,
    }


# --------------------------------------------------------------------------- #
# Market reaction (yfinance) with baseline de-confounding
# --------------------------------------------------------------------------- #
def get_price_reaction(ticker: str, filing_date: str,
                       baseline: str = "SPY") -> dict:
    """Price/volume reaction around the filing date vs a market baseline.

    Returns the filing-day and next-day returns, volume vs 30-day average, and
    the baseline's same-day return so a market-wide move isn't mis-attributed.
    For very recent filings the post-filing days may not exist yet.
    """
    d = _parse_date(filing_date)
    if d is None:
        return {"error": f"bad filing_date: {filing_date!r}"}
    try:
        import yfinance as yf

        start = (d - timedelta(days=60)).isoformat()
        end = (d + timedelta(days=8)).isoformat()
        hist = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
        if hist.empty:
            return {"ticker": ticker, "error": "no price data (check ticker)"}

        closes = hist["Close"]
        vols = hist["Volume"]
        # trading day on/after the filing date
        idx = [ts for ts in closes.index if ts.date() >= d]
        if not idx:
            return {"ticker": ticker,
                    "error": "no trading day on/after filing date yet"}
        fday = idx[0]
        pos = closes.index.get_loc(fday)

        def pct(a, b):
            return round((a / b - 1) * 100, 2) if b else None

        filing_close = float(closes.iloc[pos])
        prev_close = float(closes.iloc[pos - 1]) if pos >= 1 else None
        next_close = float(closes.iloc[pos + 1]) if pos + 1 < len(closes) else None

        prior_vol = vols.iloc[max(0, pos - 30):pos]
        avg_vol = float(prior_vol.mean()) if len(prior_vol) else None
        fvol = float(vols.iloc[pos])

        out = {
            "ticker": ticker,
            "filing_trading_day": fday.date().isoformat(),
            "return_filing_day_pct": pct(filing_close, prev_close),
            "return_next_day_pct": pct(next_close, filing_close) if next_close else None,
            "volume_vs_30d_avg": round(fvol / avg_vol, 2) if avg_vol else None,
        }

        # baseline for de-confounding
        try:
            bhist = yf.Ticker(baseline).history(start=start, end=end, auto_adjust=True)
            bcl = bhist["Close"]
            bidx = [ts for ts in bcl.index if ts.date() >= d]
            if bidx:
                bpos = bcl.index.get_loc(bidx[0])
                if bpos >= 1:
                    out["baseline"] = baseline
                    out["baseline_return_filing_day_pct"] = pct(
                        float(bcl.iloc[bpos]), float(bcl.iloc[bpos - 1])
                    )
        except Exception:  # noqa: BLE001
            pass
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ticker": ticker, "error": f"price lookup failed: {exc}"}


# --------------------------------------------------------------------------- #
# Bitcoin context (CoinGecko, keyless) for crypto-linked issuers
# --------------------------------------------------------------------------- #
def get_btc_context(filing_date: str) -> dict:
    """Bitcoin USD price on the filing date and 7-day change.

    Use only for issuers whose value is tied to crypto holdings (e.g. MSTR).
    """
    d = _parse_date(filing_date)
    if d is None:
        return {"error": f"bad filing_date: {filing_date!r}"}

    def price_on(day: date) -> float | None:
        try:
            r = httpx.get(
                COINGECKO_HISTORY,
                params={"date": day.strftime("%d-%m-%Y"), "localization": "false"},
                headers={"accept": "application/json"},
                timeout=20.0,
            )
            r.raise_for_status()
            return (r.json().get("market_data", {})
                    .get("current_price", {}).get("usd"))
        except Exception:  # noqa: BLE001
            return None

    price = price_on(d)
    if price is None:
        return {"error": "BTC price unavailable for that date"}
    prior = price_on(d - timedelta(days=7))
    change = round((price / prior - 1) * 100, 2) if prior else None
    return {
        "date": d.isoformat(),
        "btc_usd": round(price, 2),
        "btc_7d_change_pct": change,
    }


def get_company_ticker(cik: int) -> str | None:
    with session_scope() as s:
        c = s.get(Company, cik)
        return c.ticker if c else None

"""The materiality analyst — a Claude Agent SDK loop with cross-check tools.

Given a filing, the agent reads it, calls the cross-check tools (internal
history, SEC fundamentals, market reaction, news, BTC context), reconciles the
three materiality lenses, and returns a structured verdict via the SDK's
``output_format`` (JSON schema -> ResultMessage.structured_output).
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from ..config import config
from . import datasources as ds

SERVER_NAME = "secnotice"


# --------------------------------------------------------------------------- #
# Tool wrappers (thin async adapters over the sync data layer)
# --------------------------------------------------------------------------- #
def _ok(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def _none(v):
    """Treat empty strings the agent may send as 'not provided'."""
    return v if v not in ("", None) else None


@tool("get_filing_text", "Get the readable text of the filing being analysed.",
      {"filing_id": int})
async def _t_filing_text(args):
    return _ok(ds.get_filing_text(int(args["filing_id"])))


@tool("get_prior_filings",
      "List the company's recent prior filings (optionally of one form type) "
      "before a date, to judge whether this filing is a change.",
      {"cik": int, "form_type": str, "n": int, "before_date": str})
async def _t_prior(args):
    return _ok(ds.get_prior_filings(
        int(args["cik"]), _none(args.get("form_type")),
        int(args.get("n") or 5), _none(args.get("before_date"))))


@tool("get_insider_activity",
      "Count of insider (Form 4) filings in a window before a date — a clustering "
      "signal of insider activity around the event.",
      {"cik": int, "before_date": str, "days": int})
async def _t_insider(args):
    return _ok(ds.get_insider_activity(
        int(args["cik"]), _none(args.get("before_date")), int(args.get("days") or 30)))


@tool("get_company_facts",
      "Latest revenue / total assets / equity from SEC XBRL, to scale the "
      "magnitude of any dollar figure in the filing.",
      {"cik": int})
async def _t_facts(args):
    return _ok(ds.get_company_facts(int(args["cik"])))


@tool("get_price_reaction",
      "Stock price/volume reaction around the filing date vs a market baseline "
      "(SPY). Reveals whether the market actually reacted, de-confounded from "
      "broad market moves.",
      {"ticker": str, "filing_date": str})
async def _t_price(args):
    return _ok(ds.get_price_reaction(str(args["ticker"]), str(args["filing_date"])))


@tool("get_btc_context",
      "Bitcoin USD price and 7-day change on the filing date. Use ONLY for "
      "issuers whose value is tied to crypto holdings (e.g. MSTR/Strategy).",
      {"filing_date": str})
async def _t_btc(args):
    return _ok(ds.get_btc_context(str(args["filing_date"])))


_TOOLS = [_t_filing_text, _t_prior, _t_insider, _t_facts, _t_price, _t_btc]

ALLOWED_TOOLS = ["WebSearch"] + [f"mcp__{SERVER_NAME}__{t.name}" for t in _TOOLS]

VERDICT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "materiality_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "severity": {"type": "string",
                         "enum": ["info", "notable", "high", "critical"]},
            "recommended_action": {"type": "string",
                                   "enum": ["notify", "watch", "ignore"]},
            "summary": {"type": "string"},
            "reasons": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["materiality_score", "severity", "recommended_action",
                     "summary", "reasons"],
    },
}

SYSTEM_PROMPT = """\
You are a securities-filing materiality analyst. Given one SEC filing, decide \
how market-moving / material it is and produce a structured verdict.

Triangulate THREE lenses, do not rely on the filing text alone:
1. STATED  — what the filing claims. Call get_filing_text first.
2. RELATIVE — is this a change vs history and how big is it? Use \
get_prior_filings, get_insider_activity, and get_company_facts (to scale any \
dollar amount against revenue/assets).
3. REALIZED — did the market actually react? Use get_price_reaction, and \
WebSearch for corroborating news/analyst reaction. For crypto-linked issuers \
(e.g. MSTR/Strategy), also call get_btc_context.

Rules:
- CITE OR OMIT: every number or fact in your reasons must come from a tool \
result. Do not invent figures.
- DE-CONFOUND: if get_price_reaction shows the move matches the market baseline, \
do not credit it to the filing.
- A high stated importance that the market ignored is NOT highly material; a \
quiet filing that moved the stock IS. Let realized reaction adjust the score.
- Keep `summary` to 2-3 sentences. `reasons` are short, evidence-backed bullets.

Scoring: 0-39 info, 40-59 notable, 60-79 high, 80-100 critical. \
recommended_action: notify (>=60 and genuinely actionable), watch, or ignore.

When done, output the structured verdict.
"""


@dataclass
class AnalystResult:
    verdict: dict
    sources_used: list[str]
    model: str

    @property
    def ok(self) -> bool:
        return "error" not in self.verdict


def _explain_failure(exc: Exception | None, last_text: str | None) -> str:
    """Turn an opaque SDK/CLI failure into an actionable message."""
    text = (last_text or "").lower()
    if "credit balance is too low" in text or "credit balance" in text:
        return ("Anthropic API credit balance is too low — add credits at "
                "console.anthropic.com (Plan & Billing), then retry. "
                "(The pipeline code is fine; the API call was refused.)")
    if "rate limit" in text or "rate_limit" in text:
        return "Anthropic API rate limited — wait and retry."
    if last_text:
        return f"Agent run failed: {last_text}"
    return f"Agent run failed: {exc}" if exc else "Agent produced no result."


def _build_options() -> ClaudeAgentOptions:
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_TOOLS)
    env = {}
    if config.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=config.analyst_model,
        max_turns=16,
        tools=["WebSearch"],
        mcp_servers={SERVER_NAME: server},
        allowed_tools=ALLOWED_TOOLS,
        output_format=VERDICT_SCHEMA,
        env=env,
    )


def _prompt_for(filing: dict) -> str:
    return (
        "Analyse this SEC filing for materiality.\n"
        f"- filing_id: {filing['filing_id']}\n"
        f"- cik: {filing['cik']}\n"
        f"- company: {filing.get('company')}\n"
        f"- ticker: {filing.get('ticker')}\n"
        f"- form_type: {filing.get('form_type')}\n"
        f"- filing_date: {filing.get('filing_date')}\n"
        "Use the tools, then output the structured verdict."
    )


async def _run(filing: dict) -> AnalystResult:
    options = _build_options()
    sources: list[str] = []
    verdict: dict | None = None
    last_text: str | None = None

    try:
        async for message in query(prompt=_prompt_for(filing), options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        last_text = block.text.strip()
                    name = getattr(block, "name", None)
                    if name and getattr(block, "input", None) is not None:
                        clean = name.replace(f"mcp__{SERVER_NAME}__", "")
                        if clean != "StructuredOutput":  # SDK output mechanism
                            sources.append(clean)
            elif isinstance(message, ResultMessage):
                verdict = getattr(message, "structured_output", None)
                if verdict is None and getattr(message, "result", None):
                    try:
                        verdict = json.loads(message.result)
                    except (json.JSONDecodeError, TypeError):
                        verdict = {"error": "no structured output",
                                   "raw": message.result}
    except Exception as exc:  # noqa: BLE001
        # The SDK raises when the CLI returns an error result (e.g. billing,
        # rate limit). The model's last text often holds the real reason.
        verdict = {"error": _explain_failure(exc, last_text)}

    if verdict is None:
        verdict = {"error": _explain_failure(None, last_text)}
    # de-dup sources, preserve order
    seen, ordered = set(), []
    for s in sources:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return AnalystResult(verdict=verdict, sources_used=ordered,
                         model=config.analyst_model)


def analyse(filing: dict) -> AnalystResult:
    """Synchronous entry point. `filing` needs filing_id, cik, form_type,
    filing_date, and optionally company/ticker."""
    if not config.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set (add it to .env)")
    os.environ.setdefault("ANTHROPIC_API_KEY", config.anthropic_api_key)
    return asyncio.run(_run(filing))

"""Rate-limited HTTP client for SEC EDGAR.

SEC's fair-access policy requires a descriptive User-Agent and a cap of
10 requests/second. This client enforces a conservative throttle and retries
on HTTP 429 / 5xx with backoff.
"""

from __future__ import annotations

import time

import httpx

from ..config import config

# Stay comfortably under SEC's 10 req/s ceiling.
_MIN_INTERVAL_S = 0.15
_MAX_RETRIES = 4


class EdgarClient:
    """Thin wrapper around httpx.Client with throttling and retry/backoff."""

    def __init__(self, user_agent: str | None = None) -> None:
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent or config.user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=30.0,
            follow_redirects=True,
        )
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _MIN_INTERVAL_S:
            time.sleep(_MIN_INTERVAL_S - elapsed)
        self._last_request_at = time.monotonic()

    def get(self, url: str) -> httpx.Response:
        """GET with throttle + retry. Raises for non-recoverable HTTP errors."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            self._throttle()
            try:
                resp = self._client.get(url)
            except httpx.TransportError as exc:  # network blip
                last_exc = exc
                time.sleep(2**attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                # Honor Retry-After when present, else exponential backoff.
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 2**attempt
                time.sleep(delay)
                continue

            resp.raise_for_status()
            return resp

        if last_exc:
            raise last_exc
        raise RuntimeError(f"Exhausted retries fetching {url}")

    def get_json(self, url: str) -> dict:
        return self.get(url).json()

    def get_bytes(self, url: str) -> bytes:
        return self.get(url).content

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "EdgarClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

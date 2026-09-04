"""Small shared HTTP wrapper with retries."""
from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

_UA = {"User-Agent": "poly-weather-bot/1.0", "Accept": "application/json"}


class Http:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self._c = httpx.Client(base_url=base_url, timeout=timeout, headers=_UA)

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.6, max=8),
        retry=retry_if_exception_type((httpx.HTTPError,)),
        reraise=True,
    )
    def get(self, path: str, **params: Any):
        params = {k: v for k, v in params.items() if v is not None}
        r = self._c.get(path, params=params)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._c.close()

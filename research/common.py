"""Shared HTTP helpers for the Polymarket research scripts."""
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0 (research)", "Accept": "application/json"}

# Gamma/data-api rate-limit if hammered; keep concurrent sockets bounded.
_throttle = threading.Semaphore(16)


def get(url, tries=5, timeout=60):
    """GET JSON. Returns None when the API says there is nothing to fetch."""
    for i in range(tries):
        try:
            with _throttle:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.load(r)
        except urllib.error.HTTPError as e:
            # 400/422 = the API refusing a deep offset or an odd wallet;
            # 404 = nothing there. All mean "no more data", not a failure.
            if e.code in (400, 404, 422):
                return None
            if e.code == 429:
                time.sleep(2.0 * (i + 1))
                continue
            if i == tries - 1:
                raise
            time.sleep(1.0 * (i + 1))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.0 * (i + 1))
    return None


def pmap(fn, items, workers=10):
    """Parallel map that never lets one failure kill the batch."""
    def safe(x):
        try:
            return fn(x)
        except Exception as e:  # noqa: BLE001
            print(f"  !! {type(e).__name__}: {e}", flush=True)
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(safe, items))

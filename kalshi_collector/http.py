"""HTTP with retry/backoff. A missed snapshot is a permanently lost row."""
import logging
import random
import time

import requests

from .config import HTTP_TIMEOUT, MAX_RETRIES

log = logging.getLogger(__name__)


class FetchError(Exception):
    pass


def get_json(url, params=None, retries=MAX_RETRIES, timeout=HTTP_TIMEOUT):
    last = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            # 4xx other than 429 will not fix themselves; fail fast.
            if 400 <= r.status_code < 500 and r.status_code != 429:
                raise FetchError(f"{r.status_code} {url}")
            last = FetchError(f"{r.status_code} {url}")
        except FetchError:
            raise
        except Exception as exc:  # network, timeout, malformed JSON
            last = exc
        sleep = min(30.0, (2 ** attempt)) + random.uniform(0, 0.5)
        log.warning("retry %d/%d in %.1fs: %s", attempt + 1, retries, sleep, last)
        time.sleep(sleep)
    raise FetchError(f"exhausted retries for {url}: {last}")

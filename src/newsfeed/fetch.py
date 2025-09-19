import asyncio, random, time, re
from typing import Optional, Tuple
import httpx

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Connection": "keep-alive",
    "Referer": "https://www.google.com/",
}

class Fetcher:
    def __init__(self, timeout: int = 30, max_retries: int = 4, min_delay: float = 0.6, max_delay: float = 1.2):
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.client = httpx.Client(timeout=timeout, follow_redirects=True, headers=DEFAULT_HEADERS, http2=True)

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass

    def _sleep_jitter(self, base: float):
        # джиттер для 429/5xx
        time.sleep(base + random.random() * base)

    def get(self, url: str) -> Tuple[Optional[httpx.Response], Optional[str]]:
        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.client.get(url)
                if resp.status_code == 429:
                    # экспоненциальная пауза
                    self._sleep_jitter(min(8, 1.5 ** attempt))
                    continue
                if resp.status_code in (401, 403):
                    # попробовать один раз со слегка иным заголовком
                    if attempt == 1:
                        self.client.headers["User-Agent"] = DEFAULT_HEADERS["User-Agent"].replace("Chrome/124.0", "Chrome/126.0")
                        self.client.headers["Referer"] = url
                        self._sleep_jitter(1.5)
                        continue
                resp.raise_for_status()
                # базовая пауза между запросами
                time.sleep(random.uniform(self.min_delay, self.max_delay))
                # контент
                return resp, None
            except Exception as e:
                last_err = str(e)
                self._sleep_jitter(min(5, 1.5 ** attempt))
                continue
        return None, last_err

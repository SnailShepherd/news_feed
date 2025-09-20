import time, random, logging, requests
from urllib.parse import urlparse
logger = logging.getLogger("http")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru,en;q=0.9",
    "Connection": "keep-alive",
}

class HttpClient:
    def __init__(self, min_delay=1.0, per_domain_delay=None, timeout=30):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.min_delay = float(min_delay)
        self.per_domain_delay = per_domain_delay or {}
        self.timeout = timeout
        self._last = {}

    def _throttle(self, url):
        host = urlparse(url).netloc
        delay = self.per_domain_delay.get(host, self.min_delay)
        t_prev = self._last.get(host, 0.0)
        now = time.monotonic()
        to_wait = t_prev + delay - now
        if to_wait > 0:
            time.sleep(to_wait)
        self._last[host] = time.monotonic()

    def get(self, url, allow_redirects=True):
        self._throttle(url)
        tries = 0
        while True:
            tries += 1
            try:
                resp = self.session.get(url, allow_redirects=allow_redirects, timeout=self.timeout)
            except requests.RequestException:
                if tries < 3:
                    time.sleep(2 ** tries + random.random())
                    continue
                raise
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if tries < 4:
                    ra = resp.headers.get("Retry-After")
                    to_wait = float(ra) if (ra and ra.isdigit()) else (2 ** tries + random.uniform(0, 1.5))
                    logger.warning("HTTP %s for %s; backing off %.1fs", resp.status_code, url, to_wait)
                    time.sleep(to_wait)
                    continue
            resp.raise_for_status()
            return resp

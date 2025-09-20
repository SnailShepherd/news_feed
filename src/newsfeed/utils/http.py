import time
import logging
import requests
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

class RateLimiter:
    def __init__(self, default_delay=2.0):
        self.default_delay = default_delay
        self._last = {}
        self._per_domain = {}

    def set_delay(self, domain, seconds):
        self._per_domain[domain] = seconds

    def wait(self, url):
        dom = urlparse(url).netloc.lower()
        delay = self._per_domain.get(dom, self.default_delay)
        last = self._last.get(dom, 0.0)
        now = time.time()
        to_wait = last + delay - now
        if to_wait > 0:
            time.sleep(to_wait)
        self._last[dom] = time.time()

def build_session():
    sess = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 newsfeed/0.1",
        "Accept-Language": "ru,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return sess

"""A polite HTTP client.

* identifies itself with an honest User-Agent (configure a contact email)
* obeys robots.txt for website crawling
* rate-limits per host
* caches responses in SQLite so re-runs don't re-hit sites or re-bill APIs
* retries 429 / 5xx with backoff
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from .config import Config


@dataclass
class HttpResult:
    url: str
    status: int
    text: str
    headers: dict
    from_cache: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    def json(self):
        return json.loads(self.text)


class RobotsDisallowed(Exception):
    pass


class HttpClient:
    def __init__(self, cfg: Config, cache_path: str | Path | None = None, offline: bool = False):
        self.user_agent = cfg.get("http.user_agent", "beauty-leads/0.1")
        self.timeout = float(cfg.get("http.timeout_seconds", 15))
        self.delay = float(cfg.get("http.per_host_delay_seconds", 1.5))
        self.respect_robots = bool(cfg.get("http.respect_robots_txt", True))
        self.max_bytes = int(cfg.get("http.max_page_bytes", 2_000_000))
        self.max_api_bytes = int(cfg.get("http.max_api_bytes", 300_000_000))
        self.ttl = float(cfg.get("http.cache_ttl_hours", 168)) * 3600
        self.offline = offline

        self._local = threading.local()
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._meta_lock = threading.Lock()
        self._robots: dict[str, RobotFileParser | None] = {}

        path = Path(cache_path or cfg.path("http_cache"))
        path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_lock = threading.Lock()
        self._cache = sqlite3.connect(str(path), check_same_thread=False)
        self._cache.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, url TEXT, status INTEGER,"
            " headers TEXT, body TEXT, fetched_at REAL)"
        )

    # --- internals -----------------------------------------------------------
    def _session(self) -> requests.Session:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({"User-Agent": self.user_agent, "Accept-Language": "en-CA,en;q=0.8"})
            self._local.session = sess
        return sess

    def _wait_for_host(self, host: str) -> threading.Lock:
        with self._meta_lock:
            lock = self._host_locks.setdefault(host, threading.Lock())
        lock.acquire()
        last = self._host_last.get(host, 0.0)
        wait = self.delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        return lock

    def _release_host(self, host: str, lock: threading.Lock) -> None:
        self._host_last[host] = time.monotonic()
        lock.release()

    @staticmethod
    def _cache_key(method: str, url: str, body: str | None, headers: dict | None) -> str:
        # Don't put secrets (API keys) in the key material in plain text.
        relevant = {k: v for k, v in (headers or {}).items() if k.lower() in ("x-goog-fieldmask",)}
        raw = json.dumps([method, url, body, relevant], sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _cache_get(self, key: str) -> HttpResult | None:
        with self._cache_lock:
            row = self._cache.execute(
                "SELECT url, status, headers, body, fetched_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        url, status, headers, body, fetched_at = row
        if not self.offline and time.time() - fetched_at > self.ttl:
            return None
        return HttpResult(url=url, status=status, text=body, headers=json.loads(headers), from_cache=True)

    def _cache_put(self, key: str, result: HttpResult) -> None:
        with self._cache_lock:
            self._cache.execute(
                "INSERT OR REPLACE INTO cache (key, url, status, headers, body, fetched_at) VALUES (?,?,?,?,?,?)",
                (key, result.url, result.status, json.dumps(result.headers), result.text, time.time()),
            )
            self._cache.commit()

    def allowed_by_robots(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        with self._meta_lock:
            known = base in self._robots
            parser = self._robots.get(base)
        if not known:
            parser = None
            res = self._fetch("GET", base + "/robots.txt", use_cache=True, check_robots=False, retries=0)
            if res.ok and res.text:
                parser = RobotFileParser()
                parser.parse(res.text.splitlines())
            elif res.status in (401, 403):
                # Whole site closed to bots.
                parser = RobotFileParser()
                parser.parse(["User-agent: *", "Disallow: /"])
            with self._meta_lock:
                self._robots[base] = parser
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    def _fetch(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
        json_body: dict | None = None,
        use_cache: bool = True,
        check_robots: bool = False,
        retries: int = 2,
    ) -> HttpResult:
        if params:
            req = requests.Request(method, url, params=params).prepare()
            url = req.url or url
        body = json.dumps(json_body, sort_keys=True) if json_body is not None else None
        key = self._cache_key(method, url, body, headers)
        if use_cache:
            cached = self._cache_get(key)
            if cached:
                return cached
        if self.offline:
            return HttpResult(url=url, status=0, text="", headers={}, error="offline (not in cache)")
        if check_robots and not self.allowed_by_robots(url):
            return HttpResult(url=url, status=0, text="", headers={}, error="blocked by robots.txt")
        # Crawled websites get a small cap; API responses (JSON) must not be truncated.
        limit = self.max_bytes if check_robots else self.max_api_bytes

        host = urlsplit(url).netloc.lower()
        attempt = 0
        while True:
            lock = self._wait_for_host(host)
            try:
                resp = self._session().request(
                    method, url, headers=headers, data=body,
                    timeout=self.timeout, stream=True, allow_redirects=True,
                )
                chunks, size = [], 0
                for chunk in resp.iter_content(chunk_size=65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= limit:
                        break
                resp.close()
                encoding = resp.encoding or "utf-8"
                if (resp.headers.get("content-type") or "").startswith("text/html") and resp.encoding == "ISO-8859-1":
                    encoding = resp.apparent_encoding or "utf-8"
                text = b"".join(chunks).decode(encoding, errors="replace")
                result = HttpResult(url=resp.url, status=resp.status_code, text=text,
                                    headers={k.lower(): v for k, v in resp.headers.items()})
            except requests.RequestException as exc:
                result = HttpResult(url=url, status=0, text="", headers={}, error=f"{type(exc).__name__}: {exc}")
            finally:
                self._release_host(host, lock)

            retryable = result.status == 429 or 500 <= result.status < 600
            if retryable and attempt < retries:
                attempt += 1
                retry_after = result.headers.get("retry-after", "")
                time.sleep(float(retry_after) if retry_after.isdigit() else 2 ** attempt * 2)
                continue
            break

        if use_cache and result.error is None and (result.ok or result.status in (404, 410)):
            self._cache_put(key, result)
        return result

    # --- public API ----------------------------------------------------------
    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None,
            crawl: bool = False, use_cache: bool = True) -> HttpResult:
        """GET a URL. Set crawl=True for third-party websites (enforces robots.txt)."""
        return self._fetch("GET", url, params=params, headers=headers, use_cache=use_cache, check_robots=crawl)

    def post_json(self, url: str, body: dict, *, headers: dict | None = None, use_cache: bool = True) -> HttpResult:
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        return self._fetch("POST", url, headers=hdrs, json_body=body, use_cache=use_cache)

    def close(self) -> None:
        with self._cache_lock:
            self._cache.close()

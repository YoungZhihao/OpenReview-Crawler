"""OpenReview API client.

Standard library only (urllib) so the crawler runs on a bare Python install.

Design notes
------------
* Talks to ``api2.openreview.net``. The old ``api1`` host no longer resolves,
  and api2 serves every venue, including the ones whose data is still stored in
  the v1 *shape* (raw content values instead of ``{"value": ...}`` wrappers).
  Callers should therefore never assume either shape -- use
  :func:`unwrap` / :func:`unwrap_content` instead of poking at ``content``.
* Anonymous read access is enough for public venues (ICLR, NeurIPS, ICML, ...).
  A bearer token is optional and only widens access to private venues and
  raises rate limits; it is never required for the common case.
* Every GET is cached on disk keyed by the full request URL (token excluded, so
  a token change does not invalidate the cache). Re-runs are then instant and
  offline-capable, which matters because a single ICLR paper thread can be a
  handful of paginated requests and a batch run is hundreds.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Iterable

API2 = "https://api2.openreview.net"
WEB = "https://openreview.net"

DEFAULT_TIMEOUT = 40
DEFAULT_RETRIES = 4
MAX_LIMIT = 1000


class OpenReviewError(RuntimeError):
    """Raised for unrecoverable API failures."""


class NotFound(OpenReviewError):
    """Raised when the API answers 404 -- usually a bad id or a private venue."""


class TokenExpired(OpenReviewError):
    """Raised when the API rejects our bearer token.

    Distinguished from a plain auth failure so callers can react by signing in
    again and retrying, rather than reporting the paper as unfetchable. A
    token can also be rejected before its ``exp`` claim -- for instance after
    signing out elsewhere -- which is why this is detected from the response
    and not only from the local expiry check.
    """


class ChallengeRequired(OpenReviewError):
    """Raised when OpenReview answers with a bot-check instead of data.

    OpenReview fronts api2 with a challenge that fires probabilistically once
    your IP looks busy. It is transient -- the same request usually succeeds a
    few seconds later -- so it is retried, and only surfaced if it keeps
    happening. It is emphatically *not* a permissions error, which is what a
    bare 403 looks like.
    """

    def __init__(self, url: str, challenge_url: str | None = None,
                 req_id: str | None = None) -> None:
        self.url = url
        self.challenge_url = challenge_url
        self.req_id = req_id
        message = (
            "OpenReview 要求通过人机校验（ChallengeRequiredError），这通常是"
            "短时间内请求过多导致的，不是你账号或权限的问题。\n"
            "  处理办法（任选其一）：\n"
            "    1. 等几分钟再跑一次 —— 已有结果都在缓存里，不必从头抓；\n"
            "    2. 用 --token 带一个登录令牌，认证请求一般不触发校验；\n"
            "    3. 在浏览器里打开下面这个链接完成校验，再把浏览器的 Cookie "
            "复制给 --cookie。"
        )
        if challenge_url:
            message += f"\n  校验链接: {challenge_url}"
        if req_id:
            message += f"\n  请求编号: {req_id} (reqId)"
        super().__init__(message)


# --------------------------------------------------------------------------- #
# value unwrapping
# --------------------------------------------------------------------------- #
_UNWRAPPED_KEYS = ("value", "values")


def unwrap(field: Any) -> Any:
    """Collapse an api2 ``{"value": X}`` (or ``{"values": [...]}``) box.

    v1-shaped notes store bare values, so a bare value passes straight through.
    """
    if isinstance(field, dict):
        if "value" in field:
            return unwrap(field["value"])
        if "values" in field:
            return [unwrap(v) for v in field["values"]]
    return field


def unwrap_content(content: Any) -> dict[str, Any]:
    """Unwrap every field of a note ``content`` mapping."""
    if not isinstance(content, dict):
        return {}
    return {key: unwrap(val) for key, val in content.items()}


def as_text(value: Any) -> str:
    """Render an arbitrary field value as readable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        parts = [as_text(v) for v in value]
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        # Unwrap first; anything left is a genuinely nested record.
        if "value" in value or "values" in value:
            return as_text(unwrap(value))
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return str(value)


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class OpenReviewClient:
    """Minimal, polite, cached OpenReview REST client."""

    def __init__(
        self,
        base: str = API2,
        token: str | None = None,
        cookie: str | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        cache_ttl: float | None = None,
        offline: bool = False,
        refresh: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        min_interval: float = 0.35,
        verbose: bool = False,
    ) -> None:
        self.base = base.rstrip("/")
        self.token = token or None
        self.cookie = cookie or None
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl
        self.offline = offline
        self.refresh = refresh
        self.timeout = timeout
        self.retries = retries
        # Deliberately unhurried: OpenReview starts issuing human-check
        # challenges to IPs that hammer the API, and a slower crawl that
        # completes beats a fast one that gets throttled.
        self.min_interval = min_interval
        self.verbose = verbose
        self._last_request = 0.0
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0, "challenges": 0}
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- logging ---------------------------------------------------------- #
    def log(self, message: str) -> None:
        if self.verbose:
            print(f"  · {message}", flush=True)

    # -- cache ------------------------------------------------------------ #
    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        # A readable prefix makes the cache directory browsable by hand.
        hint = urllib.parse.urlparse(url).path.strip("/").replace("/", "_")[:40]
        return self.cache_dir / f"{hint or 'root'}.{digest}.json"

    def _cache_read(self, url: str) -> Any | None:
        path = self._cache_path(url)
        if not path or not path.exists():
            return None
        if self.cache_ttl is not None:
            age = time.time() - path.stat().st_mtime
            if age > self.cache_ttl:
                return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return None
        return payload.get("data")

    def _cache_write(self, url: str, data: Any) -> None:
        path = self._cache_path(url)
        if not path:
            return
        # Never persist an empty listing. `/notes/search` intermittently
        # answers "no results" for a query it just answered, and a cached empty
        # would make that hiccup permanent for every later run.
        if isinstance(data, dict) and data.get("notes") == []:
            self.log(f"not caching empty response: {url}")
            return
        tmp = path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump({"url": url, "fetched_at": time.time(), "data": data}, fh,
                          ensure_ascii=False)
            tmp.replace(path)
        except OSError:
            pass

    # -- transport -------------------------------------------------------- #
    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        wait = self.min_interval - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _raw_get(self, url: str) -> Any:
        headers = {
            "User-Agent": "openreview-crawler/1.0 (stdlib urllib)",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.cookie:
            headers["Cookie"] = self.cookie

        request = urllib.request.Request(url, headers=headers)
        self._throttle()
        last_error: Exception | None = None
        challenge: ChallengeRequired | None = None
        challenge_hits = 0

        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    encoding = (response.headers.get("Content-Encoding") or "").lower()
                    if "gzip" in encoding:
                        raw = gzip.decompress(raw)
                    elif "deflate" in encoding:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                    self._last_request = time.time()
                    self.stats["requests"] += 1
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                self._last_request = time.time()
                body = self._error_body(exc)

                if exc.code == 404:
                    raise NotFound(url) from exc

                # The challenge is probabilistic, so a plain retry usually gets
                # through -- give it a longer, jittered backoff than other errors.
                if body.get("name") == "ChallengeRequiredError" or (
                    exc.code == 403 and "challenge" in json.dumps(body).lower()
                ):
                    details = body.get("details") or {}
                    challenge = ChallengeRequired(
                        url,
                        challenge_url=details.get("challengeUrl"),
                        req_id=details.get("reqId"),
                    )
                    challenge_hits += 1
                    self.stats["challenges"] = self.stats.get("challenges", 0) + 1
                    if attempt < self.retries:
                        backoff = min(45.0, 2.5 * (2 ** attempt)) + random.random() * 2
                        self.log(
                            f"human-check challenge (hit {challenge_hits}), "
                            f"waiting {backoff:.0f}s before retry"
                        )
                        time.sleep(backoff)
                        continue
                    self.stats["errors"] += 1
                    raise challenge from exc

                if exc.code in (401, 403):
                    message = body.get("message") or f"HTTP {exc.code}"
                    lowered = message.lower()
                    token_problem = exc.code == 401 or (
                        bool(self.token)
                        and "token" in lowered
                        and any(word in lowered
                                for word in ("expire", "invalid", "not valid", "revoked"))
                    )
                    if token_problem:
                        self.stats["errors"] += 1
                        raise TokenExpired(
                            f"{message}\n  URL: {url}\n"
                            "  令牌已失效，重新登录一次即可继续"
                            "（python crawl.py --login，或直接重跑本命令会提示输入密码）。"
                        ) from exc
                    if not self.token:
                        message += (
                            "\n  Hint: this venue may be private or its reviews "
                            "may not be public yet. Retry with --token if you have access."
                        )
                    self.stats["errors"] += 1
                    raise OpenReviewError(f"{message}\n  URL: {url}") from exc
                if exc.code == 429 or 500 <= exc.code < 600:
                    last_error = exc
                    if attempt < self.retries:
                        backoff = min(30.0, 1.5 ** attempt) + random.random()
                        self.log(f"HTTP {exc.code}, retry in {backoff:.1f}s ({url})")
                        time.sleep(backoff)
                        continue
                self.stats["errors"] += 1
                message = body.get("message") or str(exc)
                raise OpenReviewError(f"HTTP {exc.code}: {message}\n  URL: {url}") from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                self._last_request = time.time()
                last_error = exc
                if attempt < self.retries:
                    backoff = min(30.0, 1.5 ** attempt) + random.random()
                    self.log(f"{type(exc).__name__}, retry in {backoff:.1f}s ({url})")
                    time.sleep(backoff)
                    continue
        self.stats["errors"] += 1
        raise OpenReviewError(f"request failed for {url}: {last_error}")

    @staticmethod
    def _error_body(exc: urllib.error.HTTPError) -> dict:
        """Read the JSON error body so the real reason can be reported.

        A bare ``HTTP 403`` hides whether the cause is a private venue, a
        rejected token, or a bot challenge -- three problems with three
        completely different fixes.
        """
        try:
            raw = exc.read()
            if not raw:
                return {}
            encoding = (exc.headers.get("Content-Encoding") or "").lower() if exc.headers else ""
            if "gzip" in encoding:
                raw = gzip.decompress(raw)
            payload = json.loads(raw.decode("utf-8", "replace"))
            return payload if isinstance(payload, dict) else {"message": str(payload)}
        except (OSError, ValueError, AttributeError):
            return {}

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a JSON endpoint, through the disk cache when possible."""
        query = urllib.parse.urlencode(
            {k: v for k, v in (params or {}).items() if v is not None}, doseq=True
        )
        url = f"{self.base}{path}" + (f"?{query}" if query else "")

        if not self.refresh:
            cached = self._cache_read(url)
            if cached is not None:
                self.stats["cache_hits"] += 1
                return cached
        if self.offline:
            raise OpenReviewError(
                f"offline mode: no cached response for {url}\n"
                "Run once without --offline to populate the cache."
            )

        data = self._raw_get(url)
        self._cache_write(url, data)
        return data

    # -- endpoints -------------------------------------------------------- #
    def notes(self, **params: Any) -> list[dict]:
        """Fetch notes, following pagination to the end."""
        limit = int(params.pop("limit", 1000))
        limit = max(1, min(limit, MAX_LIMIT))
        hard_cap = int(params.pop("max_notes", 20000))
        offset = int(params.pop("offset", 0))

        collected: list[dict] = []
        while True:
            page = self.get_json("/notes", {**params, "limit": limit, "offset": offset})
            batch = page.get("notes") or []
            collected.extend(batch)
            total = page.get("count")
            offset += len(batch)
            if not batch or len(batch) < limit:
                break
            if total is not None and offset >= int(total):
                break
            if len(collected) >= hard_cap:
                self.log(f"stopped at max_notes={hard_cap}")
                break
        return collected

    def note(self, note_id: str) -> dict | None:
        data = self.get_json("/notes", {"id": note_id})
        notes = data.get("notes") or []
        return notes[0] if notes else None

    def forum_notes(self, forum_id: str, details: str | None = None) -> list[dict]:
        """Every note in a forum thread (the submission itself included)."""
        params: dict[str, Any] = {"forum": forum_id, "trash": "false"}
        if details:
            params["details"] = details
        return self.notes(**params)

    def search_notes(self, term: str, limit: int = 30, **extra: Any) -> list[dict]:
        """Full-text search over notes.

        The ``/notes/search`` endpoint is noticeably flaky: the same call can
        return results and then an empty list moments later, and ``source=forum``
        in particular comes and goes. Callers are expected to combine several
        parameter sets and retry -- see :func:`orcr.resolve.search_papers`.
        """
        params: dict[str, Any] = {"term": term, "limit": limit}
        params.update({k: v for k, v in extra.items() if v is not None})
        data = self.get_json("/notes/search", params)
        return data.get("notes") or []

    def group(self, group_id: str) -> dict | None:
        data = self.get_json("/groups", {"id": group_id})
        groups = data.get("groups") or []
        return groups[0] if groups else None

    def profiles(self, ids: Iterable[str]) -> list[dict]:
        ids = [i for i in ids if i]
        if not ids:
            return []
        data = self.get_json("/profiles", {"ids": ",".join(ids)})
        return data.get("profiles") or []

    def invitation(self, invitation_id: str) -> dict | None:
        data = self.get_json("/invitations", {"id": invitation_id})
        items = data.get("invitations") or []
        return items[0] if items else None

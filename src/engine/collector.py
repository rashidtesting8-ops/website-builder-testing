"""Evidence-preserving website asset/content collector for small business previews.

Public website pages only; no authentication, platform scraping, or copyrighted
asset licensing claims. Designed for locally controlled sites / authorized reviews.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import mimetypes
import re
import socket
import time
from collections import deque
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Comment, Tag
from PIL import Image, UnidentifiedImageError
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util.connection import allowed_gai_family

AGENT = "PetSiteResearchBot"
USER_AGENT = "PetSiteResearchBot/0.2 (+respectful-preview-audit)"
DEFAULT_MAX_MEDIA = 80
MAX_SITE_BYTES = 140 * 1024 * 1024
MAX_REDIRECT_HOPS = 6
MEDIA_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".avif",
    ".gif",
    ".svg",
    ".mp4",
    ".webm",
    ".mov",
}
MEDIA_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}
BAD_PAGE_HINTS = ("logout", "cart", "checkout", "admin", "wp-login", "/feed")
PAGE_HINTS = (
    "gallery",
    "photo",
    "portfolio",
    "about",
    "service",
    "price",
    "review",
    "faq",
    "contact",
    "policy",
    "book",
)
_NUMERIC_OR_HEX_LABEL_RE = re.compile(r"^(?:0x[0-9a-f]+|\d+)$", re.I)

AddrInfoRecord = tuple[int, int, int, str, tuple[Any, ...]]
ResolverFn = Callable[[str, int], Sequence[AddrInfoRecord]]
ConnectorFn = Callable[[tuple[str, int], float | None, tuple[str, int] | None], socket.socket]


def now() -> str:
    return datetime.now(UTC).isoformat()


def slugify(value: str) -> str:
    val = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return val[:65] or "website"


def validate_ip_address(raw_ip: str, *, allow_private: bool = False) -> str:
    """Validate an IP string, unwrapping IPv4-mapped IPv6 and rejecting non-global addresses."""
    cleaned = raw_ip.strip().strip("[]").split("%")[0]
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError as exc:
        raise ValueError(f"Invalid IP address: {raw_ip!r}") from exc

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if not allow_private and (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise ValueError(f"Non-public address blocked: {raw_ip}")

    return str(ip)


def _validate_hostname(hostname: str, *, allow_private: bool = False) -> str:
    host = hostname.strip().strip("[]")
    if not host:
        raise ValueError("Empty hostname is not allowed")

    if ":" in host:
        # IPv6 literal
        return validate_ip_address(host, allow_private=allow_private)

    labels = host.rstrip(".").split(".")
    if any(not lbl for lbl in labels):
        raise ValueError(f"Invalid hostname: {hostname!r}")

    # If the final label (or every label) is numeric or hex, it must be a canonical
    # 4-octet decimal IPv4 address (no octal leading zeros, no hex, no 32-bit integer form).
    if all(_NUMERIC_OR_HEX_LABEL_RE.match(lbl) for lbl in labels) or _NUMERIC_OR_HEX_LABEL_RE.match(
        labels[-1]
    ):
        try:
            ipv4 = ipaddress.IPv4Address(host)
        except ipaddress.AddressValueError as exc:
            raise ValueError(f"Encoded or non-canonical IP literal blocked: {hostname}") from exc
        return validate_ip_address(str(ipv4), allow_private=allow_private)

    return host


def safe_url(url: str, *, allow_private: bool = False) -> str:
    """Early syntactic URL checks: http/https scheme, standard ports 80/443, no userinfo."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Only absolute HTTP(S) URLs are allowed")
    if p.username or p.password:
        raise ValueError("Embedded credentials are not permitted")
    try:
        port = p.port
    except ValueError as exc:
        raise ValueError("Invalid URL port") from exc
    if not allow_private and port not in (None, 80, 443):
        raise ValueError("Only standard HTTP/HTTPS ports (80/443) are allowed")
    _validate_hostname(p.hostname, allow_private=allow_private)
    return url


def _default_resolver(host: str, port: int) -> Sequence[AddrInfoRecord]:
    family = allowed_gai_family()
    return socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)


def _default_connector(
    addr: tuple[str, int],
    timeout: float | None,
    source_address: tuple[str, int] | None,
) -> socket.socket:
    if timeout is not None and not isinstance(timeout, int | float):
        timeout = None
    return socket.create_connection(addr, timeout=timeout, source_address=source_address)


def dial_verified_socket(
    host: str,
    port: int,
    *,
    timeout: float | None = None,
    source_address: tuple[str, int] | None = None,
    socket_options: Sequence[tuple[int, int, int | bytes]] | None = None,
    allow_private: bool = False,
    resolver: ResolverFn | None = None,
    connector: ConnectorFn | None = None,
) -> socket.socket:
    """Single dialer: resolve once -> validate ALL resolved IPs -> connect to validated IP."""
    if not allow_private and port not in (80, 443):
        raise ValueError(f"Non-standard port blocked: {port}")
    clean_host = host.strip().strip("[]")
    _validate_hostname(clean_host, allow_private=allow_private)

    active_resolver = resolver or _default_resolver
    active_connector = connector or _default_connector

    records = list(active_resolver(clean_host, port))
    if not records:
        raise ValueError(f"DNS resolution returned no addresses for {clean_host!r}")

    # Validate EVERY resolved address before attempting any connection.
    validated_targets: list[tuple[str, int]] = []
    for entry in records:
        sockaddr = entry[4]
        raw_ip = str(sockaddr[0])
        target_port = int(sockaddr[1]) if len(sockaddr) > 1 else port
        canonical_ip = validate_ip_address(raw_ip, allow_private=allow_private)
        validated_targets.append((canonical_ip, target_port))

    last_err: Exception | None = None
    for target in validated_targets:
        sock: socket.socket | None = None
        try:
            sock = active_connector(target, timeout, source_address)
            if socket_options:
                for opt in socket_options:
                    if opt[0] == socket.IPPROTO_TCP and sock.family not in (
                        socket.AF_INET,
                        socket.AF_INET6,
                    ):
                        continue
                    sock.setsockopt(*opt)
            return sock
        except Exception as exc:
            if sock is not None:
                sock.close()
            last_err = exc

    if last_err is not None:
        raise last_err
    raise ValueError(f"Failed to connect to {clean_host!r}")


class SafeHTTPAdapter(HTTPAdapter):
    """Scoped HTTPAdapter mounting custom connection pools that use dial_verified_socket."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        resolver: ResolverFn | None = None,
        connector: ConnectorFn | None = None,
        **kwargs: Any,
    ) -> None:
        self._allow_private = allow_private
        self._resolver = resolver
        self._connector = connector
        super().__init__(**kwargs)

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: Any,
    ) -> None:
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
        allow_private = self._allow_private
        resolver = self._resolver
        connector = self._connector

        class _ScopedHTTPConnection(HTTPConnection):
            def _new_conn(self) -> socket.socket:
                timeout_val = float(self.timeout) if isinstance(self.timeout, int | float) else None
                return dial_verified_socket(
                    self._dns_host,
                    int(self.port),
                    timeout=timeout_val,
                    source_address=self.source_address,
                    socket_options=self.socket_options,
                    allow_private=allow_private,
                    resolver=resolver,
                    connector=connector,
                )

        class _ScopedHTTPSConnection(HTTPSConnection):
            def _new_conn(self) -> socket.socket:
                timeout_val = float(self.timeout) if isinstance(self.timeout, int | float) else None
                return dial_verified_socket(
                    self._dns_host,
                    int(self.port),
                    timeout=timeout_val,
                    source_address=self.source_address,
                    socket_options=self.socket_options,
                    allow_private=allow_private,
                    resolver=resolver,
                    connector=connector,
                )

        class _ScopedHTTPConnectionPool(HTTPConnectionPool):
            ConnectionCls = _ScopedHTTPConnection  # type: ignore[assignment]

        class _ScopedHTTPSConnectionPool(HTTPSConnectionPool):
            ConnectionCls = _ScopedHTTPSConnection  # type: ignore[assignment]

        self.poolmanager.pool_classes_by_scheme = {
            "http": _ScopedHTTPConnectionPool,
            "https": _ScopedHTTPSConnectionPool,
        }


def build_collector_session(
    *,
    allow_private: bool = False,
    resolver: ResolverFn | None = None,
    connector: ConnectorFn | None = None,
    user_agent: str = USER_AGENT,
) -> requests.Session:
    """Create a hardened collector Session with trust_env=False and SafeHTTPAdapter."""
    sess = requests.Session()
    sess.trust_env = False
    sess.proxies = {}
    sess.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,image/*;q=0.9,*/*;q=0.8",
        }
    )
    adapter = SafeHTTPAdapter(
        allow_private=allow_private,
        resolver=resolver,
        connector=connector,
    )
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    return sess


def _ensure_safe_session(
    session: requests.Session,
    *,
    allow_private: bool = False,
    resolver: ResolverFn | None = None,
    connector: ConnectorFn | None = None,
) -> None:
    session.trust_env = False
    session.proxies = {}
    existing = session.get_adapter("http://")
    needs_mount = (
        not isinstance(existing, SafeHTTPAdapter)
        or existing._allow_private != allow_private
        or (resolver is not None and existing._resolver is not resolver)
        or (connector is not None and existing._connector is not connector)
    )
    if needs_mount:
        adapter = SafeHTTPAdapter(
            allow_private=allow_private,
            resolver=resolver if resolver is not None else getattr(existing, "_resolver", None),
            connector=connector if connector is not None else getattr(existing, "_connector", None),
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)


def response_get(
    session: requests.Session,
    url: str,
    *,
    allow_private: bool = False,
    stream: bool = False,
    resolver: ResolverFn | None = None,
    connector: ConnectorFn | None = None,
) -> requests.Response:
    """Fetch URL with manual redirect following and full SSRF/rebinding checks on every hop."""
    _ensure_safe_session(
        session,
        allow_private=allow_private,
        resolver=resolver,
        connector=connector,
    )
    current_url = url
    for _ in range(MAX_REDIRECT_HOPS):
        safe_url(current_url, allow_private=allow_private)
        response = session.get(
            current_url,
            timeout=(7, 18),
            stream=stream,
            allow_redirects=False,
            proxies={},
        )
        if response.status_code in (301, 302, 303, 307, 308):
            nxt = response.headers.get("Location")
            response.close()
            if not nxt:
                raise RuntimeError("Redirect without Location")
            current_url = urljoin(current_url, nxt)
            continue
        return response
    raise RuntimeError("Too many redirects")


def normalize_page_url(url: str) -> str:
    return urldefrag(url)[0].rstrip("/") or url


def same_site(a: str, b: str) -> bool:
    ah = (urlparse(a).hostname or "").lower().removeprefix("www.")
    bh = (urlparse(b).hostname or "").lower().removeprefix("www.")
    return ah == bh


def robots_allowed(
    session: requests.Session,
    url: str,
    cache: dict[str, RobotFileParser | bool | None],
    *,
    allow_private: bool = False,
) -> bool:
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    if origin not in cache:
        rp = RobotFileParser()
        rp.set_url(origin + "/robots.txt")
        try:
            r = response_get(
                session, origin + "/robots.txt", allow_private=allow_private, stream=True
            )
            try:
                if r.status_code in (401, 403) or r.status_code >= 500:
                    cache[origin] = False  # inaccessible / temporarily unavailable: do not crawl
                elif r.status_code in (404, 410):
                    cache[origin] = None
                elif r.ok:
                    text = bytearray()
                    for chunk in r.iter_content(65536):
                        text.extend(chunk)
                        if len(text) > 1024 * 1024:
                            raise ValueError("robots.txt too large")
                    rp.parse(text.decode("utf8", errors="replace").splitlines())
                    cache[origin] = rp
                else:
                    cache[origin] = False
            finally:
                r.close()
        except (requests.RequestException, ValueError, RuntimeError):
            cache[origin] = False
    rp_entry = cache[origin]
    if rp_entry is None:
        return True
    if isinstance(rp_entry, RobotFileParser):
        return rp_entry.can_fetch(AGENT, url)
    return False


def choose_srcset(srcset: str) -> str:
    picks: list[tuple[float, str]] = []
    pos = 0
    while pos < len(srcset):
        while pos < len(srcset) and (srcset[pos].isspace() or srcset[pos] == ","):
            pos += 1
        if pos >= len(srcset):
            break

        url_start = pos
        while pos < len(srcset) and not srcset[pos].isspace():
            pos += 1
        url = srcset[url_start:pos]
        if url.endswith(","):
            url = url.rstrip(",")
            if url:
                picks.append((1.0, url))
            continue

        descriptor = ""
        while pos < len(srcset):
            while pos < len(srcset) and srcset[pos].isspace():
                pos += 1
            if pos >= len(srcset) or srcset[pos] == ",":
                break
            descriptor_start = pos
            while pos < len(srcset) and not (
                srcset[pos].isspace() or srcset[pos] == ","
            ):
                pos += 1
            if not descriptor:
                descriptor = srcset[descriptor_start:pos]

        weight = 1.0
        if descriptor.endswith(("w", "x")):
            try:
                weight = float(descriptor[:-1])
                if descriptor.endswith("x"):
                    weight *= 1000.0
            except ValueError:
                pass
        picks.append((weight, url))
        if pos < len(srcset) and srcset[pos] == ",":
            pos += 1
    return max(picks, default=(0.0, ""))[1]


def candidate_url(raw: str, base: str) -> str | None:
    if not raw or raw.startswith(("data:", "blob:", "javascript:", "mailto:")):
        return None
    return urljoin(base, raw.strip())


def _attr_str(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(x) for x in value)
    return str(value or "")


def grab_media(soup: BeautifulSoup, page_url: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    keys = ("src", "data-src", "data-lazy-src", "data-original", "data-image", "poster")
    for tag in soup.find_all(["img", "source", "video", "a", "meta"]):
        vals: list[str] = []
        for k in keys:
            if tag.has_attr(k):
                vals.append(_attr_str(tag[k]))
        for k in ("srcset", "data-srcset"):
            if tag.has_attr(k):
                vals.append(choose_srcset(_attr_str(tag[k])))
        if (
            tag.name == "a"
            and tag.has_attr("href")
            and Path(urlparse(_attr_str(tag["href"])).path.lower()).suffix in MEDIA_EXTENSIONS
        ):
            vals.append(_attr_str(tag["href"]))
        if tag.name == "meta" and _attr_str(tag.get("property", "")).lower() in (
            "og:image",
            "og:video",
            "twitter:image",
        ):
            vals.append(_attr_str(tag.get("content", "")))
        for raw in vals:
            url = candidate_url(str(raw), page_url)
            if url:
                found.append(
                    {
                        "url": url,
                        "source_page": page_url,
                        "alt": _attr_str(tag.get("alt", "")),
                        "discovery": tag.name,
                    }
                )
    # Inline CSS assets such as branded background images.
    for tag in soup.select("[style]"):
        for raw in re.findall(r"url\(\s*['\"]?([^)'\"]+)", _attr_str(tag.get("style", "")), re.I):
            url = candidate_url(raw, page_url)
            if url:
                found.append(
                    {"url": url, "source_page": page_url, "alt": "", "discovery": "inline-css"}
                )
    return found


STRIPPED_HTML_TAGS = (
    "script",
    "style",
    "iframe",
    "noscript",
    "svg",
    "object",
    "embed",
    "template",
)

PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?", re.I),
    re.compile(r"disregard\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?", re.I),
    re.compile(r"forget\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?", re.I),
    re.compile(r"\bsystem\s+prompt\b", re.I),
    re.compile(r"<\|(?:system|im_start|im_end|endoftext)\|>", re.I),
    re.compile(r"\[(?:system|inst|/inst)\]", re.I),
    re.compile(r"#{2,}\s*(?:system|instruction|prompt)\b", re.I),
    re.compile(r"\bdeveloper\s+mode\b", re.I),
    re.compile(r"\bdo\s+anything\s+now\b", re.I),
)


def sanitize_prompt_injection(text: str) -> tuple[str, list[str]]:
    """Scan text against prompt-injection delimiters and neutralize offending lines."""
    detected: list[str] = []
    safe_lines: list[str] = []
    for raw_line in text.splitlines():
        matched_line = False
        for pattern in PROMPT_INJECTION_PATTERNS:
            match = pattern.search(raw_line)
            if match:
                detected.append(match.group(0))
                matched_line = True
        if not matched_line:
            safe_lines.append(raw_line)
    return "\n".join(safe_lines), detected


def collect_page_text(soup: BeautifulSoup, url: str) -> dict[str, Any]:
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup(list(STRIPPED_HTML_TAGS)):
        tag.decompose()
    raw_text = "\n".join(t.strip() for t in soup.stripped_strings if t.strip())
    sanitized_text, injection_hits = sanitize_prompt_injection(raw_text)
    cleaned = re.sub(r"\n{3,}", "\n\n", sanitized_text)
    return {
        "url": url,
        "text": cleaned[:90000],
        "prompt_injection_hits": injection_hits,
    }


def media_type(content_type: str, url: str) -> tuple[str | None, str | None]:
    ctype = content_type.split(";")[0].strip().lower()
    if ctype in MEDIA_TYPES:
        return ctype, MEDIA_TYPES[ctype]
    ext = Path(urlparse(url).path.lower()).suffix
    if ext in MEDIA_EXTENSIONS:
        guessed = mimetypes.guess_type("x" + ext)[0] or "application/octet-stream"
        return guessed, ext
    return None, None


def download_one(
    session: requests.Session,
    candidate: dict[str, Any],
    folder: Path,
    seen_sha: dict[str, str],
    robots_cache: dict[str, RobotFileParser | bool | None],
    *,
    allow_private: bool = False,
    remaining_bytes: int | None = None,
) -> dict[str, Any]:
    base = {
        **candidate,
        "captured_at": now(),
        "rights_confirmed_for_preview": False,
    }
    url = str(candidate["url"])
    if not robots_allowed(session, url, robots_cache, allow_private=allow_private):
        return {**base, "status": "blocked_by_robots"}
    res: requests.Response | None = None
    try:
        res = response_get(session, url, allow_private=allow_private, stream=True)
        res.raise_for_status()
        ctype, ext = media_type(res.headers.get("content-type", ""), res.url)
        if not ctype or not ext:
            return {**base, "status": "not_recognized_media"}
        limit = 40 * 1024 * 1024 if ctype.startswith("video/") else 16 * 1024 * 1024
        if remaining_bytes is not None:
            limit = min(limit, remaining_bytes)
        if (
            res.headers.get("Content-Length", "").isdigit()
            and int(res.headers["Content-Length"]) > limit
        ):
            return {**base, "status": "too_large", "limit_bytes": limit}
        blob = bytearray()
        for chunk in res.iter_content(chunk_size=65536):
            blob.extend(chunk)
            if len(blob) > limit:
                return {**base, "status": "too_large", "limit_bytes": limit}
        if not blob:
            return {**base, "status": "empty"}
        dimensions: list[int] | None = None
        if ctype.startswith("image/") and ext != ".svg":
            try:
                with Image.open(io.BytesIO(blob)) as im:
                    dimensions = [im.width, im.height]
            except (UnidentifiedImageError, OSError, ValueError):
                return {**base, "status": "invalid_image"}
        sha = hashlib.sha256(blob).hexdigest()
        if sha in seen_sha:
            return {**base, "status": "duplicate", "same_as": seen_sha[sha], "sha256": sha}
        source_hint = Path(unquote(urlparse(res.url).path)).stem
        fname = slugify(str(candidate.get("alt", "")) or source_hint)[:36] + "-" + sha[:12] + ext
        out = folder / fname
        out.write_bytes(blob)
        rel = "assets/website/" + fname
        seen_sha[sha] = rel
        return {
            **base,
            "status": "downloaded",
            "file": rel,
            "bytes": len(blob),
            "sha256": sha,
            "dimensions": dimensions,
            "mime": ctype,
        }
    except (requests.RequestException, RuntimeError, ValueError, OSError) as e:
        return {**base, "status": "failed", "error": str(e)[:240]}
    finally:
        if res is not None:
            res.close()


def collect(
    site_url: str,
    base_folder: str | Path,
    max_pages: int = 25,
    max_media: int = DEFAULT_MAX_MEDIA,
    *,
    allow_private: bool = False,
    resolver: ResolverFn | None = None,
    connector: ConnectorFn | None = None,
) -> dict[str, Any]:
    if not 1 <= max_pages <= 40 or not 1 <= max_media <= 120:
        raise ValueError("max_pages must be 1..40 and max_media 1..120")
    safe_url(site_url, allow_private=allow_private)
    parsed = urlparse(site_url)
    name = slugify((parsed.hostname or "").removeprefix("www."))
    folder = Path(base_folder) / name
    folder.mkdir(parents=True, exist_ok=True)
    for sub in (
        "assets/website",
        "assets/instagram",
        "assets/facebook",
        "assets/optimized",
        "research",
        "research/snapshots",
        "design",
        "qa",
        "website",
    ):
        (folder / sub).mkdir(parents=True, exist_ok=True)

    sess = build_collector_session(
        allow_private=allow_private,
        resolver=resolver,
        connector=connector,
    )
    queue: deque[str] = deque([site_url])
    seen_pages: set[str] = set()
    candidates: dict[str, dict[str, str]] = {}
    pages: list[dict[str, Any]] = []
    social: dict[str, list[str]] = {"instagram": [], "facebook": []}
    problems: list[dict[str, str]] = []
    robots: dict[str, RobotFileParser | bool | None] = {}
    deadline = time.monotonic() + 210
    while queue and len(seen_pages) < max_pages:
        if time.monotonic() > deadline:
            problems.append({"reason": "crawl_time_limit"})
            break
        page = normalize_page_url(queue.popleft())
        if (
            page in seen_pages
            or not same_site(page, site_url)
            or any(x in page.lower() for x in BAD_PAGE_HINTS)
        ):
            continue
        seen_pages.add(page)
        if not robots_allowed(sess, page, robots, allow_private=allow_private):
            problems.append({"url": page, "reason": "robots_disallow"})
            continue
        try:
            res = response_get(sess, page, allow_private=allow_private, stream=True)
            try:
                res.raise_for_status()
                status_code = res.status_code
                if "html" not in res.headers.get("content-type", "").lower():
                    continue
                payload = bytearray()
                for chunk in res.iter_content(65536):
                    payload.extend(chunk)
                    if len(payload) > 4 * 1024 * 1024:
                        break
                if len(payload) > 4 * 1024 * 1024:
                    problems.append({"url": page, "reason": "html_too_large"})
                    continue
                res.encoding = res.encoding or "utf8"
                html = payload.decode(res.encoding, errors="replace")
            finally:
                res.close()
            fetched_at = now()
            page_sha = hashlib.sha256(payload).hexdigest()
            (folder / "research/snapshots" / f"{page_sha}.html").write_bytes(payload)
            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            description_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
            description = (
                _attr_str(description_tag.get("content", ""))
                if isinstance(description_tag, Tag)
                else ""
            )
            media = grab_media(soup, res.url)
            for m in media:
                candidates.setdefault(m["url"], m)
            links: list[str] = []
            for a in soup.select("a[href]"):
                target = candidate_url(_attr_str(a["href"]), res.url)
                if not target:
                    continue
                host = (urlparse(target).hostname or "").lower()
                if (
                    host in ("instagram.com", "www.instagram.com")
                    and target not in social["instagram"]
                ):
                    social["instagram"].append(target)
                if (
                    host
                    in ("facebook.com", "www.facebook.com", "m.facebook.com", "fb.com", "fb.watch")
                    and target not in social["facebook"]
                ):
                    social["facebook"].append(target)
                if (
                    same_site(target, site_url)
                    and Path(urlparse(target).path.lower()).suffix not in MEDIA_EXTENSIONS
                ):
                    links.append(normalize_page_url(target))
            links = sorted(
                set(links), key=lambda u: (not any(k in u.lower() for k in PAGE_HINTS), u)
            )
            for link in links:
                if link not in seen_pages and link not in queue:
                    queue.append(link)
            body_page = collect_page_text(soup, res.url)
            pages.append(
                {
                    "url": res.url,
                    "sha256": page_sha,
                    "fetched_at": fetched_at,
                    "status_code": status_code,
                    "title": title,
                    "description": description,
                    "text": body_page["text"],
                    "media_candidates": len(media),
                }
            )
        except (requests.RequestException, ValueError, RuntimeError) as e:
            problems.append({"url": page, "reason": str(e)[:240]})
    assets: list[dict[str, Any]] = []
    hashes: dict[str, str] = {}
    total_bytes = 0
    for item in list(candidates.values())[:max_media]:
        if time.monotonic() > deadline:
            assets.append(
                {
                    **item,
                    "status": "skipped_crawl_time_limit",
                    "captured_at": now(),
                    "rights_confirmed_for_preview": False,
                }
            )
            continue
        if total_bytes >= MAX_SITE_BYTES:
            assets.append(
                {
                    **item,
                    "status": "skipped_site_storage_limit",
                    "captured_at": now(),
                    "rights_confirmed_for_preview": False,
                }
            )
            continue
        row = download_one(
            sess,
            item,
            folder / "assets/website",
            hashes,
            robots,
            allow_private=allow_private,
            remaining_bytes=MAX_SITE_BYTES - total_bytes,
        )
        # Count all items actually present, including assets from earlier runs.
        total_bytes += int(row.get("bytes", 0))
        assets.append(row)
    status: dict[str, Any] = {
        "site_url": site_url,
        "captured_at": now(),
        "project_folder": str(folder),
        "pages_crawled": len(pages),
        "asset_candidates": len(candidates),
        "assets_saved": sum(a["status"] == "downloaded" for a in assets),
        "asset_duplicates": sum(a["status"] == "duplicate" for a in assets),
        "website_media_bytes_this_run": total_bytes,
        "issues": problems
        + [
            {
                "url": asset.get("url"),
                "source_page": asset.get("source_page"),
                "status": asset["status"],
                "reason": (
                    asset.get("error")
                    or (
                        "robots.txt disallowed the URL or access could not be verified"
                        if asset["status"] == "blocked_by_robots"
                        else "Media download failed without an error message"
                    )
                ),
            }
            for asset in assets
            if asset.get("status") in {"failed", "blocked_by_robots"}
        ],
        "notes": [
            "Public website HTML only; JavaScript-injected content may need browser fallback.",
            (
                "Instagram/Facebook content is not silently scraped. "
                "Their URLs are recorded for authorized collection."
            ),
            "All factual business claims require later verification before publication.",
        ],
    }
    (folder / "research/pages.json").write_text(
        json.dumps(pages, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (folder / "research/asset-manifest.json").write_text(
        json.dumps(assets, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (folder / "research/social-sources.json").write_text(
        json.dumps(
            {**social, "collection_status": "needs_authorized_collection_or_manual_upload"},
            indent=2,
        ),
        encoding="utf-8",
    )
    (folder / "research/run-summary.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8"
    )
    return status


def main() -> None:
    p = argparse.ArgumentParser(description="Pet grooming asset collector")
    p.add_argument("url", help="Full public business website URL")
    p.add_argument("--output", default="./projects")
    p.add_argument("--max-pages", type=int, default=25)
    p.add_argument("--max-media", type=int, default=DEFAULT_MAX_MEDIA)
    a = p.parse_args()
    print(json.dumps(collect(a.url, a.output, a.max_pages, a.max_media), indent=2))


if __name__ == "__main__":
    main()

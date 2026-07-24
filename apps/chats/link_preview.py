"""Fetch Open Graph / HTML metadata for chat link previews (with SSRF guards)."""

from __future__ import annotations

import html
import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from django.core.cache import cache

CACHE_TTL = 60 * 60 * 6
MAX_BYTES = 512_000
FETCH_TIMEOUT = 5
USER_AGENT = (
    'Mozilla/5.0 (compatible; MonicaLinkPreview/1.0; +https://metamonica.ru)'
)

_META_NAME_RE = re.compile(
    r'^(og:title|og:description|og:image|og:site_name|twitter:title|'
    r'twitter:description|twitter:image|description)$',
    re.I,
)


class _PreviewParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self._in_title = False
        self.meta: dict[str, str] = {}
        self.icons: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        low = tag.lower()
        attr = {((k or '').lower()): (v or '') for k, v in attrs}
        if low == 'title':
            self._in_title = True
            return
        if low == 'meta':
            key = attr.get('property') or attr.get('name') or ''
            content = attr.get('content', '').strip()
            if content and _META_NAME_RE.match(key):
                self.meta[key.lower()] = content
            return
        if low == 'link':
            rel = attr.get('rel', '').lower()
            href = attr.get('href', '').strip()
            if href and any(part in rel for part in ('icon', 'apple-touch-icon', 'shortcut icon')):
                self.icons.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == 'title':
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def validate_preview_url(raw: str) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if len(text) > 2048:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in ('http', 'https'):
        return None
    if parsed.username or parsed.password:
        return None
    host = (parsed.hostname or '').strip().lower()
    if not host or host == 'localhost' or host.endswith('.local'):
        return None
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None
    ips = {info[4][0] for info in infos if info and info[4]}
    if not ips or any(not _is_public_ip(ip) for ip in ips):
        return None
    # Rebuild without fragment
    return parsed._replace(fragment='').geturl()


def _pick(meta: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = (meta.get(key) or '').strip()
        if value:
            return html.unescape(value)
    return ''


def _absolute(base: str, maybe: str) -> str:
    if not maybe:
        return ''
    return urljoin(base, maybe)


def fetch_link_preview(url: str) -> dict[str, Any] | None:
    safe = validate_preview_url(url)
    if not safe:
        return None

    cache_key = f'link-preview:v1:{safe}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None

    result: dict[str, Any] | None = None
    try:
        req = Request(
            safe,
            headers={
                'User-Agent': USER_AGENT,
                'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'ru,en;q=0.8',
            },
            method='GET',
        )
        with urlopen(req, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310 — URL validated above
            final_url = resp.geturl() or safe
            if not validate_preview_url(final_url):
                cache.set(cache_key, {}, CACHE_TTL)
                return None
            ctype = (resp.headers.get('Content-Type') or '').lower()
            if 'html' not in ctype and 'xml' not in ctype and ctype:
                # Non-HTML: still return hostname card
                host = urlparse(final_url).hostname or ''
                result = {
                    'url': final_url,
                    'title': host,
                    'description': '',
                    'image': '',
                    'favicon': f'{urlparse(final_url).scheme}://{host}/favicon.ico' if host else '',
                    'site_name': host,
                }
            else:
                raw = resp.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raw = raw[:MAX_BYTES]
                charset = 'utf-8'
                if 'charset=' in ctype:
                    charset = ctype.split('charset=', 1)[1].split(';')[0].strip() or 'utf-8'
                try:
                    text = raw.decode(charset, errors='replace')
                except LookupError:
                    text = raw.decode('utf-8', errors='replace')

                parser = _PreviewParser()
                try:
                    parser.feed(text)
                    parser.close()
                except Exception:
                    pass

                host = urlparse(final_url).hostname or ''
                title = (
                    _pick(parser.meta, 'og:title', 'twitter:title')
                    or html.unescape(''.join(parser.title_parts).strip())
                    or host
                )
                description = _pick(parser.meta, 'og:description', 'twitter:description', 'description')
                image = _absolute(final_url, _pick(parser.meta, 'og:image', 'twitter:image'))
                favicon = ''
                if parser.icons:
                    favicon = _absolute(final_url, parser.icons[0])
                if not favicon and host:
                    favicon = f'{urlparse(final_url).scheme}://{host}/favicon.ico'
                site_name = _pick(parser.meta, 'og:site_name') or host

                result = {
                    'url': final_url,
                    'title': title[:300],
                    'description': description[:500],
                    'image': image[:2048],
                    'favicon': favicon[:2048],
                    'site_name': site_name[:120],
                }
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        result = None

    cache.set(cache_key, result or {}, CACHE_TTL)
    return result

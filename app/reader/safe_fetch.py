"""Fetch a public web page safely (pasted job URLs and listing pages).

Rules (from CLAUDE.md): https only; no credentials in the URL; resolve DNS first and refuse private,
loopback, link-local, and cloud-metadata addresses; connect to the validated IP so a second lookup
cannot be rebound; re-check every redirect hop (max 3); timeout and response-size cap.
"""
import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.scout.http import USER_AGENT

MAX_REDIRECTS = 3
MAX_BYTES = 2_000_000
TIMEOUT = httpx.Timeout(15.0, connect=8.0)
HTML_TYPES = ("text/html", "application/xhtml+xml")
REDIRECT_CODES = {301, 302, 303, 307, 308}

Resolver = Callable[[str, int], list[str]]


class UnsafeURL(ValueError):
    """The URL breaks a safety rule and was not fetched."""


class PageFetchError(Exception):
    """The page could not be fetched (network error, bad redirect, unreadable type)."""


@dataclass
class FetchedPage:
    """A fetched page: final URL, status, content type, decoded text."""

    url: str
    status: int
    content_type: str
    text: str
    truncated: bool = False


def resolve(host: str, port: int) -> list[str]:
    """Resolve a hostname to its IP addresses."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise PageFetchError("could not resolve host") from None
    return sorted({str(info[4][0]).split("%")[0] for info in infos})


def is_public_ip(address: str) -> bool:
    """True only for globally routable unicast addresses.

    This rejects 127.0.0.0/8, 10/8, 172.16/12, 192.168/16, 169.254/16 (incl. cloud metadata),
    ::1, fc00::/7, fe80::/10, CGNAT, multicast, and reserved ranges.
    """
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(url: str, resolver: Resolver = resolve) -> tuple[str, str, list[str]]:
    """Check a URL against the safety rules; return (host, request path+query, public IPs)."""
    if any(ch in url for ch in "\r\n\t ") or not url.strip():
        raise UnsafeURL("The URL contains whitespace or control characters")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UnsafeURL("Only https URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURL("URLs with credentials are not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURL("The URL has no host")
    try:
        port = parts.port or 443
    except ValueError:
        raise UnsafeURL("The URL has an invalid port") from None
    if port != 443:
        raise UnsafeURL("Only the default https port is allowed")
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = resolver(host, port)
    if not addresses:
        raise PageFetchError("could not resolve host")
    if not all(is_public_ip(a) for a in addresses):
        raise UnsafeURL("The URL points to a private or non-public address")
    path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    return host, path, addresses


def fetch_public_page(
    url: str, *, resolver: Resolver = resolve, transport: httpx.BaseTransport | None = None
) -> FetchedPage:
    """GET a page following the safety rules. Raises UnsafeURL or PageFetchError."""
    current = url
    host, path, addresses = validate_url(url, resolver)  # refuse an unsafe URL before anything else is set up
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(transport=transport, timeout=TIMEOUT, follow_redirects=False, headers=headers) as client:
        for hop in range(MAX_REDIRECTS + 1):
            if hop > 0:
                host, path, addresses = validate_url(current, resolver)  # every redirect hop is checked again
            response = _get_from_any_address(client, host, path, addresses)
            if response.status_code in REDIRECT_CODES:
                location = response.headers.get("location")
                if not location:
                    raise PageFetchError("redirect without a location")
                if hop == MAX_REDIRECTS:
                    raise PageFetchError("too many redirects")
                current = urljoin(current, location)  # validated again at the top of the loop
                continue
            return _to_page(current, response)
    raise PageFetchError("too many redirects")  # pragma: no cover


def _get_from_any_address(client: httpx.Client, host: str, path: str, addresses: list[str]) -> "_Response":
    """Connect to a validated IP (Host header and TLS name stay the original host)."""
    last_error = "request failed"
    for address in addresses:
        netloc = f"[{address}]" if ":" in address else address
        try:
            with client.stream(
                "GET", f"https://{netloc}{path}", headers={"Host": host}, extensions={"sni_hostname": host}
            ) as response:
                body, truncated = _read_capped(response)
                return _Response(response.status_code, dict(response.headers), body, truncated)
        except httpx.HTTPError as exc:
            last_error = f"request failed ({type(exc).__name__})"
    raise PageFetchError(last_error)


@dataclass
class _Response:
    status_code: int
    headers: dict[str, str]
    body: bytes
    truncated: bool


def _read_capped(response: httpx.Response) -> tuple[bytes, bool]:
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_BYTES:
            return b"".join(chunks)[:MAX_BYTES], True
    return b"".join(chunks), False


def _to_page(url: str, response: _Response) -> FetchedPage:
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if response.status_code == 200 and content_type not in HTML_TYPES:
        raise PageFetchError(f"unsupported content type ({content_type or 'unknown'})")
    charset = "utf-8"
    declared = response.headers.get("content-type", "")
    if "charset=" in declared:
        charset = declared.split("charset=", 1)[1].split(";")[0].strip().strip('"') or "utf-8"
    try:
        text = response.body.decode(charset, errors="replace")
    except LookupError:
        text = response.body.decode("utf-8", errors="replace")
    return FetchedPage(url=url, status=response.status_code, content_type=content_type, text=text,
                       truncated=response.truncated)

"""One-time upload tickets for binary files (``nc_webdav_create_upload``).

``nc_webdav_write_file`` takes its content as a tool argument, so a binary file
has to travel base64-encoded through the model's context. Past a few KB that is
unusable: a 240 KB PDF or a 2 MB Word file does not fit sensibly into one call.
Upload tickets move the bytes out of band instead:

1. The model calls ``nc_webdav_create_upload`` with the target ``path``, the
   file's ``size`` in bytes and its ``sha256``.
2. The tool issues a random, single-use ticket bound to those values, to the
   ``if_match`` precondition and to the Nextcloud user who asked for it, and
   returns ``<base>/upload/<ticket>`` plus a ready ``curl`` command.
3. The model sends the file from its sandbox:
   ``curl --fail-with-body -sS -X PUT --data-binary @file "<upload_url>"``.
4. :func:`handle_upload` checks size and hash and writes the file to Nextcloud
   via WebDAV **with that user's credentials** -- the same client
   ``get_client(ctx)`` returned for the tool call, never a service account.

The ticket is the only credential the PUT carries, so it is 32 random bytes,
lives ~10 minutes, and is consumed by the first attempt whatever its outcome.
Tickets are kept in process memory: they do not survive a restart and are not
shared between replicas. A deployment running several MCP replicas behind one
URL needs sticky routing for ``/upload/*`` or a single replica.
"""

import dataclasses
import hashlib
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from httpx import HTTPError, HTTPStatusError
from starlette.requests import Request
from starlette.responses import JSONResponse

from nextcloud_mcp_server.astrolabe_links import astrolabe_browser_base
from nextcloud_mcp_server.config import get_settings
from nextcloud_mcp_server.links import file_url
from nextcloud_mcp_server.server.tag_exclusion import (
    get_excluded_file_paths,
    is_path_excluded,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

#: Path the upload endpoint is mounted under (``PUT /upload/{ticket}``).
UPLOAD_ROUTE_PREFIX = "/upload/"

#: Entropy of a ticket. ``token_urlsafe(32)`` yields 43 URL-safe characters.
TICKET_BYTES = 32

#: Default lifetime when the setting is missing or non-positive.
DEFAULT_TICKET_TTL_SECONDS = 600

#: How long a used or expired ticket is remembered, so a retry is told 410
#: ("already used / expired") rather than the misleading 404 ("never existed").
TOMBSTONE_SECONDS = 3600

#: Bounds on the in-memory store. Each ticket also pins a NextcloudClient, so an
#: unbounded store would be a slow leak driven by any authenticated caller.
MAX_ACTIVE_TICKETS_PER_USER = 20
MAX_TICKETS = 1000

#: Overall timeout for the WebDAV PUT behind an upload. The client default (30 s)
#: is sized for API calls, not for pushing 50 MB to a slow Nextcloud.
UPLOAD_PUT_TIMEOUT_SECONDS = 300.0

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


class UploadTicketError(Exception):
    """Base class for ticket lookup failures; ``code`` is the JSON error slug."""

    code = "ticket_error"


class TicketNotFoundError(UploadTicketError):
    code = "ticket_not_found"


class TicketGoneError(UploadTicketError):
    """The ticket existed but is expired or was already used (HTTP 410)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TicketLimitError(UploadTicketError):
    code = "too_many_tickets"


@dataclass
class UploadTicket:
    """What a ticket authorises: one write of exactly these bytes to ``path``."""

    path: str
    size: int
    sha256: str
    if_match: str | None
    content_type: str | None
    #: Nextcloud user the ticket was issued to (for logging and per-user caps).
    user: str
    #: Deadline on the store's monotonic clock.
    expires_at: float
    #: The requesting user's client. Dropped as soon as the ticket can no longer
    #: be used, so credentials are not held longer than necessary.
    client: "NextcloudClient | None"
    used: bool = False


class UploadTicketStore:
    """In-memory, single-process store of upload tickets.

    ``issue`` and ``claim`` contain no ``await``, so under anyio's cooperative
    scheduling each runs atomically: two concurrent PUTs with the same ticket
    cannot both claim it, and no lock is needed.

    Tickets are keyed by the SHA-256 of the token rather than the token itself,
    so the secret never sits in the store (or a heap dump of it) verbatim.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._tickets: dict[str, UploadTicket] = {}

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _purge(self, now: float) -> None:
        for key, ticket in list(self._tickets.items()):
            if now >= ticket.expires_at + TOMBSTONE_SECONDS:
                del self._tickets[key]
            elif now >= ticket.expires_at:
                ticket.client = None

    def __len__(self) -> int:
        return len(self._tickets)

    def issue(
        self,
        *,
        client: "NextcloudClient",
        path: str,
        size: int,
        sha256: str,
        if_match: str | None,
        content_type: str | None,
        ttl_seconds: float,
    ) -> tuple[str, UploadTicket]:
        """Create a ticket and return ``(token, ticket)``.

        Raises:
            TicketLimitError: The user already holds the maximum number of open
                tickets, or the store is full.
        """
        now = self._clock()
        self._purge(now)
        user = client.username
        open_for_user = sum(
            1
            for t in self._tickets.values()
            if t.user == user and not t.used and now < t.expires_at
        )
        if open_for_user >= MAX_ACTIVE_TICKETS_PER_USER:
            raise TicketLimitError(
                f"{user!r} already holds {open_for_user} open upload tickets; "
                "use or let some expire before requesting more"
            )
        if len(self._tickets) >= MAX_TICKETS:
            raise TicketLimitError("Upload ticket store is full; try again later")

        token = secrets.token_urlsafe(TICKET_BYTES)
        ticket = UploadTicket(
            path=path,
            size=size,
            sha256=sha256,
            if_match=if_match,
            content_type=content_type,
            user=user,
            expires_at=now + ttl_seconds,
            client=client,
        )
        self._tickets[self._key(token)] = ticket
        return token, ticket

    def claim(self, token: str) -> UploadTicket:
        """Consume a ticket and return a copy that still carries its client.

        The stored ticket is marked used and loses its client immediately, so the
        ticket works for exactly one attempt regardless of how that attempt ends.

        Raises:
            TicketNotFoundError: Unknown token (or forgotten after the tombstone
                period).
            TicketGoneError: Expired, or already used.
        """
        now = self._clock()
        self._purge(now)
        ticket = self._tickets.get(self._key(token))
        if ticket is None:
            raise TicketNotFoundError("Unknown upload ticket")
        if ticket.used:
            raise TicketGoneError(
                "ticket_used",
                "This upload ticket was already used; request a new one with "
                "nc_webdav_create_upload",
            )
        if now >= ticket.expires_at or ticket.client is None:
            raise TicketGoneError(
                "ticket_expired",
                "This upload ticket has expired; request a new one with "
                "nc_webdav_create_upload",
            )
        claimed = dataclasses.replace(ticket)
        ticket.used = True
        ticket.client = None
        return claimed


_store = UploadTicketStore()


def get_upload_ticket_store() -> UploadTicketStore:
    """Return the process-wide ticket store."""
    return _store


# ── Helpers shared with the tool ─────────────────────────────────────────


def upload_max_bytes() -> int | None:
    """``WEBDAV_UPLOAD_MAX_MB`` in bytes, or None when the cap is disabled (0)."""
    max_mb = get_settings().webdav_upload_max_mb
    if not max_mb or max_mb <= 0:
        return None
    return int(max_mb * 1024 * 1024)


def upload_ticket_ttl_seconds() -> int:
    ttl = get_settings().webdav_upload_ticket_ttl_seconds
    return ttl if ttl and ttl > 0 else DEFAULT_TICKET_TTL_SECONDS


def _strip_base(url: str) -> str:
    base = url.strip().rstrip("/")
    # NEXTCLOUD_MCP_SERVER_URL is sometimes configured as the MCP endpoint
    # itself; the upload route lives at the server root, next to /mcp.
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    return base


def upload_base_url(request: Any) -> str | None:
    """Externally reachable base URL for upload links.

    ``WEBDAV_UPLOAD_BASE_URL`` wins, then ``NEXTCLOUD_MCP_SERVER_URL``, then the
    base URL of the MCP request that asked for the ticket (right for a direct
    connection, wrong scheme/host behind some reverse proxies -- hence the
    settings).
    """
    settings = get_settings()
    for configured in (
        settings.webdav_upload_base_url,
        settings.nextcloud_mcp_server_url,
    ):
        if configured and configured.strip():
            return _strip_base(configured)
    base_url = getattr(request, "base_url", None)
    if base_url is None:
        return None
    return _strip_base(str(base_url))


def normalize_sha256(value: str) -> str | None:
    """Lower-case hex digest, or None if ``value`` is not 64 hex characters."""
    value = value.strip()
    if not _SHA256_RE.fullmatch(value):
        return None
    return value.lower()


def validate_upload_path(path: str) -> str | None:
    """Return a reason the path is unusable as an upload target, else None."""
    stripped = path.strip()
    if not stripped.strip("/"):
        return "path must name a file, not the root folder"
    if stripped.endswith("/"):
        return "path must name a file, not a folder (trailing slash)"
    if any(seg in (".", "..") for seg in stripped.split("/")):
        return "path must not contain '.' or '..' segments"
    return None


# ── HTTP endpoint ────────────────────────────────────────────────────────


def _error(status: int, code: str, reason: str) -> JSONResponse:
    return JSONResponse({"error": code, "reason": reason}, status_code=status)


async def handle_upload(request: Request) -> JSONResponse:
    """``PUT /upload/{ticket}`` -- receive the bytes of one ticketed upload.

    Order of checks: ticket (404/410) → declared ``Content-Length`` (413/422,
    before a single body byte is read) → streamed byte count (413/422, aborted
    as soon as it overruns) → SHA-256 (422) → excluded tags (403) → WebDAV PUT
    with the ticket's precondition (412 / 423 / 409 / 502).
    """
    token = request.path_params.get("ticket", "")
    try:
        ticket = get_upload_ticket_store().claim(token)
    except TicketNotFoundError as e:
        return _error(404, e.code, str(e))
    except TicketGoneError as e:
        return _error(410, e.code, str(e))

    client = ticket.client
    if client is None:  # claim() only hands out live tickets; defensive
        return _error(410, "ticket_expired", "This upload ticket has expired")
    max_bytes = upload_max_bytes()

    def _too_large(n: int) -> JSONResponse:
        return _error(
            413,
            "too_large",
            f"Upload of {n} bytes exceeds WEBDAV_UPLOAD_MAX_MB ({max_bytes} bytes)",
        )

    def _size_mismatch(detail: str) -> JSONResponse:
        return _error(
            422,
            "size_mismatch",
            f"{detail}, but the ticket was issued for {ticket.size} bytes",
        )

    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            declared = int(raw_length)
        except ValueError:
            return _error(400, "invalid_content_length", "Malformed Content-Length")
        if max_bytes is not None and declared > max_bytes:
            return _too_large(declared)
        if declared != ticket.size:
            return _size_mismatch(f"Content-Length is {declared}")

    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        received += len(chunk)
        if received > ticket.size:
            if max_bytes is not None and received > max_bytes:
                return _too_large(received)
            return _size_mismatch(f"More than {ticket.size} bytes were sent")
        hasher.update(chunk)
        chunks.append(chunk)

    if received != ticket.size:
        return _size_mismatch(f"Received {received} bytes")
    if hasher.hexdigest() != ticket.sha256:
        return _error(
            422,
            "hash_mismatch",
            "SHA-256 of the received body does not match the ticket",
        )

    # Re-checked here, not only when the ticket was issued: a tag added to the
    # target or a parent folder in the meantime must still block the write.
    excluded = await get_excluded_file_paths(client.webdav)
    if is_path_excluded(ticket.path, excluded):
        return _error(
            403,
            "excluded",
            f"Access denied: {ticket.path!r} is tagged with an excluded tag",
        )

    content = b"".join(chunks)
    del chunks
    try:
        result = await client.webdav.write_file(
            ticket.path,
            content,
            ticket.content_type,
            if_match=ticket.if_match,
            timeout=UPLOAD_PUT_TIMEOUT_SECONDS,
        )
    except HTTPStatusError as e:
        status = e.response.status_code
        logger.warning(
            "Upload of %r for %s failed: Nextcloud answered %s",
            ticket.path,
            ticket.user,
            status,
        )
        if status == 409:
            return _error(
                409,
                "parent_missing",
                "The target folder does not exist; create it with "
                "nc_webdav_create_directory",
            )
        if status in (401, 403):
            return _error(
                403,
                "nextcloud_denied",
                f"Nextcloud refused the write for {ticket.user!r} ({status})",
            )
        if status == 507:
            return _error(507, "insufficient_storage", "Nextcloud quota exceeded")
        return _error(502, "nextcloud_error", f"Nextcloud answered {status}")
    except HTTPError as e:
        logger.warning("Upload of %r for %s failed: %s", ticket.path, ticket.user, e)
        return _error(502, "nextcloud_unreachable", f"Nextcloud request failed: {e}")

    status_code = result.get("status_code")
    if status_code == 412:
        return _error(412, "precondition_failed", str(result.get("message")))
    if status_code == 423:
        return _error(423, "locked", str(result.get("message")))

    url = None
    browser_base = astrolabe_browser_base()
    if browser_base:
        # Fail-open like nc_webdav_read_file: the link is a convenience, the
        # write already happened and must not be reported as failed.
        try:
            url = file_url(browser_base, await client.webdav.get_fileid(ticket.path))
        except Exception as e:
            logger.debug(
                "No file link for %r: fileid lookup failed: %s", ticket.path, e
            )

    created = status_code == 201
    logger.info(
        "Upload ticket wrote %r for %s (%d bytes, %s)",
        ticket.path,
        ticket.user,
        ticket.size,
        "created" if created else "overwritten",
    )
    return JSONResponse(
        {
            "path": ticket.path,
            "size": ticket.size,
            "etag": result.get("etag"),
            "created": created,
            "url": url,
        },
        status_code=201 if created else 200,
    )

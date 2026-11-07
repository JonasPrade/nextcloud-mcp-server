"""Unit tests for upload tickets (``nc_webdav_create_upload`` + ``PUT /upload``).

Three layers, each with the Nextcloud side mocked:

* ``UploadTicketStore`` -- single use, expiry, tombstones, per-user caps.
* ``handle_upload`` -- driven through a real Starlette app over httpx's ASGI
  transport, so Content-Length handling and body streaming are exercised as an
  HTTP client would hit them.
* the MCP tool -- argument validation, preflight and the ticket it issues.
"""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.routing import Route

from nextcloud_mcp_server.models.webdav import CreateUploadResponse
from nextcloud_mcp_server.server import webdav_upload
from nextcloud_mcp_server.server.webdav import configure_webdav_tools
from nextcloud_mcp_server.server.webdav_upload import (
    MAX_ACTIVE_TICKETS_PER_USER,
    TOMBSTONE_SECONDS,
    UPLOAD_PUT_TIMEOUT_SECONDS,
    TicketGoneError,
    TicketLimitError,
    TicketNotFoundError,
    UploadTicketStore,
    handle_upload,
    normalize_sha256,
    upload_base_url,
    validate_upload_path,
)

pytestmark = pytest.mark.unit

BODY = b"%PDF-1.7 fake pdf bytes \x00\x01\x02" * 100
DIGEST = hashlib.sha256(BODY).hexdigest()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "webdav_upload_max_mb": 50.0,
        "webdav_upload_ticket_ttl_seconds": 600,
        "webdav_upload_base_url": None,
        "nextcloud_mcp_server_url": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _fake_client(username: str = "alice") -> SimpleNamespace:
    client = SimpleNamespace(username=username)
    client.webdav = AsyncMock()
    client.webdav.write_file = AsyncMock(
        return_value={"status_code": 201, "etag": "etag-1"}
    )
    client.webdav.get_fileid = AsyncMock(return_value="4711")
    return client


@pytest.fixture
def store(mocker) -> tuple[UploadTicketStore, FakeClock]:
    clock = FakeClock()
    fresh = UploadTicketStore(clock=clock)
    mocker.patch.object(webdav_upload, "_store", fresh)
    return fresh, clock


@pytest.fixture
def settings(mocker):
    """Patch settings in both modules that read them; returns an installer."""

    def _install(**overrides) -> SimpleNamespace:
        values = _settings(**overrides)
        mocker.patch.object(webdav_upload, "get_settings", return_value=values)
        mocker.patch(
            "nextcloud_mcp_server.server.webdav.get_settings", return_value=values
        )
        return values

    _install()
    return _install


@pytest.fixture
def no_exclusions(mocker):
    async def fake(*_, **__):
        return set()

    mocker.patch.object(webdav_upload, "get_excluded_file_paths", side_effect=fake)
    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_excluded_file_paths", side_effect=fake
    )


@pytest.fixture
def browser_base(mocker):
    mocker.patch.object(
        webdav_upload, "astrolabe_browser_base", return_value="https://cloud.example"
    )


def _issue(store: UploadTicketStore, client, **overrides) -> str:
    kwargs = {
        "client": client,
        "path": "Vorstand/Urkunde.pdf",
        "size": len(BODY),
        "sha256": DIGEST,
        "if_match": None,
        "content_type": None,
        "ttl_seconds": 600,
    }
    kwargs.update(overrides)
    token, _ = store.issue(**kwargs)
    return token


def _http() -> httpx.AsyncClient:
    app = Starlette(routes=[Route("/upload/{ticket}", handle_upload, methods=["PUT"])])
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mcp.test"
    )


# ── Store ────────────────────────────────────────────────────────────────


def test_ticket_is_long_and_not_stored_verbatim(store):
    s, _ = store
    token = _issue(s, _fake_client())
    assert len(token) >= 43  # 32 random bytes, URL-safe base64
    assert token not in s._tickets


def test_claim_is_single_use(store):
    s, _ = store
    client = _fake_client()
    token = _issue(s, client)

    claimed = s.claim(token)
    assert claimed.client is client
    assert claimed.path == "Vorstand/Urkunde.pdf"

    with pytest.raises(TicketGoneError) as exc:
        s.claim(token)
    assert exc.value.code == "ticket_used"


def test_claim_drops_client_reference_from_store(store):
    s, _ = store
    token = _issue(s, _fake_client())
    s.claim(token)
    (stored,) = s._tickets.values()
    assert stored.client is None
    assert stored.used is True


def test_expired_ticket_is_gone_then_forgotten(store):
    s, clock = store
    token = _issue(s, _fake_client(), ttl_seconds=600)
    clock.now += 600

    with pytest.raises(TicketGoneError) as exc:
        s.claim(token)
    assert exc.value.code == "ticket_expired"

    clock.now += TOMBSTONE_SECONDS
    with pytest.raises(TicketNotFoundError):
        s.claim(token)
    assert len(s) == 0


def test_unknown_ticket_is_not_found(store):
    s, _ = store
    with pytest.raises(TicketNotFoundError):
        s.claim("no-such-ticket")


def test_per_user_ticket_cap(store):
    s, _ = store
    alice = _fake_client("alice")
    for _ in range(MAX_ACTIVE_TICKETS_PER_USER):
        _issue(s, alice)
    with pytest.raises(TicketLimitError):
        _issue(s, alice)
    # Another user is unaffected by alice's cap.
    _issue(s, _fake_client("bob"))


# ── Helpers ──────────────────────────────────────────────────────────────


def test_normalize_sha256():
    assert normalize_sha256(DIGEST.upper()) == DIGEST
    assert normalize_sha256(f"  {DIGEST}\n") == DIGEST
    assert normalize_sha256("abc") is None
    assert normalize_sha256("z" * 64) is None


@pytest.mark.parametrize(
    "path, ok",
    [
        ("Vorstand/11 Bescheinigungen/Urkunde.pdf", True),
        ("/Urkunde.pdf", True),
        ("", False),
        ("/", False),
        ("Vorstand/", False),
        ("Vorstand/../Urkunde.pdf", False),
    ],
)
def test_validate_upload_path(path, ok):
    assert (validate_upload_path(path) is None) is ok


def test_upload_base_url_precedence(settings):
    request = SimpleNamespace(base_url="http://request.host:8000/")
    assert upload_base_url(request) == "http://request.host:8000"

    settings(nextcloud_mcp_server_url="https://mcp.example/mcp/")
    assert upload_base_url(request) == "https://mcp.example"

    settings(
        nextcloud_mcp_server_url="https://mcp.example",
        webdav_upload_base_url="https://upload.example/",
    )
    assert upload_base_url(request) == "https://upload.example"


# ── Endpoint ─────────────────────────────────────────────────────────────


async def test_upload_success_writes_with_ticket_client(
    store, settings, no_exclusions, browser_base
):
    s, _ = store
    client = _fake_client()
    token = _issue(s, client, content_type="application/pdf")

    async with _http() as http:
        response = await http.put(f"/upload/{token}", content=BODY)

    assert response.status_code == 201
    assert response.json() == {
        "path": "Vorstand/Urkunde.pdf",
        "size": len(BODY),
        "etag": "etag-1",
        "created": True,
        "url": "https://cloud.example/index.php/f/4711",
    }
    client.webdav.write_file.assert_awaited_once_with(
        "Vorstand/Urkunde.pdf",
        BODY,
        "application/pdf",
        if_match=None,
        timeout=UPLOAD_PUT_TIMEOUT_SECONDS,
    )


async def test_upload_overwrite_reports_created_false(
    store, settings, no_exclusions, browser_base
):
    s, _ = store
    client = _fake_client()
    client.webdav.write_file.return_value = {"status_code": 204, "etag": "etag-2"}
    token = _issue(s, client, if_match="etag-1")

    async with _http() as http:
        response = await http.put(f"/upload/{token}", content=BODY)

    assert response.status_code == 200
    assert response.json()["created"] is False
    assert client.webdav.write_file.await_args.kwargs["if_match"] == "etag-1"


async def test_upload_link_is_optional(store, settings, no_exclusions, mocker):
    mocker.patch.object(webdav_upload, "astrolabe_browser_base", return_value=None)
    s, _ = store
    token = _issue(s, _fake_client())

    async with _http() as http:
        response = await http.put(f"/upload/{token}", content=BODY)

    assert response.status_code == 201
    assert response.json()["url"] is None


async def test_upload_ticket_used_once(store, settings, no_exclusions, browser_base):
    s, _ = store
    token = _issue(s, _fake_client())

    async with _http() as http:
        first = await http.put(f"/upload/{token}", content=BODY)
        second = await http.put(f"/upload/{token}", content=BODY)

    assert first.status_code == 201
    assert second.status_code == 410
    assert second.json()["error"] == "ticket_used"


async def test_upload_unknown_ticket_404(store, settings):
    async with _http() as http:
        response = await http.put("/upload/nope", content=BODY)
    assert response.status_code == 404
    assert response.json()["error"] == "ticket_not_found"


async def test_upload_expired_ticket_410(store, settings):
    s, clock = store
    token = _issue(s, _fake_client())
    clock.now += 601
    async with _http() as http:
        response = await http.put(f"/upload/{token}", content=BODY)
    assert response.status_code == 410
    assert response.json()["error"] == "ticket_expired"


async def test_upload_content_length_mismatch_rejected_before_write(store, settings):
    s, _ = store
    client = _fake_client()
    token = _issue(s, client, size=len(BODY) + 1)

    async with _http() as http:
        response = await http.put(f"/upload/{token}", content=BODY)

    assert response.status_code == 422
    assert response.json()["error"] == "size_mismatch"
    client.webdav.write_file.assert_not_called()


async def test_upload_streamed_overrun_aborts(store, settings):
    """Without a Content-Length (chunked), extra bytes are caught mid-stream."""
    s, _ = store
    client = _fake_client()
    token = _issue(s, client, size=10)

    async def chunks():
        yield b"0123456789"
        yield b"overflow"

    async with _http() as http:
        response = await http.put(f"/upload/{token}", content=chunks())

    assert response.status_code == 422
    assert response.json()["error"] == "size_mismatch"
    client.webdav.write_file.assert_not_called()


async def test_upload_over_limit_413(store, settings):
    settings(webdav_upload_max_mb=0.001)  # ~1 KB
    s, _ = store
    client = _fake_client()
    token = _issue(s, client, size=len(BODY))

    async with _http() as http:
        response = await http.put(f"/upload/{token}", content=BODY)

    assert response.status_code == 413
    assert response.json()["error"] == "too_large"
    client.webdav.write_file.assert_not_called()


async def test_upload_hash_mismatch_422(store, settings):
    s, _ = store
    client = _fake_client()
    token = _issue(s, client, sha256="0" * 64)

    async with _http() as http:
        response = await http.put(f"/upload/{token}", content=BODY)

    assert response.status_code == 422
    assert response.json()["error"] == "hash_mismatch"
    client.webdav.write_file.assert_not_called()


async def test_upload_excluded_path_403(store, settings, mocker):
    async def fake(*_, **__):
        return {"Vorstand"}

    mocker.patch.object(webdav_upload, "get_excluded_file_paths", side_effect=fake)
    s, _ = store
    client = _fake_client()
    token = _issue(s, client)

    async with _http() as http:
        response = await http.put(f"/upload/{token}", content=BODY)

    assert response.status_code == 403
    assert response.json()["error"] == "excluded"
    client.webdav.write_file.assert_not_called()


@pytest.mark.parametrize(
    "result, status, code",
    [
        (
            {"status_code": 412, "message": "File already exists"},
            412,
            "precondition_failed",
        ),
        ({"status_code": 423, "message": "Locked"}, 423, "locked"),
    ],
)
async def test_upload_conflicts_from_nextcloud(
    store, settings, no_exclusions, result, status, code
):
    s, _ = store
    client = _fake_client()
    client.webdav.write_file.return_value = result
    token = _issue(s, client)

    async with _http() as http:
        response = await http.put(f"/upload/{token}", content=BODY)

    assert response.status_code == status
    assert response.json()["error"] == code


@pytest.mark.parametrize(
    "nc_status, status, code",
    [
        (409, 409, "parent_missing"),
        (401, 403, "nextcloud_denied"),
        (507, 507, "insufficient_storage"),
        (500, 502, "nextcloud_error"),
    ],
)
async def test_upload_nextcloud_http_errors(
    store, settings, no_exclusions, nc_status, status, code
):
    s, _ = store
    client = _fake_client()
    request = httpx.Request("PUT", "http://nc/remote.php/dav/files/alice/x")
    client.webdav.write_file.side_effect = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(nc_status, request=request)
    )
    token = _issue(s, client)

    async with _http() as http:
        response = await http.put(f"/upload/{token}", content=BODY)

    assert response.status_code == status
    assert response.json()["error"] == code


# ── Tool ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def basicauth_mode():
    """Make ``require_scopes`` take the BasicAuth pass-through branch."""
    with patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=SimpleNamespace(enable_login_flow=False),
    ):
        yield


@pytest.fixture
def create_upload():
    mcp = MCPServer(name="test-webdav-upload")
    configure_webdav_tools(mcp)
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    return tools["nc_webdav_create_upload"].fn


@pytest.fixture
def patch_get_client(mocker):
    def _install(client):
        async def fake_get_client(ctx):
            return client

        mocker.patch(
            "nextcloud_mcp_server.server.webdav.get_client", side_effect=fake_get_client
        )

    return _install


def _ctx(base_url: str | None = "http://localhost:8000/") -> SimpleNamespace:
    request = None if base_url is None else SimpleNamespace(base_url=base_url)
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


async def test_tool_issues_bound_ticket(
    create_upload, store, settings, no_exclusions, patch_get_client
):
    s, _ = store
    client = _fake_client()
    client.webdav.get_fileid = AsyncMock(
        side_effect=lambda p: "1" if p == "Vorstand" else None
    )
    patch_get_client(client)

    result = await create_upload(
        path="Vorstand/Urkunde.pdf",
        size=len(BODY),
        sha256=DIGEST.upper(),
        ctx=_ctx(),
    )

    assert isinstance(result, CreateUploadResponse)
    assert result.path == "Vorstand/Urkunde.pdf"
    assert result.expires_in == 600
    assert result.upload_url.startswith("http://localhost:8000/upload/")
    assert result.upload_url in result.curl
    assert "--data-binary @<file>" in result.curl

    token = result.upload_url.rsplit("/", 1)[1]
    ticket = s.claim(token)
    assert ticket.client is client
    assert ticket.user == "alice"
    assert ticket.size == len(BODY)
    assert ticket.sha256 == DIGEST
    assert ticket.if_match is None


async def test_tool_refuses_without_http_transport(
    create_upload, store, settings, patch_get_client
):
    patch_get_client(_fake_client())
    ctx = _ctx(base_url=None)
    with pytest.raises(ToolError, match="HTTP transport"):
        await create_upload(path="a.pdf", size=1, sha256=DIGEST, ctx=ctx)


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"sha256": "abc"}, "sha256"),
        ({"size": -1}, "size"),
        ({"path": "Vorstand/"}, "Invalid path"),
    ],
)
async def test_tool_validates_arguments(
    create_upload, store, settings, patch_get_client, kwargs, match
):
    patch_get_client(_fake_client())
    args = {"path": "a.pdf", "size": 1, "sha256": DIGEST}
    args.update(kwargs)
    ctx = _ctx()
    with pytest.raises(ToolError, match=match):
        await create_upload(ctx=ctx, **args)


async def test_tool_refuses_over_limit(
    create_upload, store, settings, patch_get_client
):
    settings(webdav_upload_max_mb=1.0)
    patch_get_client(_fake_client())
    ctx = _ctx()
    with pytest.raises(ToolError, match="WEBDAV_UPLOAD_MAX_MB"):
        await create_upload(path="a.pdf", size=2 * 1024 * 1024, sha256=DIGEST, ctx=ctx)


async def test_tool_refuses_excluded_path(
    create_upload, store, settings, patch_get_client, mocker
):
    async def fake(*_, **__):
        return {"Vorstand"}

    mocker.patch(
        "nextcloud_mcp_server.server.webdav.get_excluded_file_paths", side_effect=fake
    )
    patch_get_client(_fake_client())
    ctx = _ctx()
    with pytest.raises(ToolError, match="excluded tag"):
        await create_upload(path="Vorstand/Urkunde.pdf", size=1, sha256=DIGEST, ctx=ctx)
    assert len(store[0]) == 0


async def test_tool_preflight_existing_file_without_if_match(
    create_upload, store, settings, no_exclusions, patch_get_client
):
    client = _fake_client()
    client.webdav.get_fileid = AsyncMock(return_value="42")
    patch_get_client(client)
    ctx = _ctx()
    with pytest.raises(ToolError, match="already exists"):
        await create_upload(path="Vorstand/Urkunde.pdf", size=1, sha256=DIGEST, ctx=ctx)


async def test_tool_preflight_missing_parent(
    create_upload, store, settings, no_exclusions, patch_get_client
):
    client = _fake_client()
    client.webdav.get_fileid = AsyncMock(return_value=None)
    patch_get_client(client)
    ctx = _ctx()
    with pytest.raises(ToolError, match="does not exist"):
        await create_upload(path="Vorstand/Urkunde.pdf", size=1, sha256=DIGEST, ctx=ctx)


async def test_tool_preflight_force_overwrite_needs_existing_file(
    create_upload, store, settings, no_exclusions, patch_get_client
):
    client = _fake_client()
    client.webdav.get_fileid = AsyncMock(
        side_effect=lambda p: "1" if p == "Vorstand" else None
    )
    patch_get_client(client)
    ctx = _ctx()
    with pytest.raises(ToolError, match="omit if_match"):
        await create_upload(
            path="Vorstand/Urkunde.pdf",
            size=1,
            sha256=DIGEST,
            ctx=ctx,
            if_match="*",
        )


async def test_tool_preflight_is_fail_open(
    create_upload, store, settings, no_exclusions, patch_get_client
):
    """A PROPFIND that errors must not block the ticket: the PUT decides."""
    client = _fake_client()
    client.webdav.get_fileid = AsyncMock(side_effect=httpx.ConnectError("down"))
    patch_get_client(client)
    result = await create_upload(
        path="Vorstand/Urkunde.pdf", size=1, sha256=DIGEST, ctx=_ctx()
    )
    assert result.upload_url

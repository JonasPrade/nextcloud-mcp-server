"""End-to-end tests for upload tickets against the running single-user stack.

Exercises the real flow: ``nc_webdav_create_upload`` over MCP, then a raw HTTP
PUT of the bytes to the returned URL (what ``curl --data-binary`` does), then a
direct WebDAV read to prove the exact bytes landed in Nextcloud.

Requires the ``mcp`` container (``docker compose up --build -d mcp``).
"""

import hashlib
import json
import os
import uuid

import httpx
import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

pytestmark = pytest.mark.integration


async def _create_upload(
    nc_mcp_client: ClientSession, path: str, body: bytes, **extra
) -> dict:
    result = await nc_mcp_client.call_tool(
        "nc_webdav_create_upload",
        {
            "path": path,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            **extra,
        },
    )
    assert result.is_error is False, f"create_upload failed: {result.content}"
    return json.loads(result.content[0].text)


@pytest.fixture
async def upload_dir(nc_client: NextcloudClient):
    path = f"mcp_upload_test_{uuid.uuid4().hex[:8]}"
    await nc_client.webdav.create_directory(path)
    try:
        yield path
    finally:
        await nc_client.webdav.delete_resource(path)


async def test_upload_ticket_round_trip(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, upload_dir: str
):
    # Binary content that would be awkward as base64 in a tool call.
    body = os.urandom(300 * 1024)
    path = f"{upload_dir}/Urkunde.pdf"

    ticket = await _create_upload(nc_mcp_client, path, body)
    assert ticket["path"] == path
    assert ticket["expires_in"] > 0
    assert ticket["upload_url"] in ticket["curl"]

    async with httpx.AsyncClient(timeout=60) as http:
        response = await http.put(ticket["upload_url"], content=body)
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["path"] == path
        assert data["size"] == len(body)
        assert data["created"] is True
        assert data["etag"]

        # Single use: the same URL is refused afterwards.
        again = await http.put(ticket["upload_url"], content=body)
        assert again.status_code == 410
        assert again.json()["error"] == "ticket_used"

    content, _, etag = await nc_client.webdav.read_file(path)
    assert content == body
    assert etag == data["etag"]

    # Replacing requires the etag (or "*"), exactly like nc_webdav_write_file.
    replacement = os.urandom(64 * 1024)
    ticket = await _create_upload(
        nc_mcp_client, path, replacement, if_match=data["etag"]
    )
    async with httpx.AsyncClient(timeout=60) as http:
        response = await http.put(ticket["upload_url"], content=replacement)
    assert response.status_code == 200, response.text
    assert response.json()["created"] is False

    content, _, _ = await nc_client.webdav.read_file(path)
    assert content == replacement


async def test_upload_ticket_rejects_wrong_bytes(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, upload_dir: str
):
    body = b"declared content"
    path = f"{upload_dir}/mismatch.bin"
    ticket = await _create_upload(nc_mcp_client, path, body)

    async with httpx.AsyncClient(timeout=30) as http:
        # Same length, different bytes: passes the size check, fails the hash.
        response = await http.put(ticket["upload_url"], content=b"differs content!")
    assert response.status_code == 422
    assert response.json()["error"] == "hash_mismatch"

    assert await nc_client.webdav.get_fileid(path) is None


async def test_upload_ticket_unknown_is_404(nc_mcp_client: ClientSession):
    ticket = await _create_upload(nc_mcp_client, "irrelevant.bin", b"x")
    unknown = ticket["upload_url"].rsplit("/", 1)[0] + "/not-a-ticket"
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.put(unknown, content=b"x")
    assert response.status_code == 404
    assert response.json()["error"] == "ticket_not_found"


async def test_create_upload_refuses_existing_path_without_if_match(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient, upload_dir: str
):
    path = f"{upload_dir}/exists.txt"
    await nc_client.webdav.write_file(path, b"already here")

    result = await nc_mcp_client.call_tool(
        "nc_webdav_create_upload",
        {"path": path, "size": 3, "sha256": hashlib.sha256(b"new").hexdigest()},
    )
    assert result.is_error is True
    assert "already exists" in result.content[0].text

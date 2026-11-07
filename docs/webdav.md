# WebDAV support

### WebDAV File System Tools

| Tool | Description |
|------|-------------|
| `nc_webdav_list_directory` | List files and directories in any NextCloud path |
| `nc_webdav_read_file` | Read file content (documents extracted to text/markdown, text decoded, other binary as base64) |
| `nc_webdav_write_file` | Create or update files in NextCloud |
| `nc_webdav_create_upload` | Get a one-time upload URL to send a (binary) file out of band, without base64 |
| `nc_webdav_create_directory` | Create new directories |
| `nc_webdav_delete_resource` | Delete files or directories |
| `nc_webdav_move_resource` | Move or rename files and directories |
| `nc_webdav_copy_resource` | Copy files and directories |
| `nc_webdav_list_comments` | Read the comments on a file |
| `nc_webdav_create_comment` | Comment on a file, mentioning people to notify them |

### WebDAV File System Access

The server provides complete file system access to your NextCloud instance, enabling you to:

- Browse any directory structure
- Read and write files of any type
- Create and delete directories
- Manage your NextCloud files directly through LLM interactions

**Usage Examples:**

```python
# List files in root directory
await nc_webdav_list_directory("")

# Browse a specific folder
await nc_webdav_list_directory("Documents/Projects")

# Read a text file
content = await nc_webdav_read_file("Documents/readme.txt")

# Read a document: extracted text by default, no base64
result = await nc_webdav_read_file("Documents/report.pdf")
result.content          # the document's text
result.parse_tier       # "fast" | "structured" | "ocr" -- what produced it
result.content_format   # "text" | "markdown" | "base64"
result.parse_notes      # non-empty => say what degraded; this is not the whole document

# Ask for structure (headings, tables) instead of a flat text layer
await nc_webdav_read_file("Documents/report.pdf", parse_document="markdown")

# Read part of a long PDF: only these pages are parsed and returned
result = await nc_webdav_read_file("Documents/report.pdf", page_start=21, page_end=40)
result.page_count       # total pages in the document -- read on while page_end < page_count
result.page_start, result.page_end   # the pages this content covers (21, 40)

# Or take the file itself, unparsed
await nc_webdav_read_file("Documents/report.pdf", parse_document="raw")

# Create a new directory
await nc_webdav_create_directory("NewProject/docs")

# Write content to a file
await nc_webdav_write_file("NewProject/docs/notes.md", "# My Notes\n\nContent here...")

# Delete a file or directory
await nc_webdav_delete_resource("old_file.txt")

# Move or rename a file
await nc_webdav_move_resource("document.txt", "new_name.txt")

# Move a file to another directory
await nc_webdav_move_resource("document.txt", "Archive/document.txt")

# Move a directory
await nc_webdav_move_resource("Projects/OldProject", "Projects/NewProject")

# Copy a file
await nc_webdav_copy_resource("document.txt", "document_copy.txt")

# Copy a file to another directory
await nc_webdav_copy_resource("document.txt", "Backup/document.txt")

# Copy a directory
await nc_webdav_copy_resource("Projects/ProjectA", "Projects/ProjectA_Backup")
```

### File Comments

Comments annotate a file in place — review requests, hand-offs, context that
does not belong in the file itself. They are the same comments the Nextcloud
web UI shows in a file's sidebar.

```python
# Ask a colleague to review; @-mentioning them sends the notification
await nc_webdav_create_comment(
    "Reports/q3.pdf", 'numbers updated, please review @"alice"'
)

# Read the thread, newest first
await nc_webdav_list_comments("Reports/q3.pdf")

# Page through a long thread
await nc_webdav_list_comments("Reports/q3.pdf", limit=20, offset=20)
```

Mention a user by their Nextcloud user ID: `@alice`, or `@"user id with spaces"`.
Nextcloud parses the mention out of the message when it stores the comment and
sends the notification itself.

A comment is capped at **1000 characters** (measured after trimming whitespace,
counting Unicode code points — Nextcloud's own rule). Longer content belongs in
the file, with a short pointer comment.

### Safe Writes: Concurrent Edits and Locks

`nc_webdav_write_file` is **fail-closed** — it never silently overwrites an
existing file. Every write is a conditional PUT; the `if_match` argument
selects one of three modes:

| `if_match`            | Behaviour |
|-----------------------|-----------|
| omitted (`None`)      | **Create-only.** Fails if the path already exists. |
| an `etag`             | **Safe overwrite.** Fails if the file changed since that etag was read. |
| `"*"`                 | **Force-overwrite.** Overwrites unconditionally; fails only if the file does not exist. |

To change an existing file, read it first to obtain its `etag`
(`nc_webdav_read_file` returns one), then pass that `etag` back into the
write. If the file changed in the meantime (e.g. someone edited it directly
in the Nextcloud web UI), the write fails instead of clobbering their change.
`nc_webdav_list_directory` and the search/find tools also return an `etag`
per file, so you can obtain one without a full read.

```python
# Read, capture the etag, and write back safely
result = await nc_webdav_read_file("Documents/notes.md")
await nc_webdav_write_file(
    "Documents/notes.md", result["content"] + "\nMore.", if_match=result["etag"]
)
# Raises ToolError if the file changed since the read (etag mismatch, HTTP 412)
# or if it's locked by another client, e.g. open in the web editor (HTTP 423).

# Create a brand-new file (fails with ToolError if it already exists):
await nc_webdav_write_file("Documents/new.md", "# New")

# Deliberately replace an existing file wholesale, without reading it first:
await nc_webdav_write_file("Documents/notes.md", "# Regenerated", if_match="*")
```

> **Breaking change (0.x):** an `if_match`-less write over an *existing* file
> now fails with a `ToolError` rather than overwriting it (the previous
> last-write-wins behaviour). Pass the file's `etag`, or `if_match="*"` to
> force the overwrite.

### Write Size Limit

`nc_webdav_write_file` builds its request from a single in-memory MCP tool
argument — there is no chunked/streaming upload for writes (unlike the
read/ingest path). A pre-flight size gate rejects content over
`WEBDAV_WRITE_MAX_MB` (default 50, `0` disables) with a clear error rather
than risking a timeout or out-of-memory failure on a very large PUT.

## Upload Tickets for Binary Files

`nc_webdav_write_file` takes the file as a tool argument, so binary content has
to travel base64-encoded through the model's context. Beyond a few KB that is
impractical: a 240 KB PDF or a 2 MB Word document does not fit sensibly into
one call. When the file already exists in the client's sandbox, use an upload
ticket instead and send the bytes directly over HTTP:

1. Call `nc_webdav_create_upload` with the target `path`, the file's `size` in
   bytes and its hex `sha256` (plus optional `if_match` and `content_type`).
2. The tool returns `upload_url`, a ready `curl` command, `expires_in` and
   `path`.
3. Send the file:
   `curl --fail-with-body -sS -X PUT --data-binary @Urkunde.pdf "<upload_url>"`
4. The server checks size and SHA-256 and writes the file to Nextcloud via
   WebDAV, with the credentials of the user who requested the ticket.

```python
# size = stat -c %s Urkunde.pdf, sha256 = sha256sum Urkunde.pdf
ticket = await nc_webdav_create_upload(
    path="Vorstand/11 Bescheinigungen/Urkunde.pdf",
    size=245_760,
    sha256="9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
)
# then, in the sandbox: run ticket["curl"] with <file> replaced by the local path
```

`if_match` works exactly as for `nc_webdav_write_file`: omit it to create a new
file only, pass an etag to replace the file only if it is unchanged, or pass
`"*"` to force-overwrite an existing file. The tool already refuses a ticket
that is certain to fail (target folder missing, file exists without
`if_match`, `"*"` for a missing file), so no bytes are sent in vain. The
conditional PUT remains the authoritative check.

### The ticket

- 32 random bytes in the URL path, valid for `WEBDAV_UPLOAD_TICKET_TTL_SECONDS`
  (default 600) and for **one attempt**: the first PUT consumes it whatever the
  outcome, so request a new ticket after an error.
- Bound to `path`, `size`, `sha256`, `if_match` and the Nextcloud user who
  requested it. The PUT carries no other credential. The write uses that user's
  client (BasicAuth, Login Flow v2 app password or OAuth token, as for every
  other tool), never a service account.
- Held in process memory: tickets do not survive a restart and are not shared
  between replicas. Several replicas behind one URL need sticky routing for
  `/upload/*`, or a single replica.
- At most 20 open tickets per user.

### The upload endpoint: `PUT /upload/{ticket}`

Checks run in this order, and nothing is written unless all pass:

| Status | `error` | Meaning |
|--------|---------|---------|
| 404 | `ticket_not_found` | Unknown ticket |
| 410 | `ticket_expired` / `ticket_used` | Ticket expired or already used |
| 413 | `too_large` | Body exceeds `WEBDAV_UPLOAD_MAX_MB` |
| 422 | `size_mismatch` | `Content-Length` or the bytes received differ from `size` (checked before the body is read, and aborted as soon as more bytes arrive) |
| 422 | `hash_mismatch` | SHA-256 of the body differs from `sha256` |
| 403 | `excluded` | Target or a parent folder carries an `EXCLUDED_TAGS` tag (re-checked at upload time) |
| 409 | `parent_missing` | The target folder does not exist |
| 412 | `precondition_failed` | `if_match` precondition failed (file exists, changed, or is missing for `"*"`) |
| 423 | `locked` | File is locked, e.g. open in the web editor |
| 403 / 507 / 502 | `nextcloud_denied` / `insufficient_storage` / `nextcloud_error` | Nextcloud refused or failed the write |

Every error body is `{"error": "...", "reason": "..."}`. On success (201 for a
new file, 200 for a replaced one) the body is:

```json
{"path": "Vorstand/11 Bescheinigungen/Urkunde.pdf", "size": 245760,
 "etag": "\"6a1f...\"", "created": true,
 "url": "https://cloud.example.org/index.php/f/4711"}
```

`url` opens the file in Nextcloud. It is `null` when no browser-reachable
Nextcloud URL is configured.

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `WEBDAV_UPLOAD_MAX_MB` | `50` | Size cap for ticketed uploads, separate from `WEBDAV_WRITE_MAX_MB`. `0` disables the cap. The body is buffered in memory until the hash is verified. |
| `WEBDAV_UPLOAD_TICKET_TTL_SECONDS` | `600` | Ticket lifetime |
| `WEBDAV_UPLOAD_BASE_URL` | unset | Externally reachable base URL for upload links, e.g. `https://mcp.example.org`. Falls back to `NEXTCLOUD_MCP_SERVER_URL`, then to the base URL of the MCP request. Set it when the server sits behind a reverse proxy that does not forward the public scheme and host. |

The endpoint needs the HTTP transport. Under stdio the tool refuses and points
to `nc_webdav_write_file`. Make sure a reverse proxy in front of the server
allows request bodies up to `WEBDAV_UPLOAD_MAX_MB` on `/upload/` (nginx:
`client_max_body_size`).

## Conditional move and copy

`nc_webdav_move_resource` and `nc_webdav_copy_resource` accept an optional
`if_destination_match`. With `overwrite=True` alone the destination is replaced
unconditionally; supplying the destination's ETag replaces it **only if it is
still that exact version**, so a file someone else changed in the meantime is not
clobbered.

```python
info = await nc_webdav_read_file(path="Docs/report.txt")
await nc_webdav_move_resource(
    source_path="Drafts/report.txt",
    destination_path="Docs/report.txt",
    overwrite=True,
    if_destination_match=info["etag"],
)
```

### Why not `If-Match`?

`If-Match` applies to the **request-URI**, which for MOVE/COPY is the *source*.
Conditioning the destination requires RFC 4918 §10.4's tagged-list `If:` form,
which names the resource explicitly:

```
If: </remote.php/dav/files/user/Docs/report.txt> (["etag"])
```

### Limitations

Both come from sabre/dav and are surfaced rather than hidden:

- **Files only.** The etag is checked with `$node instanceof IFile`, so a
  **directory** destination always fails the condition with 412.
- **A missing destination yields 404, not 412.** An `If:` condition naming a URI
  that does not exist raises `NotFound` inside sabre. "The destination must
  exist" is therefore not expressible this way — `overwrite=False` already covers
  "must not exist".

`if_destination_match="*"` and combining it with `overwrite=False` both raise
`ValueError` at the client boundary rather than being silently reinterpreted.

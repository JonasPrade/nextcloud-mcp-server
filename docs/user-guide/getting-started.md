# Getting Started

A short guide to using the Nextcloud MCP Server from an AI assistant (Claude, ChatGPT, an IDE, …). For installation and operator setup, see [Installation](../installation.md) and [Configuration](../configuration.md).

## Features

The server lets an MCP client read and change your Nextcloud data through natural language. It exposes 110+ tools across these apps:

| App | What you can do |
|-----|-----------------|
| Notes | Create, read, update, delete, search notes |
| Files (WebDAV) | Browse, read, write, move, copy, tag, comment, restore versions and trash; text extraction/OCR for PDFs, DOCX, images |
| Calendar | Events and tasks (VTODO), recurring events, attendees, free/busy availability |
| Contacts | Address books and contacts (CardDAV) |
| Deck | Boards, stacks, cards, labels, assignments, comments, attachments |
| Collectives | Collectives, pages, tags |
| Mail | Accounts, mailboxes, messages, attachments, sending, flags, tags |
| Talk | Conversations, reading and posting messages |
| Polls | Create polls, add options, vote, comment, read results |
| Cookbook, Tables, News, Shopping List, Sharing | Recipes (incl. URL import), table rows, feeds, shopping lists, share links |

Additional capabilities:

- MCP resources (`nc://…` URIs) for browsing data directly
- Optional semantic search across Notes, Files, News, Deck and Mail (operator must enable it)
- Tools for apps that aren't installed on your Nextcloud, or are too old, are hidden automatically
- Files tagged with an excluded system tag are invisible to the assistant

## How Login Works

The server never uses your Nextcloud login password. It talks to Nextcloud with an **app password**, which you can see and revoke at any time under **Settings → Security → Devices & sessions**. How that app password is obtained depends on how the server was deployed.

### Single-user (personal setup)

You create an app password yourself and put it into the server configuration:

```bash
NEXTCLOUD_HOST=https://cloud.example.com
NEXTCLOUD_USERNAME=alice
NEXTCLOUD_PASSWORD=<app password>
```

Every request runs as that one account. No further login is needed in the client.

### Multi-user (Login Flow v2 — hosted / shared server)

Login happens in two steps, and only once per user:

1. **Sign in to the MCP server (OAuth).** When you add the server URL (`https://<mcp-server>/mcp`) to your client, the client opens a browser window. You sign in with your Nextcloud account (or your organisation's identity provider) and approve the requested permissions, e.g. `notes.read`, `calendar.write`.
2. **Grant Nextcloud access (Login Flow v2).** On the first tool call the server has no app password for you yet. Your client shows a link (or the assistant prints it). Open it, log in to Nextcloud and click **Grant access**. Nextcloud creates an app password, the server checks it belongs to the same user who signed in, and stores it encrypted.

After that, the assistant retries the request and everything works without further prompts. If something looks off, ask the assistant to run `nc_auth_check_status`.

Permissions are per app (`<app>.read` / `<app>.write`). A tool you haven't granted a scope for is refused. To add or remove permissions later, ask the assistant to run `nc_auth_update_scopes`.

### Multi-user BasicAuth (internal setups)

Some internal deployments let each client send its own username and app password in the HTTP `Authorization` header. In that case, configure your client with your Nextcloud username and an app password.

## Using It

### Connect your client

Local, single-user, via stdio:

```json
{
  "mcpServers": {
    "nextcloud": {
      "command": "uvx",
      "args": ["nextcloud-mcp-server", "run", "--transport", "stdio"],
      "env": {
        "NEXTCLOUD_HOST": "https://cloud.example.com",
        "NEXTCLOUD_USERNAME": "alice",
        "NEXTCLOUD_PASSWORD": "<app password>"
      }
    }
  }
}
```

Remote server: add `https://<mcp-server>/mcp` as a custom connector / remote MCP server in your client and follow the login steps above.

### Ask in plain language

You don't call tools by name — just describe what you want:

- "Summarise my notes in the category *Projects*."
- "What's on my calendar next week? Find a free hour on Thursday."
- "Create a Deck card *Review budget* in the *Backlog* stack of board *Team*, due Friday."
- "Read `/Documents/report.pdf` and list the action items."
- "Add the ingredients of the lasagne recipe to my shopping list."

Read-only requests run directly. For changes (create, update, delete) most clients ask you to confirm the tool call first — check what it does before approving, especially for deletions.

### Revoking access

Delete the app password under **Settings → Security → Devices & sessions** in Nextcloud. In multi-user mode the server will ask you to grant access again on the next request.

## See Also

- [Authentication](../authentication.md) — all deployment modes in detail
- [Login Flow v2](../login-flow-v2.md) — architecture, scope reference, troubleshooting
- [Troubleshooting](../troubleshooting.md)

# arc-mcp

MCP (Model Context Protocol) server for Arc browser. Manage spaces and tabs
programmatically. Written in Python, with safety hardening — see
[Security](#security-notes).

## Features

- **List spaces** – get all Arc browser spaces
- **List tabs** – get the pinned/unpinned tab & folder hierarchy of a space
- **Focus space** – switch to a space (AppleScript)
- **Open URL** – open a URL in Arc immediately (AppleScript)
- **Create / delete space, add / delete tab, create workspace** – edit Arc's
  sidebar file directly (opt-in, see below)

## Requirements

- macOS (Arc is macOS-only)
- Arc browser installed
- Python ≥ 3.10
- For space/tab **file** modifications: **Arc Sync disabled** and **Arc quit**

## Installation

```bash
uv venv
uv pip install mcp        # or: uv pip install -e .
```

## Usage

### With Claude Code

Read-only + live actions (`list_spaces`, `list_tabs`, `focus_space`, `open_url`):

```json
{
  "mcpServers": {
    "arc": {
      "command": "/absolute/path/to/arc-mcp/.venv/bin/python",
      "args": ["-m", "arc_mcp"]
    }
  }
}
```

To also expose the file-mutating tools, add the opt-in env var:

```json
{
  "mcpServers": {
    "arc": {
      "command": "/absolute/path/to/arc-mcp/.venv/bin/python",
      "args": ["-m", "arc_mcp"],
      "env": { "ARC_MCP_ENABLE_WRITES": "1" }
    }
  }
}
```

### Running directly

```bash
.venv/bin/python -m arc_mcp
```

## Tools

| Tool | Args | Notes |
| --- | --- | --- |
| `list_spaces` | – | read-only |
| `list_tabs` | `space?` | read-only; defaults to first space |
| `focus_space` | `space` | AppleScript |
| `open_url` | `url`, `space?` | AppleScript; http/https only |
| `create_space` | `name`, `icon?` | write; needs `ARC_MCP_ENABLE_WRITES=1` |
| `delete_space` | `space` | write |
| `add_tab` | `space`, `url`, `title?`, `pinned?` | write; http/https only |
| `delete_tab` | `tabId` | write |
| `create_workspace` | `name`, `urls[]`, `icon?` | write |

## Environment variables

| Var | Effect |
| --- | --- |
| `ARC_MCP_ENABLE_WRITES=1` | Register the 5 file-mutating tools (off by default) |
| `ARC_MCP_ALLOW_RUNNING=1`  | Allow file mutations while Arc is running (unsafe) |

## Security notes

- **No AppleScript / shell injection.** `osascript` is invoked with an argument
  list (no shell) and every interpolated value is escaped; URLs are restricted to
  `http`/`https`, so a crafted URL or space title can't inject commands.
- **Atomic writes** (temp file + `os.replace`, `0600`), so a crash can't corrupt
  the 2–3 MB sidebar file.
- **Backups rotated** (last 10) and stored in `~/.arc-mcp/backups/`, not inside
  Arc's own directory.
- **Correct timestamps** (Apple reference epoch) and **correct icon shape**
  (`{emoji, emoji_v2}`), matching what Arc actually writes.
- **Mutations refuse to run while Arc is open** (override with
  `ARC_MCP_ALLOW_RUNNING=1`).
- **Writes are opt-in** via `ARC_MCP_ENABLE_WRITES`.

Note: `list_tabs` / `list_spaces` still expose your tab URLs to the model — treat
that as you would any browser-history sharing.

## How It Works

Arc stores its sidebar data in
`~/Library/Application Support/Arc/StorableSidebar.json`. This server reads and
(optionally) modifies that file for space/tab management, and uses AppleScript for
immediate actions (focus space, open URL).

## License

MIT

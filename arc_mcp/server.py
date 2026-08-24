"""MCP server for Arc browser — manage spaces and tabs.

Tools are grouped by risk:

* Always available: ``list_spaces``, ``list_tabs`` (read-only) and the live
  AppleScript actions ``focus_space`` / ``open_url``.
* File-mutating tools (``create_space``, ``delete_space``, ``add_tab``,
  ``delete_tab``, ``create_workspace``) directly rewrite Arc's sidebar file and
  are registered only when ``ARC_MCP_ENABLE_WRITES=1``. They also refuse to run
  while Arc is open unless ``ARC_MCP_ALLOW_RUNNING=1``.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from . import __version__, arc

mcp = MCPServer("arc-mcp", version=__version__)

WRITES_ENABLED = os.environ.get("ARC_MCP_ENABLE_WRITES") == "1"


class WorkspaceLink(BaseModel):
    """A tab to open in a new workspace."""

    url: Annotated[str, Field(description="http/https URL to open")]
    title: Annotated[str | None, Field(description="Optional tab title")] = None


# --------------------------------------------------------------------------- #
# Read-only tools
# --------------------------------------------------------------------------- #


@mcp.tool()
def list_spaces() -> str:
    """List all Arc browser spaces."""
    spaces = arc.list_spaces()
    return json.dumps([vars(s) for s in spaces], indent=2, ensure_ascii=False)


@mcp.tool()
def list_tabs(
    space: Annotated[
        str | None,
        Field(description="Name or ID of the space to list tabs from (defaults to first space)"),
    ] = None,
) -> str:
    """List tabs and folders in an Arc space with full pinned/unpinned hierarchy."""
    result = arc.list_tabs(space)
    if result is None:
        return f"Space not found: {space}" if space else "No spaces found"
    return json.dumps(result.to_dict(), indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Live AppleScript actions
# --------------------------------------------------------------------------- #


@mcp.tool()
def focus_space(
    space: Annotated[str, Field(description="Name or ID of the space to focus")],
) -> str:
    """Switch to a specific Arc space (uses AppleScript)."""
    try:
        arc.focus_space(space)
        return f'Switched to space "{space}"'
    except arc.ArcError as exc:
        return f"Failed to focus space: {exc}"


@mcp.tool()
def open_url(
    url: Annotated[str, Field(description="http/https URL to open")],
    space: Annotated[
        str | None, Field(description="Name or ID of the space to open the URL in (optional)")
    ] = None,
) -> str:
    """Open a URL in Arc immediately (uses AppleScript). Optionally target a space."""
    try:
        arc.open_url(url, space)
        return f'Opened {url} in space "{space}"' if space else f"Opened {url}"
    except arc.ArcError as exc:
        return f"Failed to open URL: {exc}"


# --------------------------------------------------------------------------- #
# File-mutating tools (opt-in)
# --------------------------------------------------------------------------- #


def _register_write_tools() -> None:
    @mcp.tool()
    def create_space(
        name: Annotated[str, Field(description="Name of the new space")],
        icon: Annotated[
            str, Field(description="Emoji (e.g. '🚀') or SF Symbol name (e.g. 'star.fill')")
        ] = "⭐",
    ) -> str:
        """Create a new Arc space. Requires Arc to be quit; restart to see it. Arc sync must be disabled to persist."""
        try:
            space_id = arc.create_space(name, icon)
            return f'Space "{name}" created (ID: {space_id}). Restart Arc to see it.'
        except arc.ArcError as exc:
            return f"Failed to create space: {exc}"

    @mcp.tool()
    def delete_space(
        space: Annotated[str, Field(description="Name or ID of the space to delete")],
    ) -> str:
        """Delete an Arc space by name or ID. Requires Arc to be quit; restart to see changes."""
        try:
            arc.delete_space(space)
            return f'Space "{space}" deleted. Restart Arc to see the changes.'
        except arc.ArcError as exc:
            return f"Failed to delete space: {exc}"

    @mcp.tool()
    def add_tab(
        space: Annotated[str, Field(description="Name or ID of the space to add the tab to")],
        url: Annotated[str, Field(description="http/https URL for the new tab")],
        title: Annotated[str | None, Field(description="Title for the tab (defaults to URL)")] = None,
        pinned: Annotated[bool, Field(description="Add as a pinned tab")] = False,
    ) -> str:
        """Add a tab to a space. Requires Arc to be quit; restart to see it. For live opening, use open_url."""
        try:
            tab_id = arc.add_tab(space, url, title, pinned)
            return f"Tab added (ID: {tab_id}). Restart Arc to see it in the sidebar."
        except arc.ArcError as exc:
            return f"Failed to add tab: {exc}"

    @mcp.tool()
    def delete_tab(
        tabId: Annotated[str, Field(description="ID of the tab to delete")],
    ) -> str:
        """Delete a tab by its ID. Requires Arc to be quit; restart to see changes."""
        try:
            arc.delete_tab(tabId)
            return "Tab deleted. Restart Arc to see the changes."
        except arc.ArcError as exc:
            return f"Failed to delete tab: {exc}"

    @mcp.tool()
    def create_workspace(
        name: Annotated[str, Field(description="Workspace name (e.g. a ticket ID like 'PROJ-1234')")],
        urls: Annotated[list[WorkspaceLink], Field(description="Tabs to open in the workspace")],
        icon: Annotated[str, Field(description="Emoji or SF Symbol for the space")] = "🔧",
    ) -> str:
        """Create a space plus its tabs in one call. Requires Arc to be quit; restart to see it."""
        try:
            links = [link.model_dump() for link in urls]
            space_id, tab_ids = arc.create_workspace(name, links, icon)
            return f'Workspace "{name}" created with {len(tab_ids)} tabs (Space ID: {space_id}). Restart Arc to see it.'
        except arc.ArcError as exc:
            return f"Failed to create workspace: {exc}"


if WRITES_ENABLED:
    _register_write_tools()


def main() -> None:
    mode = "read/write" if WRITES_ENABLED else "read-only (set ARC_MCP_ENABLE_WRITES=1 for write tools)"
    print(f"Arc MCP server running on stdio — mode: {mode}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()

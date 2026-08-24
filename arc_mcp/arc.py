"""Core logic for the Arc MCP server.

Reads and (optionally) mutates Arc's ``StorableSidebar.json`` directly, and
drives Arc via AppleScript for live actions (focus space, open URL).

Design notes:

* AppleScript is invoked via an argument list (no shell) and every interpolated
  string is escaped, so a crafted URL or space title cannot inject commands.
* URLs are restricted to http/https.
* The sidebar file is written atomically (temp file + ``os.replace``) with 0600
  permissions, so a crash mid-write can't corrupt it.
* Backups are rotated and kept outside Arc's own directory.
* Timestamps use Apple's reference epoch (2001-01-01), which is what Arc uses.
* Space icons are written in Arc's real ``{"emoji": <codepoint>, "emoji_v2": <str>}``
  shape.
* File mutations refuse to run while Arc is open (it holds the file in memory and
  will clobber external edits), unless ``ARC_MCP_ALLOW_RUNNING=1``.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

ARC_DIR = Path.home() / "Library" / "Application Support" / "Arc"
SIDEBAR_FILE = ARC_DIR / "StorableSidebar.json"

# Arc stores backups outside its own support directory to avoid polluting it.
BACKUP_DIR = Path.home() / ".arc-mcp" / "backups"
MAX_BACKUPS = 10

# Arc timestamps use the Apple/CoreFoundation reference epoch: 2001-01-01 UTC.
APPLE_EPOCH_OFFSET = 978_307_200

ALLOWED_URL_SCHEMES = {"http", "https"}


class ArcError(Exception):
    """Raised for expected, user-facing failures (bad input, Arc running, ...)."""


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass
class Space:
    id: str
    title: str
    icon: str | None = None


@dataclass
class Tab:
    id: str
    title: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "tab", "id": self.id, "title": self.title, "url": self.url}


@dataclass
class Folder:
    id: str
    title: str
    children: list[Tab | Folder] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "folder",
            "id": self.id,
            "title": self.title,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class SpaceTabs:
    space_id: str
    space_title: str
    pinned: list[Tab | Folder]
    unpinned: list[Tab | Folder]

    def to_dict(self) -> dict[str, Any]:
        return {
            "spaceId": self.space_id,
            "spaceTitle": self.space_title,
            "pinned": [c.to_dict() for c in self.pinned],
            "unpinned": [c.to_dict() for c in self.unpinned],
        }


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #


def _now_apple() -> float:
    """Current time in Arc's reference epoch (seconds since 2001-01-01)."""
    return time.time() - APPLE_EPOCH_OFFSET


def _new_id() -> str:
    return str(uuid.uuid4()).upper()


def load_sidebar() -> dict[str, Any]:
    if not SIDEBAR_FILE.exists():
        raise ArcError(f"Arc sidebar file not found at {SIDEBAR_FILE}. Is Arc installed?")
    with SIDEBAR_FILE.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_sidebar(data: dict[str, Any]) -> None:
    """Write the sidebar atomically with restrictive permissions.

    Writes to a temp file in the same directory, then ``os.replace`` swaps it in
    as a single atomic operation, so a crash mid-write can never truncate the
    real file.
    """
    ARC_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix="StorableSidebar.", suffix=".tmp.json", dir=str(ARC_DIR)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, SIDEBAR_FILE)
    finally:
        tmp.unlink(missing_ok=True)


def create_backup() -> Path:
    """Copy the current sidebar into the (rotated) backup directory."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    # Millisecond stamp keeps ordering without needing wall-clock formatting.
    stamp = int(time.time() * 1000)
    backup = BACKUP_DIR / f"StorableSidebar.backup.{stamp}.json"
    data = SIDEBAR_FILE.read_bytes()
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    _rotate_backups()
    return backup


def _rotate_backups() -> None:
    backups = sorted(BACKUP_DIR.glob("StorableSidebar.backup.*.json"))
    for old in backups[:-MAX_BACKUPS]:
        old.unlink(missing_ok=True)


def is_arc_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-x", "Arc"], capture_output=True, text=True, check=False
    )
    return result.returncode == 0


def _ensure_arc_not_running() -> None:
    """File mutations are unsafe while Arc holds the file open."""
    if os.environ.get("ARC_MCP_ALLOW_RUNNING") == "1":
        return
    if is_arc_running():
        raise ArcError(
            "Arc is currently running. It keeps StorableSidebar.json in memory and "
            "will overwrite external edits (and edits may corrupt its state). Quit "
            "Arc before modifying spaces/tabs, or set ARC_MCP_ALLOW_RUNNING=1 to "
            "override."
        )


def _container(data: dict[str, Any]) -> dict[str, Any]:
    """The space/items container (index 1 in Arc's layout)."""
    containers = data.get("sidebar", {}).get("containers", [])
    if len(containers) < 2 or not isinstance(containers[1], dict):
        raise ArcError("Unexpected sidebar structure: missing containers[1].")
    return containers[1]


def _get_device_id(data: dict[str, Any]) -> str:
    device_id = (
        data.get("firebaseSyncState", {})
        .get("syncData", {})
        .get("orderedSpaceIDs", {})
        .get("lastChangedDevice", "")
    )
    if device_id:
        return device_id
    for item in _container(data).get("items", []):
        if isinstance(item, dict) and item.get("originatingDevice"):
            return item["originatingDevice"]
    return ""


def _build_icon(icon: str) -> dict[str, Any]:
    """Return Arc's real iconType shape for an emoji or SF Symbol name."""
    if not icon:
        icon = "⭐"
    if "." in icon:  # SF Symbol, e.g. "star.fill"
        return {"icon": icon}
    # Emoji: Arc stores the leading codepoint plus the full string.
    return {"emoji": ord(icon[0]), "emoji_v2": icon}


def _validate_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise ArcError(
            f"Only http/https URLs are allowed (got scheme {parts.scheme!r})."
        )
    if not parts.netloc:
        raise ArcError(f"URL has no host: {url!r}")
    if any(c in url for c in "\r\n\t"):
        raise ArcError("URL must not contain control characters.")
    return url


def _find_space_obj(container: dict[str, Any], name_or_id: str) -> dict[str, Any] | None:
    """Find a raw space object in the sidebar container by id or title."""
    for item in container.get("spaces", []):
        if isinstance(item, dict) and (item.get("id") == name_or_id or item.get("title") == name_or_id):
            return item
    return None


def _resolve_space(name_or_id: str) -> Space | None:
    """Resolve a space (by id or title) to a Space, via a fresh sidebar read."""
    return next(
        (s for s in list_spaces() if s.id == name_or_id or s.title == name_or_id),
        None,
    )


# --------------------------------------------------------------------------- #
# AppleScript
# --------------------------------------------------------------------------- #


def _osa_escape(value: str) -> str:
    """Escape a string for safe embedding inside an AppleScript string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _run_osascript(script: str) -> None:
    """Run AppleScript via an argument list (no shell) so nothing can be injected."""
    proc = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise ArcError(proc.stderr.strip() or "osascript failed")


# --------------------------------------------------------------------------- #
# Read operations
# --------------------------------------------------------------------------- #


def list_spaces() -> list[Space]:
    data = load_sidebar()
    spaces: list[Space] = []
    for item in _container(data).get("spaces", []):
        if isinstance(item, dict) and item.get("id") and item.get("title"):
            icon_type = (item.get("customInfo") or {}).get("iconType") or {}
            icon = icon_type.get("emoji_v2") or icon_type.get("emoji") or icon_type.get("icon")
            spaces.append(Space(id=item["id"], title=item["title"], icon=icon))
    return spaces


def list_tabs(space_name_or_id: str | None = None) -> SpaceTabs | None:
    data = load_sidebar()
    container = _container(data)
    spaces = container.get("spaces", [])
    items = container.get("items", [])

    item_map: dict[str, dict[str, Any]] = {
        it["id"]: it for it in items if isinstance(it, dict) and "id" in it
    }

    if space_name_or_id is None:
        target = next((s for s in spaces if isinstance(s, dict) and s.get("id")), None)
    else:
        target = _find_space_obj(container, space_name_or_id)
    if target is None:
        return None

    container_ids = [c for c in (target.get("containerIDs") or []) if c not in ("pinned", "unpinned")]
    pinned_container_id = container_ids[0] if len(container_ids) > 0 else None
    unpinned_container_id = container_ids[1] if len(container_ids) > 1 else None

    def item_type(item: dict[str, Any]) -> str:
        data_ = item.get("data") or {}
        if data_.get("tab"):
            return "tab"
        if data_.get("itemContainer"):
            return "container"
        return "folder"

    def build_tree(item_id: str) -> Tab | Folder | None:
        item = item_map.get(item_id)
        if item is None:
            return None
        kind = item_type(item)
        if kind == "tab":
            tab = item.get("data", {}).get("tab", {})
            return Tab(
                id=item["id"],
                title=tab.get("savedTitle") or tab.get("savedURL") or "Untitled",
                url=tab.get("savedURL") or "",
            )
        if kind == "folder":
            children: list[Tab | Folder] = []
            for child_id in item.get("childrenIds") or []:
                child = build_tree(child_id)
                if child is not None:
                    children.append(child)
            return Folder(id=item["id"], title=item.get("title") or "Untitled", children=children)
        return None

    def children_of(container_id: str | None) -> list[Tab | Folder]:
        result: list[Tab | Folder] = []
        if container_id is None:
            return result
        container_item = item_map.get(container_id)
        if container_item is None:
            return result
        for child_id in container_item.get("childrenIds") or []:
            child = build_tree(child_id)
            if child is not None:
                result.append(child)
        return result

    return SpaceTabs(
        space_id=target["id"],
        space_title=target.get("title", ""),
        pinned=children_of(pinned_container_id),
        unpinned=children_of(unpinned_container_id),
    )


# --------------------------------------------------------------------------- #
# AppleScript-driven live actions
# --------------------------------------------------------------------------- #


def focus_space(space_name_or_id: str) -> None:
    space = _resolve_space(space_name_or_id)
    if space is None:
        raise ArcError(f"Space not found: {space_name_or_id}")
    title = _osa_escape(space.title)
    script = (
        'tell application "Arc"\n'
        "  tell front window\n"
        f'    tell space "{title}" to focus\n'
        "  end tell\n"
        "end tell"
    )
    _run_osascript(script)


def open_url(url: str, space_name_or_id: str | None = None) -> None:
    url = _validate_url(url)
    esc_url = _osa_escape(url)
    if space_name_or_id:
        space = _resolve_space(space_name_or_id)
        if space is None:
            raise ArcError(f"Space not found: {space_name_or_id}")
        esc_title = _osa_escape(space.title)
        script = (
            'tell application "Arc"\n'
            "  tell front window\n"
            f'    tell space "{esc_title}"\n'
            f'      make new tab with properties {{URL:"{esc_url}"}}\n'
            "    end tell\n"
            "  end tell\n"
            "end tell"
        )
    else:
        script = (
            'tell application "Arc"\n'
            "  tell front window\n"
            f'    make new tab with properties {{URL:"{esc_url}"}}\n'
            "  end tell\n"
            "end tell"
        )
    _run_osascript(script)


# --------------------------------------------------------------------------- #
# File-mutating operations
# --------------------------------------------------------------------------- #


def create_space(name: str, icon: str = "⭐") -> str:
    """Create a new space. Returns the new space id."""
    _ensure_arc_not_running()
    data = load_sidebar()
    create_backup()

    container = _container(data)
    space_id = _new_id()
    pinned_id = _new_id()
    unpinned_id = _new_id()
    ts = _now_apple()
    device_id = _get_device_id(data)

    theme = None
    for s in container.get("spaces", []):
        if isinstance(s, dict) and (s.get("customInfo") or {}).get("windowTheme"):
            theme = s["customInfo"]["windowTheme"]
            break
    if theme is None:
        raise ArcError("Could not find a windowTheme template from existing spaces.")

    space_obj = {
        "id": space_id,
        "title": name,
        "profile": {"default": True},
        "containerIDs": ["pinned", pinned_id, "unpinned", unpinned_id],
        "newContainerIDs": [
            {"pinned": {}},
            pinned_id,
            {"unpinned": {"_0": {"shared": {}}}},
            unpinned_id,
        ],
        "customInfo": {"iconType": _build_icon(icon), "windowTheme": theme},
    }

    def container_base() -> dict[str, Any]:
        return {
            "title": None,
            "parentID": None,
            "childrenIds": [],
            "createdAt": ts,
            "originatingDevice": device_id,
            "isUnread": False,
            "data": {"itemContainer": {"containerType": {"spaceItems": {"_0": space_id}}}},
        }

    pinned_cont = {**container_base(), "id": pinned_id}
    unpinned_cont = {**container_base(), "id": unpinned_id}

    # 1. sidebar.containers[1].spaces
    container.setdefault("spaces", []).extend([space_id, space_obj])

    # 2. sidebar.containers[1].items
    container.setdefault("items", []).extend(
        [pinned_id, pinned_cont, unpinned_id, unpinned_cont]
    )

    # 3. sidebarSyncState.container.value.orderedSpaceIDs
    ss = data.get("sidebarSyncState") or {}
    ordered = ss.get("container", {}).get("value", {}).get("orderedSpaceIDs")
    if isinstance(ordered, list):
        ordered.append(space_id)

    # 4. sidebarSyncState.spaceModels
    if isinstance(ss.get("spaceModels"), list):
        ss["spaceModels"].extend([space_id, {"encodedCKRecordFields": None, "value": space_obj}])

    # 5-7. firebaseSyncState.syncData
    sync_data = (data.get("firebaseSyncState") or {}).get("syncData") or {}
    ordered_fb = sync_data.get("orderedSpaceIDs")
    if isinstance(ordered_fb, dict):
        ordered_fb.setdefault("value", []).append(space_id)
        ordered_fb["lastChangeDate"] = ts
        ordered_fb["lastChangedDevice"] = device_id
    if isinstance(sync_data.get("spaceModels"), list):
        sync_data["spaceModels"].extend(
            [space_id, {"id": space_id, "lastChangeDate": ts, "lastChangedDevice": device_id, "value": space_obj}]
        )
    if isinstance(sync_data.get("items"), list):
        for cid, cont in ((pinned_id, pinned_cont), (unpinned_id, unpinned_cont)):
            sync_data["items"].extend(
                [cid, {"id": cid, "lastChangeDate": ts, "lastChangedDevice": device_id, "value": cont}]
            )

    save_sidebar(data)
    return space_id


def delete_space(space_name_or_id: str) -> None:
    _ensure_arc_not_running()
    data = load_sidebar()
    create_backup()

    container = _container(data)
    spaces = container.get("spaces", [])

    space_obj = _find_space_obj(container, space_name_or_id)
    if space_obj is None:
        raise ArcError(f"Space not found: {space_name_or_id}")
    space_id = space_obj["id"]

    container_ids = [c for c in (space_obj.get("containerIDs") or []) if c not in ("pinned", "unpinned")]

    container["spaces"] = [
        it for it in spaces
        if not (isinstance(it, str) and it == space_id)
        and not (isinstance(it, dict) and it.get("id") == space_id)
    ]

    items = container.get("items", [])
    to_remove: set[str] = set(container_ids)
    for item in items:
        if isinstance(item, dict) and item.get("parentID") in container_ids:
            to_remove.add(item["id"])
    container["items"] = [
        it for it in items
        if not (isinstance(it, str) and it in to_remove)
        and not (isinstance(it, dict) and it.get("id") in to_remove)
    ]

    def _keeps_space(it: Any, *, id_key: str) -> bool:
        """False for the string ref or model entry that points at this space.

        ``id_key`` is a dotted path (``"id"`` or ``"value.id"``) into the entry.
        """
        if isinstance(it, str):
            return it != space_id
        node: Any = it
        for part in id_key.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return node != space_id

    def _keeps_item(it: Any) -> bool:
        """False for any string ref or item entry whose id is being removed."""
        if isinstance(it, str):
            return it not in to_remove
        return it.get("id") not in to_remove

    ss = data.get("sidebarSyncState") or {}
    ordered = ss.get("container", {}).get("value", {}).get("orderedSpaceIDs")
    if isinstance(ordered, list):
        ss["container"]["value"]["orderedSpaceIDs"] = [i for i in ordered if i != space_id]
    if isinstance(ss.get("spaceModels"), list):
        # sidebarSyncState models nest the id under value.id
        ss["spaceModels"] = [it for it in ss["spaceModels"] if _keeps_space(it, id_key="value.id")]

    sync_data = (data.get("firebaseSyncState") or {}).get("syncData") or {}
    ordered_fb = sync_data.get("orderedSpaceIDs")
    if isinstance(ordered_fb, dict) and isinstance(ordered_fb.get("value"), list):
        ordered_fb["value"] = [i for i in ordered_fb["value"] if i != space_id]
    if isinstance(sync_data.get("spaceModels"), list):
        sync_data["spaceModels"] = [it for it in sync_data["spaceModels"] if _keeps_space(it, id_key="id")]
    if isinstance(sync_data.get("items"), list):
        sync_data["items"] = [it for it in sync_data["items"] if _keeps_item(it)]

    save_sidebar(data)


def add_tab(space_name_or_id: str, url: str, title: str | None = None, pinned: bool = False) -> str:
    """Add a tab to a space. Returns the new tab id."""
    url = _validate_url(url)
    _ensure_arc_not_running()
    data = load_sidebar()
    create_backup()

    container = _container(data)
    space_obj = _find_space_obj(container, space_name_or_id)
    if space_obj is None:
        raise ArcError(f"Space not found: {space_name_or_id}")
    space_id = space_obj["id"]
    container_ids = [c for c in (space_obj.get("containerIDs") or []) if c not in ("pinned", "unpinned")]

    container_id = container_ids[0] if pinned else (container_ids[1] if len(container_ids) > 1 else None)
    if not container_id:
        raise ArcError(f"Container not found for space: {space_id}")

    tab_id = _new_id()
    ts = _now_apple()
    device_id = _get_device_id(data)

    tab_obj = {
        "id": tab_id,
        "title": None,
        "parentID": container_id,
        "childrenIds": [],
        "createdAt": ts,
        "originatingDevice": device_id,
        "isUnread": False,
        "data": {
            "tab": {
                "savedURL": url,
                "savedTitle": title or url,
                "savedMuteStatus": "allowAudio",
                "timeLastActiveAt": ts,
            }
        },
    }

    items = container.setdefault("items", [])
    items.extend([tab_id, tab_obj])
    for item in items:
        if isinstance(item, dict) and item.get("id") == container_id:
            item.setdefault("childrenIds", []).append(tab_id)
            break

    sync_data = (data.get("firebaseSyncState") or {}).get("syncData") or {}
    if isinstance(sync_data.get("items"), list):
        sync_data["items"].extend(
            [tab_id, {"id": tab_id, "lastChangeDate": ts, "lastChangedDevice": device_id, "value": tab_obj}]
        )

    save_sidebar(data)
    return tab_id


def delete_tab(tab_id: str) -> None:
    _ensure_arc_not_running()
    data = load_sidebar()
    create_backup()

    container = _container(data)
    items = container.get("items", [])

    parent_id: str | None = None
    found = False
    for item in items:
        if isinstance(item, dict) and item.get("id") == tab_id:
            parent_id = item.get("parentID")
            found = True
            break
    if not found:
        raise ArcError(f"Tab not found: {tab_id}")

    container["items"] = [
        it for it in items
        if not (isinstance(it, str) and it == tab_id)
        and not (isinstance(it, dict) and it.get("id") == tab_id)
    ]

    if parent_id:
        for item in container["items"]:
            if isinstance(item, dict) and item.get("id") == parent_id and item.get("childrenIds"):
                item["childrenIds"] = [i for i in item["childrenIds"] if i != tab_id]
                break

    def _keeps(it: Any) -> bool:
        return (it != tab_id) if isinstance(it, str) else (it.get("id") != tab_id)

    sync_data = (data.get("firebaseSyncState") or {}).get("syncData") or {}
    if isinstance(sync_data.get("items"), list):
        sync_data["items"] = [it for it in sync_data["items"] if _keeps(it)]

    save_sidebar(data)


def create_workspace(
    name: str, urls: list[dict[str, Any]], icon: str = "🔧"
) -> tuple[str, list[str]]:
    """Create a space and populate it with tabs. Returns (space_id, tab_ids)."""
    _ensure_arc_not_running()
    # Validate everything up front so we never create a half-built space.
    for i, entry in enumerate(urls):
        if "url" not in entry:
            raise ArcError(f"urls[{i}] is missing a 'url' field.")
        _validate_url(entry["url"])

    space_id = create_space(name, icon)
    tab_ids = [add_tab(space_id, entry["url"], entry.get("title"), False) for entry in urls]
    return space_id, tab_ids

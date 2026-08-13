"""Inventory snapshots and retention pruning."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Pane
from .policy import iso
from .store import AtticHome


def append_inventory(
    home: AtticHome, panes: list[Pane], labels: dict[str, str], now: datetime
) -> Path:
    home.ensure()
    entry = {
        "at": iso(now),
        "panes": [
            {
                "pane_id": p.pane_id,
                "workspace": labels.get(p.workspace_id, p.workspace_id),
                "cwd": p.cwd,
                "title": p.title,
                "status": p.agent_status,
                "session_uuid": p.session_uuid,
            }
            for p in panes
        ],
    }
    path = home.inventory_dir / f"{now.strftime('%Y-%m-%d')}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        os.chmod(path, 0o600)
        fh.write(json.dumps(entry) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return path


def prune_inventory(home: AtticHome, now: datetime, retention_days: int) -> list[Path]:
    cutoff = now - timedelta(days=retention_days)
    removed: list[Path] = []
    if not home.inventory_dir.exists():
        return removed
    for path in sorted(home.inventory_dir.glob("*.jsonl")):
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if day < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def _archived_at(path: Path) -> datetime | None:
    """The manifest's `archived_at`, or None if the manifest is missing, unreadable,
    or malformed in any way.

    A manifest we cannot trust is treated as no manifest at all. Every rejection
    below is a real crash this would otherwise cause inside the project's only
    irreversible operation, running unattended:
      - non-dict JSON -> data["archived_at"] raises TypeError
      - non-str stamp -> .replace() raises AttributeError
      - naive stamp   -> comparing naive to aware raises TypeError at the caller
    """
    try:
        data = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    stamp = data.get("archived_at")
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def prune_archives(home: AtticHome, now: datetime, retention_days: int) -> list[Path]:
    cutoff = now - timedelta(days=retention_days)
    removed: list[Path] = []
    if not home.archive_dir.exists():
        return removed
    for path in sorted(p for p in home.archive_dir.iterdir() if p.is_dir()):
        stamp = _archived_at(path)
        if stamp is None:
            # No usable manifest: an orphaned partial archive, invisible to
            # `attic list` and otherwise immortal. Reclaim it, but only once its
            # mtime is past retention — nothing legitimate is manifest-less and
            # 30 days old, and the age bar guarantees an in-flight write is never
            # touched.
            try:
                stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                continue
        if stamp < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():       # report only what actually went away
                removed.append(path)
    return removed

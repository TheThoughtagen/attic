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


def prune_archives(home: AtticHome, now: datetime, retention_days: int) -> list[Path]:
    cutoff = now - timedelta(days=retention_days)
    removed: list[Path] = []
    if not home.archive_dir.exists():
        return removed
    for path in sorted(p for p in home.archive_dir.iterdir() if p.is_dir()):
        manifest = path / "manifest.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            archived_at = datetime.fromisoformat(data["archived_at"].replace("Z", "+00:00"))
        except (OSError, ValueError, KeyError):
            # No readable manifest: such a directory is invisible to `attic list`
            # and would otherwise be immortal. Reclaim it, but only once its mtime
            # is past retention — nothing legitimate is manifest-less and 30 days
            # old, and the age bar guarantees an in-flight write is never touched.
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path)
            continue
        if archived_at < cutoff:
            shutil.rmtree(path)
            removed.append(path)
    return removed

"""Filesystem layout, configuration, and idle-state persistence."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Config:
    idle_threshold_hours: float = 4.0
    per_tick_cap: int = 3
    archive_retention_days: int = 30
    inventory_retention_days: int = 90
    herdr_protocol: int = 19


@dataclass
class PaneState:
    first_idle_at: str | None
    last_revision: int
    # Exemptions. `snooze_until` expires on its own so a forgotten snooze cannot
    # become a permanent leak; `pinned` requires an explicit unpin.
    snooze_until: str | None = None
    pinned: bool = False


class AtticHome:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.config_path = self.root / "config.json"
        self.state_path = self.root / "state.json"
        self.pause_path = self.root / "PAUSE"
        self.inventory_dir = self.root / "inventory"
        self.archive_dir = self.root / "archive"
        self.index_path = self.archive_dir / "index.jsonl"
        self.log_path = self.root / "logs" / "attic.log"
        self.legacy_dir = self.root / "legacy"

    @classmethod
    def default(cls) -> "AtticHome":
        env = os.environ.get("ATTIC_HOME")
        return cls(Path(env) if env else Path.home() / ".attic")

    def ensure(self) -> None:
        # 0700 throughout: archives hold raw terminal scrollback and the inventory
        # records every repo path you had open. Owner-only, not default umask.
        for d in (
            self.root, self.inventory_dir, self.archive_dir,
            self.log_path.parent, self.legacy_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)

    def is_paused(self) -> bool:
        return self.pause_path.exists()

    def load_config(self) -> Config:
        known = {f.name for f in fields(Config)}
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Config()
        if not isinstance(raw, dict):
            return Config()
        return Config(**{k: v for k, v in raw.items() if k in known})

    def load_state(self) -> dict[str, PaneState]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, PaneState] = {}
        for key, val in raw.items():
            # A malformed entry is dropped, not fatal: losing one pane's idle clock
            # restarts it, which delays archiving. Raising would kill the whole
            # unattended tick.
            if not isinstance(val, dict):
                continue
            first_idle_at = val.get("first_idle_at")
            if first_idle_at is not None:
                if not isinstance(first_idle_at, str):
                    continue
                # Same validation inventory._archived_at applies: a stamp that fails
                # to parse, or parses but is naive, would otherwise survive here and
                # reach policy._parse -> decide(), raising TypeError/ValueError
                # outside run_tick's `except HerdrError` and killing every future
                # tick. Dropping the entry only restarts that pane's idle clock.
                try:
                    parsed = datetime.fromisoformat(first_idle_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    continue
            try:
                last_revision = int(val.get("last_revision", 0))
            except (TypeError, ValueError):
                continue
            snooze_until = val.get("snooze_until")
            if snooze_until is not None:
                if not isinstance(snooze_until, str):
                    continue
                try:
                    parsed = datetime.fromisoformat(snooze_until.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    continue          # naive stamps raise at comparison time
            pinned = val.get("pinned", False)
            if not isinstance(pinned, bool):
                continue
            out[key] = PaneState(
                first_idle_at=first_idle_at,
                last_revision=last_revision,
                snooze_until=snooze_until,
                pinned=pinned,
            )
        return out

    def save_state(self, state: dict[str, PaneState]) -> None:
        self.ensure()
        payload = {
            k: {
                "first_idle_at": v.first_idle_at,
                "last_revision": v.last_revision,
                "snooze_until": v.snooze_until,
                "pinned": v.pinned,
            }
            for k, v in state.items()
        }
        tmp = self.state_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)

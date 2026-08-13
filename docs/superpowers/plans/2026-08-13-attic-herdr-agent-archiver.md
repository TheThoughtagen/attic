# attic — herdr agent archiver: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a tool that snapshots herdr's pane inventory on a timer, and archives-then-closes idle Claude agent panes so they can be restored later, reclaiming memory without losing work.

**Architecture:** A `uv`-managed Python package. All herdr interaction is confined to a `HerdrClient` class so the dangerous paths can be tested with a fake. All reaping policy lives in a pure `decide()` function with no I/O or clock. A LaunchAgent runs `attic tick` every 5 minutes; `tick` always snapshots, and reaps only when every safety guard passes.

**Tech Stack:** Python 3.11+ (stdlib only at runtime), `uv` for env management, `pytest` for tests, macOS `launchd` for scheduling, `herdr` 0.8.0 (socket API protocol 19) as the data source.

**Spec:** `docs/superpowers/specs/2026-08-13-attic-herdr-agent-archiver-design.md`

## Deviation from the spec — read before starting

The spec specifies "a single-file Python script, stdlib-only, with a PEP 723 inline
metadata header, invoked via `uv run --script`." **This plan uses a `uv` project with a
`src/attic/` package instead.**

Reason: the spec's own testing strategy requires injecting a fake `HerdrClient` and
importing `decide()` in isolation. A PEP 723 single script is awkward to import from a
test suite and would grow past 600 lines across seven responsibilities. The *constraint*
behind that spec decision — no system pip, no manual venv, hermetic execution via `uv` —
is fully preserved: `uv run attic` and `uv run pytest` need no activation step and touch
no system interpreter.

Nothing else in the spec changes. If the user prefers the literal single file, stop and
raise it before Task 1.

## Global Constraints

- **Never `pip install` into system/global Python.** All Python execution goes through `uv`.
- **Runtime dependencies: stdlib only.** `pytest` is a dev-group dependency and must not be imported by `src/attic/`.
- **Python 3.11+** (uses `X | Y` type syntax and `datetime.UTC`).
- **Archive before close, always.** No code path may call `pane_close()` unless an archive directory with a manifest has been written and fsynced.
- **`tick` always exits 0.** A reaper that crashes its own timer silently stops protecting the user.
- **All timestamps are timezone-aware UTC**, serialized as ISO 8601 with `Z`.
- **Runtime data lives at `~/.attic/`** (override with `ATTIC_HOME`), never inside the repo.
- **herdr protocol pin: 19.** Mismatch means snapshot-only, never reap.
- **Reap policy values:** idle threshold 4 hours, per-tick cap 3, archive retention 30 days, inventory retention 90 days, tick interval 5 minutes.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | uv project, `attic` entry point, pytest dev group |
| `src/attic/models.py` | `Pane` dataclass and parsing of herdr's `pane list` JSON |
| `src/attic/store.py` | `Config`, `PaneState`, `AtticHome` paths, atomic state persistence |
| `src/attic/policy.py` | Pure `update_state()` and `decide()` — all reap policy |
| `src/attic/herdr.py` | `HerdrClient` — the only code that shells out to herdr |
| `src/attic/archive.py` | `slugify`, `make_archive_id`, `Archiver.archive()` with fsync |
| `src/attic/inventory.py` | Inventory line append and retention pruning |
| `src/attic/cli.py` | `tick`, `reap --dry-run`, `list`, `show`, `restore`, `prune` |
| `tests/fixtures/pane_list_sample.json` | Real captured herdr output |
| `tests/fakes.py` | `FakeHerdrClient` with programmable failures |
| `launchd/com.you.attic.plist` | 5-minute timer |

---

## Task 1: Project scaffold and pane parsing

**Files:**
- Create: `pyproject.toml`, `src/attic/__init__.py`, `src/attic/models.py`
- Create: `tests/fixtures/pane_list_sample.json`, `tests/test_models.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Pane` frozen dataclass with fields `pane_id: str`, `terminal_id: str`, `workspace_id: str`, `tab_id: str`, `agent: str | None`, `agent_status: str`, `session_uuid: str | None`, `cwd: str`, `title: str`, `focused: bool`, `revision: int`, `scroll_rows: int`; and `parse_pane_list(payload: dict) -> list[Pane]`

- [ ] **Step 1: Create the uv project**

Create `pyproject.toml`:

```toml
[project]
name = "attic"
version = "0.1.0"
description = "Archive idle herdr agent panes before reclaiming them"
requires-python = ">=3.11"
dependencies = []

[project.scripts]
attic = "attic.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/attic"]

[dependency-groups]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
markers = ["integration: requires a live herdr server (deselect with -m 'not integration')"]
addopts = "-m 'not integration'"
```

Create empty `src/attic/__init__.py`.

- [ ] **Step 2: Install the fixture**

Move the sample captured during design into the test fixtures directory:

```bash
mkdir -p tests/fixtures
git mv docs/superpowers/specs/pane-list-sample.json tests/fixtures/pane_list_sample.json
```

This file is real `herdr pane list` output. Do not hand-edit it — its quirks are the point.

- [ ] **Step 3: Write the failing test**

Create `tests/test_models.py`:

```python
import json
from pathlib import Path

from attic.models import Pane, parse_pane_list

FIXTURE = Path(__file__).parent / "fixtures" / "pane_list_sample.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parses_every_pane_from_real_herdr_output():
    panes = parse_pane_list(load_fixture())
    assert len(panes) == 10


def test_agent_pane_carries_session_uuid():
    panes = {p.pane_id: p for p in parse_pane_list(load_fixture())}
    p = panes["w4:p2"]
    assert p.agent == "claude"
    assert p.session_uuid == "55555555-5555-4555-8555-555555555555"
    assert p.agent_status == "idle"
    assert p.cwd == "/Users/you/data/projects/analytics"
    assert p.title == "Debug batch transaction group logging in production"
    assert p.terminal_id == "term_658ed00535c1118"


def test_non_agent_pane_has_no_agent_or_session():
    panes = {p.pane_id: p for p in parse_pane_list(load_fixture())}
    p = panes["w3:p8"]          # an `nvim .` pane
    assert p.agent is None
    assert p.session_uuid is None
    assert p.agent_status == "unknown"


def test_scroll_rows_is_buffer_plus_viewport():
    panes = {p.pane_id: p for p in parse_pane_list(load_fixture())}
    p = panes["w3:p1"]          # max_offset_from_bottom 5839, viewport_rows 91
    assert p.scroll_rows == 5930


def test_pane_is_frozen():
    p = parse_pane_list(load_fixture())[0]
    try:
        p.revision = 1
    except Exception as exc:
        assert "frozen" in str(exc).lower() or isinstance(exc, AttributeError)
    else:
        raise AssertionError("Pane must be immutable")
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'attic.models'`

- [ ] **Step 5: Write the implementation**

Create `src/attic/models.py`:

```python
"""Parsing of herdr's `pane list` JSON into immutable Pane records."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pane:
    pane_id: str
    terminal_id: str
    workspace_id: str
    tab_id: str
    agent: str | None
    agent_status: str
    session_uuid: str | None
    cwd: str
    title: str
    focused: bool
    revision: int
    scroll_rows: int

    @property
    def is_agent(self) -> bool:
        return self.agent is not None

    @classmethod
    def from_json(cls, obj: dict) -> "Pane":
        session = obj.get("agent_session") or {}
        scroll = obj.get("scroll") or {}
        return cls(
            pane_id=obj["pane_id"],
            terminal_id=obj.get("terminal_id", obj["pane_id"]),
            workspace_id=obj.get("workspace_id", ""),
            tab_id=obj.get("tab_id", ""),
            agent=obj.get("agent"),
            agent_status=obj.get("agent_status", "unknown"),
            session_uuid=session.get("value"),
            cwd=obj.get("cwd", ""),
            title=obj.get("terminal_title_stripped") or obj.get("terminal_title", ""),
            focused=bool(obj.get("focused", False)),
            revision=int(obj.get("revision", 0)),
            scroll_rows=int(scroll.get("max_offset_from_bottom", 0))
            + int(scroll.get("viewport_rows", 0)),
        )


def parse_pane_list(payload: dict) -> list[Pane]:
    """Accept either the full CLI envelope or a bare {"panes": [...]} object."""
    node = payload.get("result", payload)
    return [Pane.from_json(p) for p in node.get("panes", [])]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/attic tests/
git commit -m "feat: parse herdr pane list into immutable Pane records"
```

---

## Task 2: Config, state, and the attic home

**Files:**
- Create: `src/attic/store.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Config` frozen dataclass (`idle_threshold_hours: float = 4.0`, `per_tick_cap: int = 3`, `archive_retention_days: int = 30`, `inventory_retention_days: int = 90`, `herdr_protocol: int = 19`); `PaneState` dataclass (`first_idle_at: str | None`, `last_revision: int`); `AtticHome` with attributes `root`, `config_path`, `state_path`, `pause_path`, `inventory_dir`, `archive_dir`, `index_path`, `log_path`, `legacy_dir`, and methods `ensure() -> None`, `load_config() -> Config`, `load_state() -> dict[str, PaneState]`, `save_state(dict[str, PaneState]) -> None`, `is_paused() -> bool`, `default() -> AtticHome` (classmethod, honors `ATTIC_HOME`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_store.py`:

```python
import json

from attic.store import AtticHome, Config, PaneState


def test_default_config_matches_spec(tmp_path):
    home = AtticHome(tmp_path)
    cfg = home.load_config()
    assert cfg.idle_threshold_hours == 4.0
    assert cfg.per_tick_cap == 3
    assert cfg.archive_retention_days == 30
    assert cfg.inventory_retention_days == 90
    assert cfg.herdr_protocol == 19


def test_config_file_overrides_defaults_partially(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    home.config_path.write_text(json.dumps({"idle_threshold_hours": 1.5}))
    cfg = home.load_config()
    assert cfg.idle_threshold_hours == 1.5
    assert cfg.per_tick_cap == 3


def test_unknown_config_keys_are_ignored(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    home.config_path.write_text(json.dumps({"nonsense": 1, "per_tick_cap": 7}))
    assert home.load_config().per_tick_cap == 7


def test_corrupt_config_falls_back_to_defaults(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    home.config_path.write_text("{not json")
    assert home.load_config().idle_threshold_hours == 4.0


def test_state_roundtrips_keyed_by_terminal_id(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    home.save_state({"term_abc": PaneState(first_idle_at="2026-08-13T00:00:00Z", last_revision=12)})
    loaded = home.load_state()
    assert loaded["term_abc"].first_idle_at == "2026-08-13T00:00:00Z"
    assert loaded["term_abc"].last_revision == 12


def test_missing_state_file_is_empty_not_an_error(tmp_path):
    assert AtticHome(tmp_path).load_state() == {}


def test_corrupt_state_falls_back_to_empty(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    home.state_path.write_text("{not json")
    assert home.load_state() == {}


def test_save_state_is_atomic_leaving_no_tempfile(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    home.save_state({"term_abc": PaneState(None, 1)})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("state.json.")]
    assert leftovers == []


def test_pause_file_detection(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    assert home.is_paused() is False
    home.pause_path.touch()
    assert home.is_paused() is True


def test_default_home_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path / "custom"))
    assert AtticHome.default().root == tmp_path / "custom"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'attic.store'`

- [ ] **Step 3: Write the implementation**

Create `src/attic/store.py`:

```python
"""Filesystem layout, configuration, and idle-state persistence."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
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
        for d in (self.root, self.inventory_dir, self.archive_dir, self.log_path.parent):
            d.mkdir(parents=True, exist_ok=True)

    def is_paused(self) -> bool:
        return self.pause_path.exists()

    def load_config(self) -> Config:
        known = {f.name for f in fields(Config)}
        try:
            raw = json.loads(self.config_path.read_text())
        except (OSError, ValueError):
            return Config()
        if not isinstance(raw, dict):
            return Config()
        return Config(**{k: v for k, v in raw.items() if k in known})

    def load_state(self) -> dict[str, PaneState]:
        try:
            raw = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, PaneState] = {}
        for key, val in raw.items():
            if isinstance(val, dict):
                out[key] = PaneState(
                    first_idle_at=val.get("first_idle_at"),
                    last_revision=int(val.get("last_revision", 0)),
                )
        return out

    def save_state(self, state: dict[str, PaneState]) -> None:
        self.ensure()
        payload = {
            k: {"first_idle_at": v.first_idle_at, "last_revision": v.last_revision}
            for k, v in state.items()
        }
        tmp = self.state_path.with_suffix(".json.tmp")
        with open(tmp, "w") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/attic/store.py tests/test_store.py
git commit -m "feat: add attic home layout, config, and atomic state persistence"
```

---

## Task 3: The reap policy (pure)

This is the safety-critical task. Every rule from the spec's "Reap policy" and "Safety guards" sections is enforced here, with no I/O.

**Files:**
- Create: `src/attic/policy.py`, `tests/test_policy.py`

**Interfaces:**
- Consumes: `Pane` (Task 1), `Config`, `PaneState` (Task 2)
- Produces: `Archive` frozen dataclass (`pane: Pane`, `idle_since: datetime`); `Skip` frozen dataclass (`pane: Pane`, `reason: str`); `Action = Archive | Skip`; `update_state(panes, state, now) -> dict[str, PaneState]`; `decide(panes, state, now, config) -> list[Action]`

**Call order contract:** `tick` must call `update_state()` first, then pass its result to `decide()`. `update_state` maintains the idle clock; `decide` only reads it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_policy.py`:

```python
from datetime import datetime, timedelta, timezone

from attic.models import Pane
from attic.policy import Archive, Skip, decide, update_state
from attic.store import Config, PaneState

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
CFG = Config()


def mkpane(pane_id="w1:p1", *, status="idle", agent="claude", uuid="u-1",
           focused=False, revision=1, terminal_id=None) -> Pane:
    return Pane(
        pane_id=pane_id,
        terminal_id=terminal_id or f"term_{pane_id}",
        workspace_id="w1", tab_id="w1:t1",
        agent=agent, agent_status=status, session_uuid=uuid,
        cwd="/tmp/repo", title="Some task", focused=focused,
        revision=revision, scroll_rows=100,
    )


def idle_since(hours: float, revision: int = 1) -> PaneState:
    return PaneState(
        first_idle_at=(NOW - timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
        last_revision=revision,
    )


def archived(actions) -> list[str]:
    return [a.pane.pane_id for a in actions if isinstance(a, Archive)]


def skip_reason(actions, pane_id: str) -> str:
    return next(a.reason for a in actions
                if isinstance(a, Skip) and a.pane.pane_id == pane_id)


# --- update_state ---------------------------------------------------------

def test_idle_pane_starts_the_clock():
    st = update_state([mkpane()], {}, NOW)
    assert st["term_w1:p1"].first_idle_at == "2026-08-13T12:00:00Z"


def test_revision_change_resets_the_clock():
    prior = {"term_w1:p1": idle_since(10, revision=5)}
    st = update_state([mkpane(revision=6)], prior, NOW)
    assert st["term_w1:p1"].first_idle_at == "2026-08-13T12:00:00Z"
    assert st["term_w1:p1"].last_revision == 6


def test_stable_revision_preserves_the_clock():
    prior = {"term_w1:p1": idle_since(10, revision=5)}
    st = update_state([mkpane(revision=5)], prior, NOW)
    assert st["term_w1:p1"].first_idle_at == "2026-08-13T02:00:00Z"


def test_non_idle_status_clears_the_clock():
    prior = {"term_w1:p1": idle_since(10)}
    st = update_state([mkpane(status="working")], prior, NOW)
    assert st["term_w1:p1"].first_idle_at is None


def test_vanished_panes_are_dropped_from_state():
    prior = {"term_gone": idle_since(10)}
    assert "term_gone" not in update_state([mkpane()], prior, NOW)


def test_recycled_pane_id_gets_a_fresh_clock():
    # Same pane_id, different terminal_id => must not inherit the old clock.
    prior = {"term_old": idle_since(10, revision=5)}
    st = update_state([mkpane("w4:p2", terminal_id="term_new")], prior, NOW)
    assert st["term_new"].first_idle_at == "2026-08-13T12:00:00Z"
    assert "term_old" not in st


# --- decide ---------------------------------------------------------------

def test_idle_past_threshold_is_archived():
    st = {"term_w1:p1": idle_since(5)}
    assert archived(decide([mkpane()], st, NOW, CFG)) == ["w1:p1"]


def test_idle_under_threshold_is_skipped():
    st = {"term_w1:p1": idle_since(3)}
    actions = decide([mkpane()], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "not idle long enough"


def test_working_pane_is_never_archived():
    st = {"term_w1:p1": idle_since(10)}
    actions = decide([mkpane(status="working")], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "status is working"


def test_blocked_pane_is_never_archived_at_any_age():
    st = {"term_w1:p1": idle_since(1000)}
    actions = decide([mkpane(status="blocked")], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "status is blocked"


def test_focused_pane_is_never_archived():
    st = {"term_w1:p1": idle_since(10)}
    actions = decide([mkpane(focused=True)], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "focused"


def test_pane_without_session_uuid_is_never_archived():
    st = {"term_w1:p1": idle_since(10)}
    actions = decide([mkpane(uuid=None)], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "no session uuid"


def test_non_agent_pane_is_never_archived():
    st = {"term_w1:p1": idle_since(10)}
    actions = decide([mkpane(agent=None, uuid=None, status="unknown")], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "not an agent pane"


def test_pane_with_no_recorded_clock_is_skipped():
    actions = decide([mkpane()], {}, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "idle clock not started"


def test_per_tick_cap_archives_longest_idle_first():
    panes, st = [], {}
    for i, hours in enumerate([5, 20, 9, 6, 30]):
        p = mkpane(f"w1:p{i}")
        panes.append(p)
        st[p.terminal_id] = idle_since(hours)
    actions = decide(panes, st, NOW, CFG)
    assert archived(actions) == ["w1:p4", "w1:p1", "w1:p2"]   # 30h, 20h, 9h
    assert skip_reason(actions, "w1:p3") == "per-tick cap reached"


def test_every_pane_receives_a_verdict():
    panes = [mkpane("w1:p1"), mkpane("w1:p2", status="working"), mkpane("w1:p3", agent=None)]
    actions = decide(panes, {}, NOW, CFG)
    assert len(actions) == 3
    assert {a.pane.pane_id for a in actions} == {"w1:p1", "w1:p2", "w1:p3"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'attic.policy'`

- [ ] **Step 3: Write the implementation**

Create `src/attic/policy.py`:

```python
"""Pure reap policy. No I/O, no clock, no herdr."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import Pane
from .store import Config, PaneState

REAPABLE_STATUS = "idle"


@dataclass(frozen=True)
class Archive:
    pane: Pane
    idle_since: datetime


@dataclass(frozen=True)
class Skip:
    pane: Pane
    reason: str


Action = Archive | Skip


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def update_state(
    panes: list[Pane], state: dict[str, PaneState], now: datetime
) -> dict[str, PaneState]:
    """Maintain the idle clock. Panes that vanished are dropped."""
    updated: dict[str, PaneState] = {}
    for pane in panes:
        prior = state.get(pane.terminal_id)
        if pane.agent_status != REAPABLE_STATUS:
            updated[pane.terminal_id] = PaneState(None, pane.revision)
            continue
        if prior is None or prior.last_revision != pane.revision or prior.first_idle_at is None:
            updated[pane.terminal_id] = PaneState(iso(now), pane.revision)
        else:
            updated[pane.terminal_id] = PaneState(prior.first_idle_at, pane.revision)
    return updated


def _verdict(pane: Pane, state: dict[str, PaneState], now, config) -> Skip | datetime:
    """Return a Skip, or the datetime the pane went idle if it qualifies."""
    if not pane.is_agent:
        return Skip(pane, "not an agent pane")
    if pane.agent_status != REAPABLE_STATUS:
        return Skip(pane, f"status is {pane.agent_status}")
    if not pane.session_uuid:
        return Skip(pane, "no session uuid")
    if pane.focused:
        return Skip(pane, "focused")
    entry = state.get(pane.terminal_id)
    if entry is None or entry.first_idle_at is None:
        return Skip(pane, "idle clock not started")
    since = _parse(entry.first_idle_at)
    if (now - since).total_seconds() < config.idle_threshold_hours * 3600:
        return Skip(pane, "not idle long enough")
    return since


def decide(
    panes: list[Pane], state: dict[str, PaneState], now: datetime, config: Config
) -> list[Action]:
    """Return one verdict per pane, preserving input order."""
    verdicts: dict[str, Skip | datetime] = {
        p.pane_id: _verdict(p, state, now, config) for p in panes
    }
    eligible = sorted(
        (p for p in panes if isinstance(verdicts[p.pane_id], datetime)),
        key=lambda p: verdicts[p.pane_id],           # oldest idle first
    )
    approved = {p.pane_id for p in eligible[: config.per_tick_cap]}

    actions: list[Action] = []
    for pane in panes:
        v = verdicts[pane.pane_id]
        if isinstance(v, Skip):
            actions.append(v)
        elif pane.pane_id in approved:
            actions.append(Archive(pane, v))
        else:
            actions.append(Skip(pane, "per-tick cap reached"))
    return actions
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_policy.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add src/attic/policy.py tests/test_policy.py
git commit -m "feat: add pure reap policy with idle clock and safety rules"
```

---

## Task 4: HerdrClient and its test double

**Files:**
- Create: `src/attic/herdr.py`, `tests/fakes.py`, `tests/test_herdr.py`

**Interfaces:**
- Consumes: `Pane`, `parse_pane_list` (Task 1)
- Produces: `HerdrError(Exception)`; `HerdrClient` with `protocol() -> int`, `pane_list() -> list[Pane]`, `snapshot() -> dict`, `workspace_labels() -> dict[str, str]`, `pane_read(pane_id: str, lines: int) -> str`, `pane_close(pane_id: str) -> None`, `tab_create(cwd: str, label: str) -> str` (returns new `pane_id`), `pane_run(pane_id: str, command: list[str]) -> None`. Also `tests/fakes.py::FakeHerdrClient` with the same surface plus `fail_read: set[str]`, `fail_close: set[str]`, `closed: list[str]`, `empty_read: set[str]`.

**Verified CLI shapes** (confirmed against herdr 0.8.0 during design — do not guess):

| Method | Command |
|---|---|
| `protocol` | `herdr status` → parse the `protocol:` line under `server:` |
| `pane_list` | `herdr pane list` → JSON envelope `{"result": {"panes": [...]}}` |
| `snapshot` | `herdr api snapshot` → JSON |
| `workspace_labels` | `herdr workspace list` → `result.workspaces[].{workspace_id,label}` |
| `pane_read` | `herdr pane read <id> --source recent-unwrapped --lines <N> --format text` |
| `pane_close` | `herdr pane close <id>` |
| `tab_create` | `herdr tab create --cwd <path> --label <text> --focus` |
| `pane_run` | `herdr pane run <pane_id> <command...>` |

- [ ] **Step 1: Write the failing test**

Create `tests/test_herdr.py`:

```python
import json

import pytest

from attic.herdr import HerdrClient, HerdrError


class Recorder:
    """Stands in for subprocess: records argv, returns canned stdout."""

    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def test_protocol_parses_server_block():
    run = Recorder(["client:\n  protocol: 18\n\nserver:\n  status: running\n  protocol: 19\n"])
    assert HerdrClient(runner=run).protocol() == 19


def test_pane_list_returns_parsed_panes():
    payload = {"result": {"panes": [
        {"pane_id": "w1:p1", "terminal_id": "t1", "agent": "claude",
         "agent_status": "idle", "agent_session": {"value": "u-1"},
         "cwd": "/tmp", "terminal_title_stripped": "x", "revision": 3,
         "scroll": {"max_offset_from_bottom": 10, "viewport_rows": 5}}]}}
    client = HerdrClient(runner=Recorder([json.dumps(payload)]))
    panes = client.pane_list()
    assert [p.pane_id for p in panes] == ["w1:p1"]
    assert panes[0].scroll_rows == 15


def test_pane_read_sizes_the_request_and_returns_text():
    run = Recorder(["hello scrollback"])
    assert HerdrClient(runner=run).pane_read("w1:p1", 250) == "hello scrollback"
    assert run.calls[0] == [
        "herdr", "pane", "read", "w1:p1",
        "--source", "recent-unwrapped", "--lines", "250", "--format", "text",
    ]


def test_workspace_labels_maps_id_to_label():
    payload = {"result": {"workspaces": [
        {"workspace_id": "w3", "label": "clients"},
        {"workspace_id": "w4", "label": "wh dev"}]}}
    labels = HerdrClient(runner=Recorder([json.dumps(payload)])).workspace_labels()
    assert labels == {"w3": "clients", "w4": "wh dev"}


def test_malformed_json_raises_herdr_error():
    with pytest.raises(HerdrError):
        HerdrClient(runner=Recorder(["{not json"])).pane_list()


def test_subprocess_failure_raises_herdr_error():
    with pytest.raises(HerdrError):
        HerdrClient(runner=Recorder([OSError("socket gone")])).pane_list()


def test_tab_create_returns_new_pane_id():
    payload = {"result": {"tab": {"tab_id": "w4:t9", "panes": [{"pane_id": "w4:p9"}]}}}
    client = HerdrClient(runner=Recorder([json.dumps(payload)]))
    assert client.tab_create("/tmp/repo", "Some task") == "w4:p9"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_herdr.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'attic.herdr'`

- [ ] **Step 3: Write the implementation**

Create `src/attic/herdr.py`:

```python
"""The only module that talks to herdr. Everything else takes this as a dependency."""

from __future__ import annotations

import json
import subprocess
from typing import Callable

from .models import Pane, parse_pane_list


class HerdrError(Exception):
    """Any failure to obtain a usable answer from herdr."""


def _subprocess_runner(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise OSError(f"{' '.join(argv)} exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


class HerdrClient:
    def __init__(self, runner: Callable[[list[str]], str] | None = None) -> None:
        self._run = runner or _subprocess_runner

    def _text(self, *args: str) -> str:
        try:
            return self._run(["herdr", *args])
        except Exception as exc:                      # OSError, timeout, missing binary
            raise HerdrError(f"herdr {' '.join(args)} failed: {exc}") from exc

    def _json(self, *args: str) -> dict:
        raw = self._text(*args)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise HerdrError(f"herdr {' '.join(args)} returned non-JSON: {raw[:200]!r}") from exc

    def protocol(self) -> int:
        """Parse the protocol line from the `server:` block of `herdr status`."""
        text = self._text("status")
        in_server = False
        for line in text.splitlines():
            if line.startswith("server:"):
                in_server = True
                continue
            if line and not line[0].isspace():
                in_server = False
            if in_server and "protocol:" in line:
                return int(line.split("protocol:")[1].strip())
        raise HerdrError("could not determine herdr server protocol")

    def pane_list(self) -> list[Pane]:
        return parse_pane_list(self._json("pane", "list"))

    def snapshot(self) -> dict:
        return self._json("api", "snapshot")

    def workspace_labels(self) -> dict[str, str]:
        node = self._json("workspace", "list").get("result", {})
        return {w["workspace_id"]: w.get("label", "") for w in node.get("workspaces", [])}

    def pane_read(self, pane_id: str, lines: int) -> str:
        return self._text(
            "pane", "read", pane_id,
            "--source", "recent-unwrapped", "--lines", str(lines), "--format", "text",
        )

    def pane_close(self, pane_id: str) -> None:
        self._text("pane", "close", pane_id)

    def tab_create(self, cwd: str, label: str) -> str:
        node = self._json("tab", "create", "--cwd", cwd, "--label", label, "--focus")
        tab = node.get("result", {}).get("tab", {})
        panes = tab.get("panes") or []
        if not panes:
            raise HerdrError("tab create returned no pane")
        return panes[0]["pane_id"]

    def pane_run(self, pane_id: str, command: list[str]) -> None:
        self._text("pane", "run", pane_id, *command)
```

- [ ] **Step 4: Write the fake used by every later task**

Create `tests/fakes.py`:

```python
"""Programmable HerdrClient double. Failures are opt-in per pane id."""

from __future__ import annotations

from attic.herdr import HerdrError
from attic.models import Pane


class FakeHerdrClient:
    def __init__(self, panes: list[Pane] | None = None, protocol: int = 19,
                 labels: dict[str, str] | None = None) -> None:
        self.panes = panes or []
        self._protocol = protocol
        self.labels = labels or {}
        self.scrollback = "line one\nline two\n"
        # Programmable failures, keyed by pane id:
        self.fail_read: set[str] = set()
        self.fail_close: set[str] = set()
        self.empty_read: set[str] = set()
        # Observations:
        self.closed: list[str] = []
        self.reads: list[tuple[str, int]] = []
        self.ran: list[tuple[str, list[str]]] = []
        self.created_tabs: list[tuple[str, str]] = []
        self.next_pane_id = "w9:p9"

    def protocol(self) -> int:
        return self._protocol

    def pane_list(self) -> list[Pane]:
        return list(self.panes)

    def snapshot(self) -> dict:
        return {"result": {"panes": [p.pane_id for p in self.panes]}}

    def workspace_labels(self) -> dict[str, str]:
        return dict(self.labels)

    def pane_read(self, pane_id: str, lines: int) -> str:
        self.reads.append((pane_id, lines))
        if pane_id in self.fail_read:
            raise HerdrError(f"simulated read failure for {pane_id}")
        if pane_id in self.empty_read:
            return ""
        return self.scrollback

    def pane_close(self, pane_id: str) -> None:
        if pane_id in self.fail_close:
            raise HerdrError(f"simulated close failure for {pane_id}")
        self.closed.append(pane_id)

    def tab_create(self, cwd: str, label: str) -> str:
        self.created_tabs.append((cwd, label))
        return self.next_pane_id

    def pane_run(self, pane_id: str, command: list[str]) -> None:
        self.ran.append((pane_id, command))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_herdr.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add src/attic/herdr.py tests/test_herdr.py tests/fakes.py
git commit -m "feat: add HerdrClient CLI wrapper and programmable test double"
```

---

## Task 5: The archiver — durability before destruction

This task implements the spec's central guarantee. The test that matters most is that a
failed read never results in a close.

**Files:**
- Create: `src/attic/archive.py`, `tests/test_archive.py`

**Interfaces:**
- Consumes: `Pane` (Task 1), `AtticHome` (Task 2), `Archive` action (Task 3), `HerdrClient` (Task 4)
- Produces: `slugify(title: str, maxlen: int = 48) -> str`; `make_archive_id(now: datetime, title: str, existing: set[str]) -> str`; `Archiver(home: AtticHome, client)` with `archive(action: Archive, workspace_label: str, now: datetime) -> Path | None` (returns `None` on any failure, having closed nothing) and `append_index(entry: dict) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_archive.py`:

```python
import json
from datetime import datetime, timedelta, timezone

from attic.archive import Archiver, make_archive_id, slugify
from attic.policy import Archive
from attic.store import AtticHome
from fakes import FakeHerdrClient
from test_policy import mkpane

NOW = datetime(2026, 8, 13, 15, 47, 0, tzinfo=timezone.utc)
IDLE_SINCE = NOW - timedelta(hours=5)


def setup(tmp_path, **kw):
    home = AtticHome(tmp_path)
    home.ensure()
    pane = mkpane("w4:p2")
    client = FakeHerdrClient(panes=[pane], labels={"w1": "wh dev"}, **kw)
    return home, client, Archive(pane, IDLE_SINCE)


def test_slugify_lowercases_and_collapses():
    assert slugify("Debug batch: transaction/group logging!") == \
        "debug-batch-transaction-group-logging"


def test_slugify_truncates_to_maxlen():
    assert len(slugify("x" * 200)) == 48


def test_slugify_handles_empty_title():
    assert slugify("") == "untitled"


def test_archive_id_shape():
    assert make_archive_id(NOW, "Some Task", set()) == "20260813T154700Z-some-task"


def test_archive_id_disambiguates_collisions():
    existing = {"20260813T154700Z-some-task"}
    assert make_archive_id(NOW, "Some Task", existing) == "20260813T154700Z-some-task-2"


def test_archive_writes_scrollback_and_manifest(tmp_path):
    home, client, action = setup(tmp_path)
    path = Archiver(home, client).archive(action, "wh dev", NOW)
    assert path is not None
    assert (path / "scrollback.txt").read_text() == "line one\nline two\n"
    m = json.loads((path / "manifest.json").read_text())
    assert m["session_uuid"] == "u-1"
    assert m["pane_id"] == "w4:p2"
    assert m["terminal_id"] == "term_w4:p2"
    assert m["workspace"] == "wh dev"
    assert m["cwd"] == "/tmp/repo"
    assert m["archived_at"] == "2026-08-13T15:47:00Z"
    assert m["idle_since"] == "2026-08-13T10:47:00Z"
    assert m["scrollback_lines"] == 2
    assert m["resume"] == "cd /tmp/repo && claude --resume u-1"


def test_archive_sizes_read_from_pane_scroll_rows(tmp_path):
    home, client, action = setup(tmp_path)
    Archiver(home, client).archive(action, "wh dev", NOW)
    assert client.reads == [("w4:p2", 100)]


def test_failed_read_writes_nothing_and_returns_none(tmp_path):
    home, client, action = setup(tmp_path)
    client.fail_read.add("w4:p2")
    assert Archiver(home, client).archive(action, "wh dev", NOW) is None
    assert list(home.archive_dir.glob("2026*")) == []
    assert client.closed == []


def test_empty_read_is_treated_as_failure(tmp_path):
    home, client, action = setup(tmp_path)
    client.empty_read.add("w4:p2")
    assert Archiver(home, client).archive(action, "wh dev", NOW) is None
    assert client.closed == []


def test_archive_never_closes_the_pane_itself(tmp_path):
    """Closing is the caller's job, and only on a non-None return."""
    home, client, action = setup(tmp_path)
    Archiver(home, client).archive(action, "wh dev", NOW)
    assert client.closed == []


def test_append_index_is_one_json_object_per_line(tmp_path):
    home, client, _ = setup(tmp_path)
    arch = Archiver(home, client)
    arch.append_index({"id": "a"})
    arch.append_index({"id": "b"})
    lines = home.index_path.read_text().strip().splitlines()
    assert [json.loads(x)["id"] for x in lines] == ["a", "b"]
```

- [ ] **Step 2: Make test helpers importable**

Create `tests/conftest.py` so `fakes` and `test_policy` import cleanly:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_archive.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'attic.archive'`

- [ ] **Step 4: Write the implementation**

Create `src/attic/archive.py`:

```python
"""Durable archives. Nothing here closes a pane; that is the caller's job,
and only when `archive()` returns a path."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from .herdr import HerdrError
from .policy import Archive, iso
from .store import AtticHome

SLUG_MAXLEN = 48


def slugify(title: str, maxlen: int = SLUG_MAXLEN) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (slug[:maxlen].rstrip("-")) or "untitled"


def make_archive_id(now: datetime, title: str, existing: set[str]) -> str:
    base = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{slugify(title)}"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _write_fsynced(path: Path, text: str) -> None:
    with open(path, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


class Archiver:
    def __init__(self, home: AtticHome, client) -> None:
        self.home = home
        self.client = client

    def archive(self, action: Archive, workspace_label: str, now: datetime) -> Path | None:
        """Write a durable archive. Return its path, or None if anything failed.

        A None return is a hard instruction to the caller: do not close this pane.
        """
        pane = action.pane
        try:
            scrollback = self.client.pane_read(pane.pane_id, max(pane.scroll_rows, 1))
        except HerdrError:
            return None
        if not scrollback.strip():
            return None

        self.home.ensure()
        existing = {p.name for p in self.home.archive_dir.iterdir() if p.is_dir()}
        archive_id = make_archive_id(now, pane.title, existing)
        path = self.home.archive_dir / archive_id

        manifest = {
            "id": archive_id,
            "pane_id": pane.pane_id,
            "terminal_id": pane.terminal_id,
            "workspace": workspace_label,
            "workspace_id": pane.workspace_id,
            "session_uuid": pane.session_uuid,
            "agent": pane.agent,
            "cwd": pane.cwd,
            "title": pane.title,
            "idle_since": iso(action.idle_since),
            "archived_at": iso(now),
            "scrollback_lines": len(scrollback.splitlines()),
            "resume": f"cd {pane.cwd} && claude --resume {pane.session_uuid}",
        }

        try:
            path.mkdir(parents=True)
            _write_fsynced(path / "scrollback.txt", scrollback)
            _write_fsynced(path / "manifest.json", json.dumps(manifest, indent=2))
            dir_fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            return None
        return path

    def append_index(self, entry: dict) -> None:
        self.home.ensure()
        with open(self.home.index_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_archive.py -v`
Expected: 11 passed

- [ ] **Step 6: Commit**

```bash
git add src/attic/archive.py tests/test_archive.py tests/conftest.py
git commit -m "feat: add archiver with fsynced writes and fail-closed semantics"
```

---

## Task 6: Inventory and pruning

**Files:**
- Create: `src/attic/inventory.py`, `tests/test_inventory.py`

**Interfaces:**
- Consumes: `Pane` (Task 1), `AtticHome`, `Config` (Task 2)
- Produces: `append_inventory(home, panes, labels, now) -> Path`; `prune_inventory(home, now, retention_days) -> list[Path]`; `prune_archives(home, now, retention_days) -> list[Path]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_inventory.py`:

```python
import json
from datetime import datetime, timezone

from attic.inventory import append_inventory, prune_archives, prune_inventory
from attic.store import AtticHome
from test_policy import mkpane

NOW = datetime(2026, 8, 13, 15, 47, 0, tzinfo=timezone.utc)


def test_inventory_line_records_every_pane(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    path = append_inventory(home, [mkpane("w4:p2")], {"w1": "wh dev"}, NOW)
    assert path.name == "2026-08-13.jsonl"
    entry = json.loads(path.read_text().strip())
    assert entry["at"] == "2026-08-13T15:47:00Z"
    assert entry["panes"][0] == {
        "pane_id": "w4:p2", "workspace": "wh dev", "cwd": "/tmp/repo",
        "title": "Some task", "status": "idle", "session_uuid": "u-1",
    }


def test_inventory_appends_within_the_same_day(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    append_inventory(home, [mkpane()], {}, NOW)
    path = append_inventory(home, [mkpane()], {}, NOW)
    assert len(path.read_text().strip().splitlines()) == 2


def test_prune_inventory_removes_files_past_retention(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    (home.inventory_dir / "2026-01-01.jsonl").write_text("{}\n")   # 224 days old
    (home.inventory_dir / "2026-08-12.jsonl").write_text("{}\n")   # 1 day old
    removed = prune_inventory(home, NOW, retention_days=90)
    assert [p.name for p in removed] == ["2026-01-01.jsonl"]
    assert (home.inventory_dir / "2026-08-12.jsonl").exists()


def test_prune_inventory_ignores_unparseable_names(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    (home.inventory_dir / "notes.txt").write_text("x")
    assert prune_inventory(home, NOW, retention_days=1) == []
    assert (home.inventory_dir / "notes.txt").exists()


def test_prune_archives_uses_manifest_archived_at(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    old = home.archive_dir / "20260101T000000Z-old"
    old.mkdir()
    (old / "manifest.json").write_text(json.dumps(
        {"archived_at": "2026-01-01T00:00:00Z", "title": "Old task"}))
    fresh = home.archive_dir / "20260812T000000Z-fresh"
    fresh.mkdir()
    (fresh / "manifest.json").write_text(json.dumps(
        {"archived_at": "2026-08-12T00:00:00Z", "title": "Fresh task"}))
    removed = prune_archives(home, NOW, retention_days=30)
    assert [p.name for p in removed] == ["20260101T000000Z-old"]
    assert not old.exists()
    assert fresh.exists()


def test_prune_archives_skips_dirs_without_manifest(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    (home.archive_dir / "20260101T000000Z-broken").mkdir()
    assert prune_archives(home, NOW, retention_days=1) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_inventory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'attic.inventory'`

- [ ] **Step 3: Write the implementation**

Create `src/attic/inventory.py`:

```python
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
    with open(path, "a") as fh:
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
            data = json.loads(manifest.read_text())
            archived_at = datetime.fromisoformat(data["archived_at"].replace("Z", "+00:00"))
        except (OSError, ValueError, KeyError):
            continue
        if archived_at < cutoff:
            shutil.rmtree(path)
            removed.append(path)
    return removed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_inventory.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/attic/inventory.py tests/test_inventory.py
git commit -m "feat: add inventory snapshots and retention pruning"
```

---

## Task 7: The tick orchestrator

**Files:**
- Create: `src/attic/cli.py`, `tests/test_tick.py`

**Interfaces:**
- Consumes: everything from Tasks 1-6
- Produces: `run_tick(home, client, now, dry_run: bool = False) -> TickResult`; `TickResult` frozen dataclass (`actions: list[Action]`, `archived: list[str]` (archive ids), `reaped: bool`, `reason: str`), plus `main(argv: list[str] | None = None) -> int`

**Ordering contract (do not reorder):** snapshot → prune → guards → `update_state` → `save_state` → `decide` → per-action archive → close.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tick.py`:

```python
import json
from datetime import datetime, timedelta, timezone

from attic.cli import run_tick
from attic.store import AtticHome, PaneState
from fakes import FakeHerdrClient
from test_policy import mkpane

NOW = datetime(2026, 8, 13, 15, 47, 0, tzinfo=timezone.utc)


def home_with_clock(tmp_path, panes, hours_idle=10):
    home = AtticHome(tmp_path)
    home.ensure()
    home.save_state({
        p.terminal_id: PaneState(
            (NOW - timedelta(hours=hours_idle)).isoformat().replace("+00:00", "Z"),
            p.revision)
        for p in panes
    })
    return home


def test_tick_archives_then_closes_in_that_order(tmp_path):
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    client = FakeHerdrClient(panes=[pane], labels={"w1": "wh dev"})
    result = run_tick(home, client, NOW)
    assert result.reaped is True
    assert client.closed == ["w4:p2"]
    archive_dir = next(home.archive_dir.glob("2026*"))
    assert (archive_dir / "manifest.json").exists()
    assert json.loads(home.index_path.read_text().strip())["id"] == archive_dir.name


def test_tick_always_writes_inventory(tmp_path):
    pane = mkpane("w4:p2", status="working")
    home = AtticHome(tmp_path)
    home.ensure()
    run_tick(home, FakeHerdrClient(panes=[pane]), NOW)
    assert (home.inventory_dir / "2026-08-13.jsonl").exists()


def test_pause_file_blocks_reaping_but_not_inventory(tmp_path):
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    home.pause_path.touch()
    client = FakeHerdrClient(panes=[pane])
    result = run_tick(home, client, NOW)
    assert result.reaped is False
    assert result.reason == "paused"
    assert client.closed == []
    assert (home.inventory_dir / "2026-08-13.jsonl").exists()


def test_protocol_mismatch_blocks_reaping(tmp_path):
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    client = FakeHerdrClient(panes=[pane], protocol=20)
    result = run_tick(home, client, NOW)
    assert result.reaped is False
    assert "protocol" in result.reason
    assert client.closed == []


def test_dry_run_produces_verdicts_but_closes_nothing(tmp_path):
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    client = FakeHerdrClient(panes=[pane])
    result = run_tick(home, client, NOW, dry_run=True)
    assert client.closed == []
    assert list(home.archive_dir.glob("2026*")) == []
    assert len(result.actions) == 1


def test_failed_read_leaves_pane_alive_and_unindexed(tmp_path):
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    client = FakeHerdrClient(panes=[pane])
    client.fail_read.add("w4:p2")
    run_tick(home, client, NOW)
    assert client.closed == []
    assert not home.index_path.exists()


def test_close_failure_keeps_archive_and_marks_it(tmp_path):
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    client = FakeHerdrClient(panes=[pane])
    client.fail_close.add("w4:p2")
    run_tick(home, client, NOW)
    entry = json.loads(home.index_path.read_text().strip())
    assert entry["close_failed"] is True
    assert next(home.archive_dir.glob("2026*")).exists()


def test_herdr_unavailable_is_survivable(tmp_path):
    class Dead(FakeHerdrClient):
        def pane_list(self):
            from attic.herdr import HerdrError
            raise HerdrError("socket gone")

    home = AtticHome(tmp_path)
    home.ensure()
    result = run_tick(home, Dead(), NOW)
    assert result.reaped is False
    assert "herdr" in result.reason.lower()


def test_state_is_persisted_across_ticks(tmp_path):
    pane = mkpane("w4:p2")
    home = AtticHome(tmp_path)
    home.ensure()
    run_tick(home, FakeHerdrClient(panes=[pane]), NOW)
    assert home.load_state()[pane.terminal_id].first_idle_at == "2026-08-13T15:47:00Z"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tick.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'attic.cli'`

- [ ] **Step 3: Write the tick implementation**

Create `src/attic/cli.py` (the remaining subcommands land in Tasks 8-9):

```python
"""Command-line entry point and the tick orchestrator."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .archive import Archiver
from .herdr import HerdrClient, HerdrError
from .inventory import append_inventory, prune_archives, prune_inventory
from .policy import Action, Archive, Skip, decide, update_state
from .store import AtticHome

log = logging.getLogger("attic")


@dataclass(frozen=True)
class TickResult:
    actions: list[Action] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    reaped: bool = False
    reason: str = ""


def _setup_logging(home: AtticHome) -> None:
    home.ensure()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(home.log_path), logging.StreamHandler(sys.stderr)],
    )


def run_tick(home: AtticHome, client, now: datetime, dry_run: bool = False) -> TickResult:
    """Snapshot always; reap only when every guard passes. Never raises."""
    home.ensure()
    config = home.load_config()

    try:
        panes = client.pane_list()
        labels = client.workspace_labels()
    except HerdrError as exc:
        log.error("herdr unavailable, skipping tick: %s", exc)
        return TickResult(reason=f"herdr unavailable: {exc}")

    append_inventory(home, panes, labels, now)
    for path in prune_inventory(home, now, config.inventory_retention_days):
        log.info("pruned inventory %s", path.name)
    for path in prune_archives(home, now, config.archive_retention_days):
        log.info("pruned archive %s", path.name)

    state = update_state(panes, home.load_state(), now)
    home.save_state(state)

    if home.is_paused():
        log.info("PAUSE present, reaping disabled")
        return TickResult(reason="paused")

    try:
        protocol = client.protocol()
    except HerdrError as exc:
        return TickResult(reason=f"herdr protocol unreadable: {exc}")
    if protocol != config.herdr_protocol:
        log.warning(
            "herdr protocol %s != pinned %s, reaping disabled", protocol, config.herdr_protocol
        )
        return TickResult(reason=f"protocol mismatch: {protocol} != {config.herdr_protocol}")

    actions = decide(panes, state, now, config)
    if dry_run:
        return TickResult(actions=actions, reason="dry-run")

    archiver = Archiver(home, client)
    archived: list[str] = []
    for action in actions:
        if not isinstance(action, Archive):
            continue
        pane = action.pane
        label = labels.get(pane.workspace_id, pane.workspace_id)
        path = archiver.archive(action, label, now)
        if path is None:
            log.warning("archive failed for %s, leaving it alive", pane.pane_id)
            continue
        close_failed = False
        try:
            client.pane_close(pane.pane_id)
        except HerdrError as exc:
            close_failed = True
            log.error("archived %s but close failed: %s", pane.pane_id, exc)
        entry = {
            "id": path.name,
            "pane_id": pane.pane_id,
            "title": pane.title,
            "cwd": pane.cwd,
            "session_uuid": pane.session_uuid,
            "archived_at": now.isoformat().replace("+00:00", "Z"),
            "close_failed": close_failed,
        }
        archiver.append_index(entry)
        archived.append(path.name)
        log.info("archived %s as %s", pane.pane_id, path.name)

    return TickResult(actions=actions, archived=archived, reaped=True, reason="ok")


def _print_verdicts(result: TickResult) -> None:
    for action in result.actions:
        if isinstance(action, Archive):
            print(f"ARCHIVE  {action.pane.pane_id:<8} {action.pane.title}")
        else:
            print(f"skip     {action.pane.pane_id:<8} {action.pane.title}  ({action.reason})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="attic")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tick", help="snapshot inventory and reap idle agent panes")
    reap = sub.add_parser("reap", help="reap now")
    reap.add_argument("--dry-run", action="store_true", help="print verdicts, change nothing")

    args = parser.parse_args(argv)
    home = AtticHome.default()
    _setup_logging(home)
    now = datetime.now(timezone.utc)

    try:
        if args.command == "tick":
            result = run_tick(home, HerdrClient(), now)
            print(f"archived {len(result.archived)} pane(s); {result.reason}")
        elif args.command == "reap":
            result = run_tick(home, HerdrClient(), now, dry_run=args.dry_run)
            _print_verdicts(result)
    except Exception:                       # never crash the LaunchAgent loop
        log.exception("unhandled error in %s", args.command)
    return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tick.py -v`
Expected: 9 passed

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -v`
Expected: all tests pass (58 total across Tasks 1-7)

- [ ] **Step 6: Commit**

```bash
git add src/attic/cli.py tests/test_tick.py
git commit -m "feat: add tick orchestrator with pause, protocol guard, and dry-run"
```

---

## Task 8: `list` and `show`

**Files:**
- Modify: `src/attic/cli.py`
- Create: `src/attic/catalog.py`, `tests/test_catalog.py`

**Interfaces:**
- Consumes: `AtticHome` (Task 2)
- Produces: `load_manifests(home) -> list[dict]` (newest first); `resolve_id(home, prefix: str) -> dict` (raises `LookupError` on miss or ambiguity); `format_list(manifests) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_catalog.py`:

```python
import json

import pytest

from attic.catalog import format_list, load_manifests, resolve_id
from attic.store import AtticHome


def make_archive(home, archive_id, title, archived_at):
    path = home.archive_dir / archive_id
    path.mkdir(parents=True)
    (path / "manifest.json").write_text(json.dumps({
        "id": archive_id, "title": title, "archived_at": archived_at,
        "cwd": "/tmp/repo", "session_uuid": "u-1", "workspace": "wh dev",
        "resume": "cd /tmp/repo && claude --resume u-1",
    }))
    (path / "scrollback.txt").write_text("some output\n")
    return path


def test_manifests_load_newest_first(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    make_archive(home, "20260101T000000Z-old", "Old", "2026-01-01T00:00:00Z")
    make_archive(home, "20260812T000000Z-new", "New", "2026-08-12T00:00:00Z")
    assert [m["title"] for m in load_manifests(home)] == ["New", "Old"]


def test_resolve_by_unique_prefix(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    make_archive(home, "20260812T000000Z-new", "New", "2026-08-12T00:00:00Z")
    assert resolve_id(home, "20260812")["title"] == "New"


def test_resolve_rejects_ambiguous_prefix(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    make_archive(home, "20260812T000000Z-a", "A", "2026-08-12T00:00:00Z")
    make_archive(home, "20260812T000000Z-b", "B", "2026-08-12T00:00:01Z")
    with pytest.raises(LookupError, match="ambiguous"):
        resolve_id(home, "20260812")


def test_resolve_rejects_unknown_id(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    with pytest.raises(LookupError, match="no archive"):
        resolve_id(home, "nope")


def test_format_list_includes_id_and_title(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    make_archive(home, "20260812T000000Z-new", "Debug the thing", "2026-08-12T00:00:00Z")
    out = format_list(load_manifests(home))
    assert "20260812T000000Z-new" in out
    assert "Debug the thing" in out


def test_format_list_handles_empty_attic():
    assert "no archives" in format_list([]).lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'attic.catalog'`

- [ ] **Step 3: Write the implementation**

Create `src/attic/catalog.py`:

```python
"""Reading the archive catalog for `list`, `show`, and `restore`."""

from __future__ import annotations

import json

from .store import AtticHome


def load_manifests(home: AtticHome) -> list[dict]:
    out: list[dict] = []
    if not home.archive_dir.exists():
        return out
    for path in home.archive_dir.iterdir():
        if not path.is_dir():
            continue
        try:
            data = json.loads((path / "manifest.json").read_text())
        except (OSError, ValueError):
            continue
        data.setdefault("id", path.name)
        out.append(data)
    return sorted(out, key=lambda m: m.get("archived_at", ""), reverse=True)


def resolve_id(home: AtticHome, prefix: str) -> dict:
    matches = [m for m in load_manifests(home) if m["id"].startswith(prefix)]
    if not matches:
        raise LookupError(f"no archive matching {prefix!r}")
    if len(matches) > 1:
        names = ", ".join(m["id"] for m in matches[:5])
        raise LookupError(f"ambiguous prefix {prefix!r} matches: {names}")
    return matches[0]


def format_list(manifests: list[dict]) -> str:
    if not manifests:
        return "no archives"
    lines = []
    for m in manifests:
        lines.append(
            f"{m['id']}  {m.get('workspace', ''):<10} {m.get('title', '')}"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Wire the subcommands**

In `src/attic/cli.py`, add the import and subparsers, then handle them in `main`:

```python
from .catalog import format_list, load_manifests, resolve_id
```

Add to the parser section in `main`, after the `reap` parser:

```python
    sub.add_parser("list", help="list archived sessions")
    show = sub.add_parser("show", help="print an archive's manifest and scrollback")
    show.add_argument("archive_id")
```

Add to the dispatch chain in `main`, after the `reap` branch:

```python
        elif args.command == "list":
            print(format_list(load_manifests(home)))
        elif args.command == "show":
            manifest = resolve_id(home, args.archive_id)
            print(json.dumps(manifest, indent=2))
            print("\n--- scrollback ---\n")
            print((home.archive_dir / manifest["id"] / "scrollback.txt").read_text())
```

Add `import json` to the top of `cli.py`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_catalog.py -v`
Expected: 6 passed

- [ ] **Step 6: Verify the CLI end to end**

Run: `uv run attic list`
Expected: `no archives` (nothing has been archived yet)

- [ ] **Step 7: Commit**

```bash
git add src/attic/catalog.py src/attic/cli.py tests/test_catalog.py
git commit -m "feat: add archive catalog with list and show commands"
```

---

## Task 9: `restore`

Per the spec, restore is **non-destructive and repeatable**: the archive survives, and
`index.jsonl` gains a `restored_at` entry rather than losing one.

**Files:**
- Modify: `src/attic/cli.py`
- Create: `src/attic/restore.py`, `tests/test_restore.py`

**Interfaces:**
- Consumes: `AtticHome` (Task 2), `HerdrClient` (Task 4), `Archiver.append_index` (Task 5), `resolve_id` (Task 8)
- Produces: `restore(home, client, manifest: dict, now: datetime) -> str` (returns the new `pane_id`; raises `FileNotFoundError` if `cwd` is gone)

- [ ] **Step 1: Write the failing test**

Create `tests/test_restore.py`:

```python
import json
from datetime import datetime, timezone

import pytest

from attic.restore import restore
from attic.store import AtticHome
from fakes import FakeHerdrClient

NOW = datetime(2026, 8, 13, 15, 47, 0, tzinfo=timezone.utc)


def manifest(cwd: str) -> dict:
    return {
        "id": "20260812T000000Z-debug", "title": "Debug the thing",
        "cwd": cwd, "session_uuid": "u-1", "workspace": "wh dev",
        "resume": "cd X && claude --resume u-1",
    }


def test_restore_creates_a_tab_and_runs_the_stored_resume(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    client = FakeHerdrClient()
    pane_id = restore(home, client, manifest(str(tmp_path)), NOW)
    assert pane_id == "w9:p9"
    assert client.created_tabs == [(str(tmp_path), "Debug the thing")]
    assert client.ran == [("w9:p9", ["claude", "--resume", "u-1"])]


def test_restore_aborts_when_cwd_is_gone(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    client = FakeHerdrClient()
    with pytest.raises(FileNotFoundError):
        restore(home, client, manifest(str(tmp_path / "vanished")), NOW)
    assert client.created_tabs == []


def test_restore_is_non_destructive_and_logs_restored_at(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    archive = home.archive_dir / "20260812T000000Z-debug"
    archive.mkdir(parents=True)
    restore(home, FakeHerdrClient(), manifest(str(tmp_path)), NOW)
    assert archive.exists()
    entry = json.loads(home.index_path.read_text().strip())
    assert entry["restored_at"] == "2026-08-13T15:47:00Z"
    assert entry["id"] == "20260812T000000Z-debug"


def test_restore_twice_yields_two_panes(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    client = FakeHerdrClient()
    restore(home, client, manifest(str(tmp_path)), NOW)
    restore(home, client, manifest(str(tmp_path)), NOW)
    assert len(client.created_tabs) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_restore.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'attic.restore'`

- [ ] **Step 3: Write the implementation**

Create `src/attic/restore.py`:

```python
"""Bring an archived session back as a live herdr pane."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .archive import Archiver
from .policy import iso
from .store import AtticHome


def restore(home: AtticHome, client, manifest: dict, now: datetime) -> str:
    """Open a new tab in the focused workspace running the archived session.

    Non-destructive: the archive is kept and the index gains a restored_at entry.
    """
    cwd = manifest["cwd"]
    if not Path(cwd).is_dir():
        raise FileNotFoundError(f"cwd no longer exists: {cwd}")

    pane_id = client.tab_create(cwd, manifest.get("title", manifest["id"]))
    client.pane_run(pane_id, ["claude", "--resume", manifest["session_uuid"]])

    Archiver(home, client).append_index({
        "id": manifest["id"],
        "restored_at": iso(now),
        "restored_into": pane_id,
    })
    return pane_id
```

- [ ] **Step 4: Wire the subcommand**

In `src/attic/cli.py` add the import:

```python
from .restore import restore as restore_archive
```

Add to the parser section:

```python
    restore_p = sub.add_parser("restore", help="reopen an archived session")
    restore_p.add_argument("archive_id")
```

Add to the dispatch chain:

```python
        elif args.command == "restore":
            manifest = resolve_id(home, args.archive_id)
            pane_id = restore_archive(home, HerdrClient(), manifest, now)
            print(f"restored {manifest['id']} into {pane_id}")
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: all pass (68 total)

- [ ] **Step 6: Commit**

```bash
git add src/attic/restore.py src/attic/cli.py tests/test_restore.py
git commit -m "feat: add non-destructive restore of archived sessions"
```

---

## Task 10: Integration test against live herdr

**Files:**
- Create: `tests/test_integration_herdr.py`

**Interfaces:**
- Consumes: `HerdrClient` (Task 4), `run_tick` (Task 7)
- Produces: nothing importable

This test is marked `integration` and excluded from the default run by the `addopts` set
in Task 1. It is the only test that touches the real herdr server.

- [ ] **Step 1: Write the integration test**

Create `tests/test_integration_herdr.py`:

```python
"""Runs against the live herdr server. Excluded by default; run with:
    uv run pytest -m integration -v
"""

import shutil
from datetime import datetime, timezone

import pytest

from attic.herdr import HerdrClient

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    if shutil.which("herdr") is None:
        pytest.skip("herdr not installed")
    return HerdrClient()


def test_protocol_matches_the_pinned_value(client):
    from attic.store import Config
    assert client.protocol() == Config().herdr_protocol, (
        "herdr protocol changed; review HerdrClient parsing, then bump "
        "Config.herdr_protocol only after re-verifying every command shape"
    )


def test_pane_list_shape_is_still_what_we_parse(client):
    panes = client.pane_list()
    assert panes, "expected at least one live pane"
    for p in panes:
        assert p.pane_id and p.terminal_id
        assert p.agent_status in {"idle", "working", "blocked", "unknown"}


def test_agent_panes_expose_session_uuids(client):
    agents = [p for p in client.pane_list() if p.is_agent]
    if not agents:
        pytest.skip("no agent panes running")
    assert all(p.session_uuid for p in agents)


def test_pane_read_returns_output_for_a_live_pane(client):
    pane = client.pane_list()[0]
    text = client.pane_read(pane.pane_id, min(pane.scroll_rows or 50, 200))
    assert isinstance(text, str)


def test_workspace_labels_resolve(client):
    labels = client.workspace_labels()
    assert labels and all(isinstance(v, str) for v in labels.values())
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest -m integration -v`
Expected: 5 passed (or skips if no agent panes are running)

If `test_protocol_matches_the_pinned_value` fails, that is the protocol guard doing its
job — do not bump `Config.herdr_protocol` without re-verifying every command shape in the
Task 4 table against `herdr --help`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration_herdr.py
git commit -m "test: add live-herdr integration checks behind an integration marker"
```

---

## Task 11: LaunchAgent, README, and the trust-building soak

Do not enable the timer and walk away. The dry-run soak is how you verify the policy
against your real workload before it has authority to close anything.

**Files:**
- Create: `launchd/com.you.attic.plist`, `README.md`, `install.sh`

**Interfaces:**
- Consumes: the `attic` entry point (Task 7)
- Produces: nothing importable

- [ ] **Step 1: Write the LaunchAgent**

Create `launchd/com.you.attic.plist`. Note `RunAtLoad` is false: the first run should
be a deliberate manual one.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.you.attic</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/you/.local/bin/uv</string>
        <string>run</string>
        <string>--project</string>
        <string>/Users/you/repos/attic</string>
        <string>attic</string>
        <string>tick</string>
    </array>
    <key>StartInterval</key>
    <integer>300</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/Users/you/.attic/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/you/.attic/logs/launchd.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/bin:/bin:/Users/you/.local/bin</string>
    </dict>
</dict>
</plist>
```

`PATH` must include `/opt/homebrew/bin` — launchd jobs do not inherit your shell
environment, and `HerdrClient` shells out to `herdr` by bare name.

- [ ] **Step 2: Write the install script**

Create `install.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/com.you.attic.plist"
mkdir -p "$HOME/.attic/logs" "$HOME/Library/LaunchAgents"
cp launchd/com.you.attic.plist "$PLIST"

# Start paused. Reaping is enabled only after the soak in the README.
touch "$HOME/.attic/PAUSE"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "attic installed and PAUSED. Inventory is running; reaping is disabled."
echo "Remove $HOME/.attic/PAUSE to enable reaping."
```

Then: `chmod +x install.sh`

Installing in the paused state is deliberate: inventory (the feature with no downside)
starts immediately, while reaping waits for you to grant it authority.

- [ ] **Step 3: Write the README**

Create `README.md`:

````markdown
# attic

Archives idle herdr agent panes before reclaiming them, and keeps a running inventory of
what was open.

See the design spec: `docs/superpowers/specs/2026-08-13-attic-herdr-agent-archiver-design.md`

## Install

```bash
./install.sh          # installs the LaunchAgent, starts PAUSED
```

## The soak — do this before enabling reaping

`attic` installs paused. Inventory runs from minute one; reaping is off.

1. Let inventory run for a few days: `attic list` stays empty, but
   `~/.attic/inventory/` fills up. Confirm it is capturing what you expect.
2. Run `attic reap --dry-run` daily. Read every verdict. Confirm that nothing you
   care about is ever marked `ARCHIVE`, especially anything `blocked`.
3. When the verdicts look right, `rm ~/.attic/PAUSE`.
4. After the first real archive, run `attic restore <id>` immediately and confirm the
   session resumes with its history intact.

## Commands

| Command | Effect |
|---|---|
| `attic tick` | Snapshot inventory, then reap if all guards pass (what launchd runs) |
| `attic reap --dry-run` | Print a verdict and reason for every pane; change nothing |
| `attic list` | List archived sessions, newest first |
| `attic show <id>` | Print an archive's manifest and scrollback (unique prefix works) |
| `attic restore <id>` | Reopen the session in a new tab; archive is kept |

## Pausing

```bash
touch ~/.attic/PAUSE     # inventory continues, reaping stops
rm ~/.attic/PAUSE        # reaping resumes
```

## What gets archived

Only panes that are **all** of: an agent pane, `agent_status == idle`, holding a session
UUID, unfocused, with an unchanged revision counter, idle for 4+ hours. At most 3 per tick.

`blocked` panes are never archived at any age — they are waiting on you.

## What scrollback actually contains

`herdr pane read` returns the pane's **rendered terminal frames**, including TUI chrome
(status line, box borders, spinners). It is a faithful record of what was on screen, not
a clean transcript. The conversation itself is recovered by `claude --resume`, which is
what the manifest's `resume` command does.

## Configuration

`~/.attic/config.json`, all keys optional:

```json
{
  "idle_threshold_hours": 4.0,
  "per_tick_cap": 3,
  "archive_retention_days": 30,
  "inventory_retention_days": 90,
  "herdr_protocol": 19
}
```

## Development

```bash
uv run pytest                  # unit tests
uv run pytest -m integration   # against the live herdr server
```
````

- [ ] **Step 4: Install and verify the first tick manually**

```bash
./install.sh
uv run attic tick
uv run attic reap --dry-run
```

Expected: `tick` prints `archived 0 pane(s); paused`, and `reap --dry-run` prints one line
per live pane with a reason. Confirm your `blocked` and `working` panes are skipped for
the right reasons.

- [ ] **Step 5: Confirm launchd is scheduled**

Run: `launchctl list | grep attic`
Expected: a line containing `com.you.attic`

Then, after five minutes: `ls ~/.attic/inventory/` should contain today's `.jsonl`.

- [ ] **Step 6: Commit**

```bash
git add launchd/ install.sh README.md
git commit -m "feat: add LaunchAgent, installer, and soak instructions"
```

---

## Task 12: One-time cleanup of the legacy graveyard

Separate from the system by design: these chores will not recur once agents run only
under herdr.

**Files:**
- Create: `docs/cleanup-2026-08-13.md`

- [ ] **Step 1: Locate zellij resurrection data before deleting anything**

```bash
zellij list-sessions
ls -la ~/.cache/zellij ~/Library/Caches/org.Zellij-Contributors.zellij 2>/dev/null
find ~/Library -maxdepth 4 -iname '*zellij*' 2>/dev/null
```

Record what you find in `docs/cleanup-2026-08-13.md`. Do not delete yet.

- [ ] **Step 2: Preserve anything real**

```bash
mkdir -p ~/.attic/legacy/zellij
# Copy whatever the previous step located, e.g.:
# cp -R ~/Library/Caches/org.Zellij-Contributors.zellij ~/.attic/legacy/zellij/
ls -R ~/.attic/legacy/zellij
```

If the previous step found nothing, note that explicitly in the doc and continue —
the sessions were already `EXITED` with no recoverable state.

- [ ] **Step 3: Delete the six dead sessions**

```bash
for s in dev sidemoney adventurous-xylophone hopeful-iguanadon jumping-apricot undulating-horse; do
  zellij delete-session "$s" || echo "already gone: $s"
done
zellij list-sessions
```

Expected: no sessions remain.

- [ ] **Step 4: Record the Daytona follow-up (do not act)**

Append to `docs/cleanup-2026-08-13.md`:

```markdown
## Daytona — deferred, needs a decision

Three ARCHIVED sandboxes (79 / 133 / 228 days old):
- 66666666-6666-4666-8666-666666666666
- 44444444-4444-4444-8444-444444444444
- 88888888-8888-4888-8888-888888888888

ARCHIVED means no compute, but storage may still bill. The local CLI is v0.154 against a
v0.204 API, so `brew upgrade daytonaio/cli/daytona` comes first. Out of scope for attic:
the user confirmed during design that remote sandboxes are not part of the leak that hurts.
Deleting these is destructive and needs explicit confirmation.
```

- [ ] **Step 5: Commit**

```bash
git add docs/cleanup-2026-08-13.md
git commit -m "docs: record legacy zellij cleanup and deferred Daytona decision"
```

---

## Self-Review Notes

**Spec coverage:** every spec section maps to a task — architecture (1-7), data model
(2, 5), archive identity (5), restore behavior (9), reap policy (3), safety guards (3, 7),
error handling (4, 5, 7), retention (6), testing (all tasks plus 10), one-time cleanup (12).

**Deliberate deviation:** package layout instead of a PEP 723 single file, flagged at the
top of this plan with reasoning. The underlying constraint (uv, no system pip) is preserved.

**Type consistency:** `Pane`, `PaneState`, `Config`, `Archive`, `Skip`, `AtticHome`,
`Archiver`, `TickResult`, and `HerdrClient`'s method set are defined once and referenced
identically throughout. `FakeHerdrClient` mirrors `HerdrClient`'s full surface, and Task 10
verifies the real client's assumptions against a live server.

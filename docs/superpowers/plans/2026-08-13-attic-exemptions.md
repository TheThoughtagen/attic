# attic exemptions and verdict recording: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a pane be protected from reaping — indefinitely (`pin`) or until a deadline (`snooze`) — and record every tick's verdict so "why wasn't that reaped?" becomes answerable.

**Architecture:** Both exemptions live in `PaneState` inside the existing `state.json`, so they are already in the dict `decide()` receives and become two pure skip predicates — no new I/O in the policy. Four CLI verbs resolve pane IDs to terminal IDs before storing, because pane IDs are positional and get recycled. The inventory gains `verdict` and `reason` per pane so the record exists at all.

**Tech Stack:** Python 3.11+, stdlib only, `uv`, pytest.

**Spec:** `docs/superpowers/specs/2026-08-13-attic-ui-design.md` (sections "Snooze and pin", "CLI verbs", "Verdicts recorded in the inventory")

**This is plan 1 of 2.** Plan 2 builds the Textual TUI on top. Nothing here depends on it.

## Global Constraints

- **Never `pip install` into system/global Python.** All Python runs through `uv` (`uv run pytest`).
- **Runtime dependencies: stdlib only.** `pytest` is never imported under `src/attic/`.
- **Python 3.11+.** All file I/O passes `encoding="utf-8"` explicitly — launchd runs with no `LANG`.
- **All timestamps are timezone-aware UTC** via `iso()` from `policy.py`, which raises on naive input.
- **`tick` always exits 0.** Interactive commands return 1 on error; `tick`/`reap` return 0 on every path.
- **Corrupt or unparseable state degrades gracefully, never raises.** An exception on the tick path silently kills the LaunchAgent job.
- **Protection can only reduce what gets closed.** No change here may cause a pane to be archived that would previously have been spared.
- **Existing suite must stay green:** 120 unit tests, plus 5 live integration tests behind `-m integration`.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `src/attic/store.py` | modify | `PaneState` gains `snooze_until` / `pinned`; serialize, validate, round-trip |
| `src/attic/policy.py` | modify | two skip predicates in `_verdict`; `update_state` preserves and expires |
| `src/attic/duration.py` | create | parse `30m` / `4h` / `2d` — one job, no dependencies |
| `src/attic/exempt.py` | create | resolve an identifier to a terminal ID; apply pin/snooze mutations |
| `src/attic/cli.py` | modify | four subcommands wired to `exempt.py` |
| `src/attic/inventory.py` | modify | record `verdict` and `reason` per pane |
| `tests/test_store.py` | modify | round-trip and validation of the new fields |
| `tests/test_policy.py` | modify | the predicates, ordering, and expiry |
| `tests/test_duration.py` | create | parsing and rejection |
| `tests/test_exempt.py` | create | terminal-ID resolution, including the recycled-pane-ID hazard |
| `tests/test_inventory.py` | modify | verdict/reason recorded, old lines tolerated |

---

## Task 1: `PaneState` carries the exemptions

**Files:**
- Modify: `src/attic/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: existing `PaneState(first_idle_at: str | None, last_revision: int)`
- Produces: `PaneState(first_idle_at, last_revision, snooze_until: str | None = None, pinned: bool = False)` — round-tripped through `save_state`/`load_state`, with the same drop-the-bad-entry validation the existing fields use.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_store.py`:

```python
def test_exemptions_round_trip(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    home.save_state({"term_a": PaneState("2026-08-13T00:00:00Z", 3,
                                         snooze_until="2026-08-14T00:00:00Z", pinned=True)})
    loaded = home.load_state()["term_a"]
    assert loaded.snooze_until == "2026-08-14T00:00:00Z"
    assert loaded.pinned is True


def test_exemptions_default_to_absent(tmp_path):
    """Entries written before this feature existed must still load."""
    home = AtticHome(tmp_path)
    home.ensure()
    home.state_path.write_text(
        json.dumps({"term_a": {"first_idle_at": None, "last_revision": 1}}), encoding="utf-8"
    )
    loaded = home.load_state()["term_a"]
    assert loaded.snooze_until is None
    assert loaded.pinned is False


def test_malformed_exemption_fields_drop_the_entry(tmp_path):
    """Same rule as first_idle_at: an entry we cannot trust is dropped, never
    fatal. A raise here kills every future tick with no visible symptom."""
    home = AtticHome(tmp_path)
    home.ensure()
    home.state_path.write_text(json.dumps({
        "term_good": {"first_idle_at": None, "last_revision": 1,
                      "snooze_until": "2026-08-14T00:00:00Z", "pinned": False},
        "term_bad_stamp": {"first_idle_at": None, "last_revision": 1,
                           "snooze_until": "not a date"},
        "term_naive_stamp": {"first_idle_at": None, "last_revision": 1,
                             "snooze_until": "2026-08-14T00:00:00"},
        "term_bad_pin": {"first_idle_at": None, "last_revision": 1, "pinned": "yes"},
    }), encoding="utf-8")
    assert set(home.load_state()) == {"term_good"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store.py -k exemption -v`
Expected: FAIL — `PaneState.__init__() got an unexpected keyword argument 'snooze_until'`

- [ ] **Step 3: Extend the dataclass**

In `src/attic/store.py`, replace the `PaneState` definition:

```python
@dataclass
class PaneState:
    first_idle_at: str | None
    last_revision: int
    # Exemptions. `snooze_until` expires on its own so a forgotten snooze cannot
    # become a permanent leak; `pinned` requires an explicit unpin.
    snooze_until: str | None = None
    pinned: bool = False
```

- [ ] **Step 4: Validate on load**

In `load_state`, after the existing `first_idle_at` / `last_revision` handling and before constructing the entry, add:

```python
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
```

then pass both into the constructor:

```python
            out[key] = PaneState(
                first_idle_at=first_idle_at,
                last_revision=last_revision,
                snooze_until=snooze_until,
                pinned=pinned,
            )
```

- [ ] **Step 5: Persist on save**

In `save_state`, replace the payload comprehension:

```python
        payload = {
            k: {
                "first_idle_at": v.first_idle_at,
                "last_revision": v.last_revision,
                "snooze_until": v.snooze_until,
                "pinned": v.pinned,
            }
            for k, v in state.items()
        }
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_store.py -v`
Expected: all pass, including the pre-existing state tests.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest`
Expected: all previously passing tests still pass.

- [ ] **Step 8: Commit**

```bash
git add src/attic/store.py tests/test_store.py
git commit -m "feat: PaneState carries pin and snooze exemptions"
```

---

## Task 2: `decide()` honours the exemptions

**Files:**
- Modify: `src/attic/policy.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: `PaneState.snooze_until`, `PaneState.pinned` (Task 1)
- Produces: two new skip reasons — `"pinned"` and `"snoozed until <ISO8601>"` — emitted by `_verdict`; `update_state` preserves both fields and clears an expired `snooze_until`.

**Ordering contract:** the exemption checks go immediately after the `is_agent` check and **before** the status check. A pinned `working` pane reports `pinned` rather than `status is working`, because the operator's explicit intent is the stronger reason to show them.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_policy.py`:

```python
def pinned_state(pinned=True, **kw):
    return PaneState(first_idle_at=(NOW - timedelta(hours=10)).isoformat().replace("+00:00", "Z"),
                     last_revision=1, pinned=pinned, **kw)


def test_pinned_pane_is_never_archived():
    st = {"term_w1:p1": pinned_state()}
    actions = decide([mkpane()], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "pinned"


def test_snoozed_pane_is_not_archived_before_the_deadline():
    st = {"term_w1:p1": pinned_state(pinned=False,
                                     snooze_until="2026-08-13T18:00:00Z")}   # NOW is 12:00
    actions = decide([mkpane()], st, NOW, CFG)
    assert archived(actions) == []
    assert skip_reason(actions, "w1:p1") == "snoozed until 2026-08-13T18:00:00Z"


def test_an_expired_snooze_stops_protecting():
    st = {"term_w1:p1": pinned_state(pinned=False,
                                     snooze_until="2026-08-13T06:00:00Z")}   # in the past
    assert archived(decide([mkpane()], st, NOW, CFG)) == ["w1:p1"]


def test_pin_outranks_status_in_the_reported_reason():
    """The operator's explicit intent is the more useful thing to show."""
    st = {"term_w1:p1": pinned_state()}
    actions = decide([mkpane(status="working")], st, NOW, CFG)
    assert skip_reason(actions, "w1:p1") == "pinned"


def test_exemptions_survive_update_state():
    prior = {"term_w1:p1": PaneState("2026-08-13T02:00:00Z", 5,
                                     snooze_until="2026-08-14T00:00:00Z", pinned=True)}
    st = update_state([mkpane(revision=5)], prior, NOW)
    assert st["term_w1:p1"].pinned is True
    assert st["term_w1:p1"].snooze_until == "2026-08-14T00:00:00Z"


def test_update_state_clears_an_expired_snooze():
    """So state.json does not accumulate stale deadlines forever."""
    prior = {"term_w1:p1": PaneState("2026-08-13T02:00:00Z", 5,
                                     snooze_until="2026-08-13T06:00:00Z")}
    st = update_state([mkpane(revision=5)], prior, NOW)
    assert st["term_w1:p1"].snooze_until is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_policy.py -k "pinned or snooze or exemption" -v`
Expected: FAIL — `AssertionError: assert 'not idle long enough' == 'pinned'` and similar.

- [ ] **Step 3: Add the predicates to `_verdict`**

In `src/attic/policy.py`, inside `_verdict`, immediately after the `is_agent` check:

```python
    entry = state.get(pane.terminal_id)
    if entry is not None:
        # Operator intent outranks every automatic reason, so it is reported first.
        if entry.pinned:
            return Skip(pane, "pinned")
        if entry.snooze_until:
            until = _parse(entry.snooze_until)
            if now < until:
                return Skip(pane, f"snoozed until {entry.snooze_until}")
```

Delete the later `entry = state.get(pane.terminal_id)` line, since `entry` is now bound above; keep the `if entry is None or entry.first_idle_at is None` check that follows.

- [ ] **Step 4: Preserve and expire in `update_state`**

In `update_state`, every branch currently constructs `PaneState(...)` with two arguments. Each must carry the exemptions forward. Replace the body of the loop with:

```python
    for pane in panes:
        prior = state.get(pane.terminal_id)
        pinned = prior.pinned if prior else False
        snooze_until = prior.snooze_until if prior else None
        # Drop a deadline that has passed so state.json does not accrue stale ones.
        if snooze_until and _parse(snooze_until) <= now:
            snooze_until = None

        if pane.agent_status not in REAPABLE_STATUSES:
            updated[pane.terminal_id] = PaneState(None, pane.revision, snooze_until, pinned)
            continue
        if prior is None or prior.last_revision != pane.revision or prior.first_idle_at is None:
            updated[pane.terminal_id] = PaneState(iso(now), pane.revision, snooze_until, pinned)
        else:
            updated[pane.terminal_id] = PaneState(
                prior.first_idle_at, pane.revision, snooze_until, pinned
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_policy.py -v`
Expected: all pass, including the pre-existing policy tests.

- [ ] **Step 6: Prove the guard has teeth**

Temporarily delete the `if entry.pinned:` branch and run
`uv run pytest tests/test_policy.py::test_pinned_pane_is_never_archived -v`.
Expected: FAIL. Restore the branch. This codebase has produced five tests that
passed against deliberately broken code; confirm this one is not the sixth.

- [ ] **Step 7: Run the full suite and commit**

```bash
uv run pytest
git add src/attic/policy.py tests/test_policy.py
git commit -m "feat: decide() honours pin and snooze exemptions"
```

---

## Task 3: Duration parsing

**Files:**
- Create: `src/attic/duration.py`, `tests/test_duration.py`

**Interfaces:**
- Consumes: nothing
- Produces: `parse_duration(text: str) -> timedelta`, raising `ValueError` with a usable message on anything it does not understand.

- [ ] **Step 1: Write the failing test**

Create `tests/test_duration.py`:

```python
from datetime import timedelta

import pytest

from attic.duration import parse_duration


def test_supported_units():
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("4h") == timedelta(hours=4)
    assert parse_duration("2d") == timedelta(days=2)


def test_leading_and_trailing_space_is_tolerated():
    assert parse_duration("  4h ") == timedelta(hours=4)


@pytest.mark.parametrize("bad", ["", "4", "h", "4w", "-4h", "0h", "four hours", "4.5h", "4h30m"])
def test_rejects_rather_than_guesses(bad):
    """A misread duration silently changes how long a session is protected, so
    anything ambiguous is refused loudly instead of interpreted."""
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_the_error_names_what_is_accepted():
    with pytest.raises(ValueError, match="30m"):
        parse_duration("4w")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_duration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'attic.duration'`

- [ ] **Step 3: Write the implementation**

Create `src/attic/duration.py`:

```python
"""Parsing snooze durations.

Deliberately narrow: a misread duration silently changes how long a session is
protected from reaping, so anything ambiguous is refused rather than
interpreted. No compound forms, no fractions, no zero, no negatives.
"""

from __future__ import annotations

import re
from datetime import timedelta

_PATTERN = re.compile(r"^(\d+)([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(text: str) -> timedelta:
    match = _PATTERN.match(text.strip())
    if not match:
        raise ValueError(f"cannot read duration {text!r}; expected forms like 30m, 4h, 2d")
    amount = int(match.group(1))
    if amount == 0:
        raise ValueError("duration must be greater than zero; use unsnooze to clear one")
    return timedelta(**{_UNITS[match.group(2)]: amount})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_duration.py -v`
Expected: 5 passed (the parametrized case counts as several).

- [ ] **Step 5: Commit**

```bash
git add src/attic/duration.py tests/test_duration.py
git commit -m "feat: add snooze duration parsing"
```

---

## Task 4: Applying exemptions, keyed by terminal ID

**Files:**
- Create: `src/attic/exempt.py`, `tests/test_exempt.py`

**Interfaces:**
- Consumes: `AtticHome` (store), `PaneState` (Task 1), `parse_duration` (Task 3), `HerdrClient.pane_list()` (existing)
- Produces:
  - `resolve_terminal_id(panes: list[Pane], identifier: str) -> str` — accepts a pane ID or a terminal ID, raises `LookupError` if neither matches
  - `set_pinned(home: AtticHome, terminal_id: str, pinned: bool) -> None`
  - `set_snooze(home: AtticHome, terminal_id: str, until: datetime | None) -> str | None` — returns the *previous* deadline so the caller can report the change

**The hazard this task exists to avoid:** pane IDs are positional and recycled. Storing a pin under `w4:p2` would protect the **slot**, so a brand-new session opening into that pane inherits the pin silently. Everything is keyed by `terminal_id`, resolved at command time.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_exempt.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from attic.exempt import resolve_terminal_id, set_pinned, set_snooze
from attic.store import AtticHome, PaneState
from test_policy import mkpane

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def test_resolves_a_pane_id_to_its_terminal_id():
    panes = [mkpane("w4:p2", terminal_id="term_abc")]
    assert resolve_terminal_id(panes, "w4:p2") == "term_abc"


def test_accepts_a_terminal_id_directly():
    panes = [mkpane("w4:p2", terminal_id="term_abc")]
    assert resolve_terminal_id(panes, "term_abc") == "term_abc"


def test_unknown_identifier_raises_lookup_error():
    with pytest.raises(LookupError, match="w9:p9"):
        resolve_terminal_id([mkpane("w4:p2")], "w9:p9")


def test_pinning_is_stored_under_the_terminal_id_not_the_pane_id(tmp_path):
    """Pane IDs are positional and recycled. A pin stored under 'w4:p2' would
    protect the slot, so a new session opening there inherits it silently."""
    home = AtticHome(tmp_path)
    home.ensure()
    panes = [mkpane("w4:p2", terminal_id="term_original")]
    set_pinned(home, resolve_terminal_id(panes, "w4:p2"), True)
    assert home.load_state()["term_original"].pinned is True
    assert "w4:p2" not in home.load_state()


def test_a_new_terminal_in_the_same_pane_slot_is_not_pinned(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    set_pinned(home, resolve_terminal_id([mkpane("w4:p2", terminal_id="term_old")], "w4:p2"), True)
    recycled = [mkpane("w4:p2", terminal_id="term_new")]
    state = home.load_state()
    assert state.get(resolve_terminal_id(recycled, "w4:p2")) is None


def test_pinning_preserves_an_existing_idle_clock(tmp_path):
    """Protection must not reset the clock — guards gate execution, not observation."""
    home = AtticHome(tmp_path)
    home.ensure()
    home.save_state({"term_abc": PaneState("2026-08-13T02:00:00Z", 7)})
    set_pinned(home, "term_abc", True)
    entry = home.load_state()["term_abc"]
    assert entry.first_idle_at == "2026-08-13T02:00:00Z"
    assert entry.last_revision == 7
    assert entry.pinned is True


def test_snooze_returns_the_previous_deadline_so_it_can_be_reported(tmp_path):
    """Re-snoozing replaces rather than stacks, which can shorten protection —
    so the caller must be able to say what it changed."""
    home = AtticHome(tmp_path)
    home.ensure()
    set_snooze(home, "term_abc", NOW + timedelta(hours=8))
    previous = set_snooze(home, "term_abc", NOW + timedelta(hours=1))
    assert previous == "2026-08-13T20:00:00Z"
    assert home.load_state()["term_abc"].snooze_until == "2026-08-13T13:00:00Z"


def test_unsnooze_clears_the_deadline(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    set_snooze(home, "term_abc", NOW + timedelta(hours=8))
    set_snooze(home, "term_abc", None)
    assert home.load_state()["term_abc"].snooze_until is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_exempt.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'attic.exempt'`

- [ ] **Step 3: Write the implementation**

Create `src/attic/exempt.py`:

```python
"""Applying pin and snooze exemptions.

Everything is keyed by `terminal_id`, never `pane_id`. Pane IDs are positional
and get recycled when panes close and reopen, so a pin stored under "w4:p2"
would protect the *slot* — and a brand-new session opening into that pane would
silently inherit the protection. The identifier is resolved at command time.
"""

from __future__ import annotations

from datetime import datetime

from .models import Pane
from .policy import iso
from .store import AtticHome, PaneState


def resolve_terminal_id(panes: list[Pane], identifier: str) -> str:
    """Accept either a pane id (w4:p2) or a terminal id (term_abc)."""
    for pane in panes:
        if pane.terminal_id == identifier:
            return identifier
    for pane in panes:
        if pane.pane_id == identifier:
            return pane.terminal_id
    raise LookupError(f"no live pane matching {identifier!r}")


def _mutate(home: AtticHome, terminal_id: str, **changes) -> PaneState:
    """Read-modify-write a single entry, leaving the idle clock untouched."""
    state = home.load_state()
    entry = state.get(terminal_id) or PaneState(None, 0)
    for field, value in changes.items():
        setattr(entry, field, value)
    state[terminal_id] = entry
    home.save_state(state)
    return entry


def set_pinned(home: AtticHome, terminal_id: str, pinned: bool) -> None:
    _mutate(home, terminal_id, pinned=pinned)


def set_snooze(home: AtticHome, terminal_id: str, until: datetime | None) -> str | None:
    """Set or clear the deadline. Returns the previous one, if any.

    Re-snoozing replaces rather than stacks — stacking would let repeated
    snoozes compound invisibly into days of protection, which is the silent
    accumulation this tool exists to prevent. Because replacing can therefore
    shorten a snooze, the previous value is returned so the caller can report it.
    """
    previous = home.load_state().get(terminal_id)
    previous_until = previous.snooze_until if previous else None
    _mutate(home, terminal_id, snooze_until=iso(until) if until else None)
    return previous_until
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_exempt.py -v`
Expected: 8 passed.

- [ ] **Step 5: Run the full suite and commit**

```bash
uv run pytest
git add src/attic/exempt.py tests/test_exempt.py
git commit -m "feat: apply pin and snooze exemptions keyed by terminal id"
```

---

## Task 5: The four CLI verbs

**Files:**
- Modify: `src/attic/cli.py`
- Test: `tests/test_exempt_cli.py` (create)

**Interfaces:**
- Consumes: `resolve_terminal_id`, `set_pinned`, `set_snooze` (Task 4), `parse_duration` (Task 3), `HerdrClient` (existing)
- Produces: subcommands `pin`, `unpin`, `snooze`, `unsnooze`, each taking one identifier; `snooze` additionally takes a duration.

**CRITICAL — how to edit `src/attic/cli.py`:** it already has `tick`, `reap`, `list`, `show`, `restore`, plus `run_tick`, `_print_verdicts`, `_gate_on_resumability`, `_setup_logging`, and `main`. **APPEND** parsers after the existing `restore` parser and dispatch branches after the existing `restore` branch. Do not rewrite or reorder `main()`. The suite has ~35 tests covering the existing commands; if any break, something was clobbered.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_exempt_cli.py`:

```python
import json

from attic.cli import main
from attic.store import AtticHome
from fakes import FakeHerdrClient
from test_policy import mkpane


def stub_client(monkeypatch, panes):
    monkeypatch.setattr("attic.cli.HerdrClient", lambda: FakeHerdrClient(panes=panes))


def test_pin_stores_under_the_terminal_id(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    stub_client(monkeypatch, [mkpane("w4:p2", terminal_id="term_abc")])
    assert main(["pin", "w4:p2"]) == 0
    assert AtticHome(tmp_path).load_state()["term_abc"].pinned is True
    assert "pinned" in capsys.readouterr().out


def test_snooze_reports_both_deadlines_when_replacing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    stub_client(monkeypatch, [mkpane("w4:p2", terminal_id="term_abc")])
    main(["snooze", "w4:p2", "8h"])
    capsys.readouterr()
    assert main(["snooze", "w4:p2", "1h"]) == 0
    out = capsys.readouterr().out
    assert "was" in out          # the shortening is reported, not silent


def test_snooze_on_a_pinned_pane_says_it_has_no_effect(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    stub_client(monkeypatch, [mkpane("w4:p2", terminal_id="term_abc")])
    main(["pin", "w4:p2"])
    capsys.readouterr()
    main(["snooze", "w4:p2", "4h"])
    assert "pinned" in capsys.readouterr().out


def test_bad_duration_exits_nonzero_without_changing_state(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    stub_client(monkeypatch, [mkpane("w4:p2", terminal_id="term_abc")])
    assert main(["snooze", "w4:p2", "4w"]) == 1
    assert AtticHome(tmp_path).load_state() == {}
    assert "30m" in capsys.readouterr().err


def test_unknown_pane_exits_nonzero(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    stub_client(monkeypatch, [mkpane("w4:p2", terminal_id="term_abc")])
    assert main(["pin", "w9:p9"]) == 1
    assert "w9:p9" in capsys.readouterr().err


def test_existing_commands_still_work(monkeypatch, tmp_path, capsys):
    """Guard against clobbering main() while appending subcommands."""
    monkeypatch.setenv("ATTIC_HOME", str(tmp_path))
    assert main(["list"]) == 0
    assert "no archives" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_exempt_cli.py -v`
Expected: FAIL — `argument command: invalid choice: 'pin'`

- [ ] **Step 3: Add the parsers**

In `main()`, immediately after the existing `restore_p` parser:

```python
    for verb, helptext in (("pin", "never reap this pane"),
                           ("unpin", "allow this pane to be reaped again"),
                           ("unsnooze", "clear this pane's snooze")):
        parser_ = sub.add_parser(verb, help=helptext)
        parser_.add_argument("identifier", help="pane id (w4:p2) or terminal id")
    snooze_p = sub.add_parser("snooze", help="protect this pane until a deadline")
    snooze_p.add_argument("identifier", help="pane id (w4:p2) or terminal id")
    snooze_p.add_argument("duration", help="30m, 4h, 2d")
```

- [ ] **Step 4: Add the dispatch branch**

Append after the existing `restore` branch, inside the same `try`:

```python
        elif args.command in ("pin", "unpin", "snooze", "unsnooze"):
            try:
                panes = HerdrClient().pane_list()
                terminal_id = resolve_terminal_id(panes, args.identifier)
            except (HerdrError, LookupError) as exc:
                print(str(exc), file=sys.stderr)
                return 1

            if args.command in ("pin", "unpin"):
                set_pinned(home, terminal_id, args.command == "pin")
                print(f"{args.command}ned {args.identifier} ({terminal_id})")
                return 0

            if args.command == "unsnooze":
                set_snooze(home, terminal_id, None)
                print(f"snooze cleared for {args.identifier}")
                return 0

            try:
                delta = parse_duration(args.duration)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            until = now + delta
            previous = set_snooze(home, terminal_id, until)
            message = f"snoozed until {iso(until)}"
            if previous:
                message += f" (was {previous})"
            print(message)
            if home.load_state()[terminal_id].pinned:
                print("note: pane is pinned; snooze applies only after unpin")
            return 0
```

Add the imports at the top of `cli.py`:

```python
from .duration import parse_duration
from .exempt import resolve_terminal_id, set_pinned, set_snooze
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_exempt_cli.py -v`
Expected: 6 passed.

- [ ] **Step 6: Verify against the real CLI**

```bash
uv run attic pin w9:p9 ; echo "exit=$?  (want 1)"
uv run attic snooze w9:p9 4w ; echo "exit=$?  (want 1)"
uv run attic --help
```
Expected: both fail with a clear message and exit 1; `--help` lists the four new verbs alongside the existing five commands.

- [ ] **Step 7: Run the full suite and commit**

```bash
uv run pytest
git add src/attic/cli.py tests/test_exempt_cli.py
git commit -m "feat: add pin, unpin, snooze and unsnooze commands"
```

---

## Task 6: The inventory records every verdict

**Files:**
- Modify: `src/attic/inventory.py`, `src/attic/cli.py`
- Test: `tests/test_inventory.py`

**Interfaces:**
- Consumes: `Action`, `Archive`, `Skip` (policy)
- Produces: `append_inventory(home, panes, labels, now, verdicts: dict[str, tuple[str, str]] | None = None)` — `verdicts` maps `pane_id` to `(verdict, reason)`; each pane entry in the JSONL gains `verdict` and `reason`.

**Why this exists:** archives land in `index.jsonl`, but skips are persisted nowhere. "Why wasn't that pane reaped last Tuesday?" is currently unanswerable, and the Activity view in plan 2 depends on this record existing.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_inventory.py`:

```python
def test_inventory_records_the_verdict_and_reason(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    pane = mkpane("w4:p2")
    verdicts = {"w4:p2": ("skip", "pinned")}
    path = append_inventory(home, [pane], {}, NOW, verdicts)
    entry = json.loads(path.read_text(encoding="utf-8").strip())["panes"][0]
    assert entry["verdict"] == "skip"
    assert entry["reason"] == "pinned"


def test_verdicts_are_optional_so_old_callers_still_work(tmp_path):
    home = AtticHome(tmp_path)
    home.ensure()
    path = append_inventory(home, [mkpane("w4:p2")], {}, NOW)
    entry = json.loads(path.read_text(encoding="utf-8").strip())["panes"][0]
    assert entry["verdict"] is None
    assert entry["reason"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_inventory.py -k verdict -v`
Expected: FAIL — `TypeError: append_inventory() takes 4 positional arguments but 5 were given`

- [ ] **Step 3: Extend `append_inventory`**

In `src/attic/inventory.py`, change the signature and the per-pane dict:

```python
def append_inventory(
    home: AtticHome,
    panes: list[Pane],
    labels: dict[str, str],
    now: datetime,
    verdicts: dict[str, tuple[str, str]] | None = None,
) -> Path:
    """Append one line recording every pane and what was decided about it.

    Archives are logged in index.jsonl, but skips were previously persisted
    nowhere — so "why wasn't that reaped?" had no answer. Entries written before
    this change lack the fields; readers tolerate their absence.
    """
    home.ensure()
    verdicts = verdicts or {}
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
                "verdict": verdicts.get(p.pane_id, (None, None))[0],
                "reason": verdicts.get(p.pane_id, (None, None))[1],
            }
            for p in panes
        ],
    }
```

The remainder of the function is unchanged.

- [ ] **Step 4: Feed verdicts from `run_tick`**

`run_tick` currently calls `append_inventory` **before** `decide()`, so verdicts do not exist yet. Move the inventory write to after the actions are computed — it must still happen on every path, including when reaping is blocked. In `src/attic/cli.py`, delete the existing `append_inventory(home, panes, labels, now)` call and insert this immediately after `actions = _gate_on_resumability(...)`:

```python
    # Inventory is written after decide() so it can record WHY each pane was
    # skipped, but it is still unconditional: snapshotting is pure observation
    # and must not be gated on whether reaping is permitted.
    verdicts = {
        a.pane.pane_id: (("archive", "") if isinstance(a, Archive) else ("skip", a.reason))
        for a in actions
    }
    append_inventory(home, panes, labels, now, verdicts)
```

- [ ] **Step 5: Add the ordering regression test**

Add to `tests/test_tick.py`:

```python
def test_inventory_is_written_even_when_reaping_is_paused(tmp_path):
    """Snapshotting is observation, not action — the pause guard must not skip it."""
    pane = mkpane("w4:p2")
    home = home_with_clock(tmp_path, [pane])
    home.pause_path.touch()
    run_tick(home, FakeHerdrClient(panes=[pane]), NOW)
    line = json.loads((home.inventory_dir / "2026-08-13.jsonl").read_text(encoding="utf-8").strip())
    assert line["panes"][0]["verdict"] == "skip"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_inventory.py tests/test_tick.py -v`
Expected: all pass, including the pre-existing pause and dry-run tests.

- [ ] **Step 7: Run the full suite and commit**

```bash
uv run pytest
git add src/attic/inventory.py src/attic/cli.py tests/test_inventory.py tests/test_tick.py
git commit -m "feat: record per-pane verdicts and reasons in the inventory"
```

---

## Task 7: End-to-end verification against a real herdr session

**Files:** none — this task produces evidence, not code.

**Why:** every test above runs against `FakeHerdrClient`. The archiver's central bug — closing a session that `claude --resume` could not recover — was invisible to 110 unit tests and 5 read-only integration tests, and surfaced only when a real pane was closed and restored. Exemptions are simpler, but the same reasoning applies: `attic pin w4:p2` resolves an identifier against a *live* herdr, and nothing above proves that path works.

**Isolation is mandatory.** `decide()` evaluates every idle agent pane on whichever server it is pointed at. Run this against an isolated session, never the user's default socket.

- [ ] **Step 1: Start an isolated headless server**

```bash
env -u HERDR_PANE_ID -u HERDR_TAB_ID -u HERDR_WORKSPACE_ID -u HERDR_ENV -u HERDR_SOCKET_PATH \
  HERDR_SESSION=attic-exempt herdr server &
export HERDR_SOCKET_PATH=$HOME/.config/herdr/sessions/attic-exempt/herdr.sock
export ATTIC_HOME=$HOME/.attic-exempt
```

`herdr server` is the headless daemon; `herdr --session <name>` launches the TUI client and will fail without a terminal. Route with `HERDR_SOCKET_PATH` — `HERDR_SESSION` alone does **not** redirect the CLI and will silently keep talking to the default socket.

- [ ] **Step 2: Confirm isolation before doing anything else**

```bash
herdr pane list | python3 -c "import sys,json;print('staging:', len(json.load(sys.stdin)['result']['panes']))"
HERDR_SOCKET_PATH=$HOME/.config/herdr/herdr.sock herdr pane list \
  | python3 -c "import sys,json;print('live:', len(json.load(sys.stdin)['result']['panes']))"
```
Expected: staging is small and independent of live. **If the two counts match, stop** — you are pointed at the user's real sessions.

- [ ] **Step 3: Create a pane and an agent**

```bash
herdr workspace create --cwd "$ATTIC_HOME/work" --label exempt-test --no-focus
P=$(herdr pane split w1:p1 --direction right --no-focus | python3 -c "import sys,json;print(json.load(sys.stdin)['result']['pane']['pane_id'])")
herdr agent start exempttest --kind claude --pane "$P"
```

- [ ] **Step 4: Verify pin, snooze, and expiry against the live path**

```bash
mkdir -p "$ATTIC_HOME"
printf '{"idle_threshold_hours": 0.0006, "per_tick_cap": 1}\n' > "$ATTIC_HOME/config.json"
uv run attic tick                      # start the idle clock
uv run attic pin "$P"                  # resolves pane id -> terminal id
uv run attic reap --dry-run            # expect: skip ... (pinned)
uv run attic unpin "$P"
uv run attic snooze "$P" 4h
uv run attic reap --dry-run            # expect: skip ... (snoozed until ...)
uv run attic snooze "$P" 1h            # expect output to report "(was ...)"
uv run attic unsnooze "$P"
uv run attic reap --dry-run            # expect: ARCHIVE
```

Record the actual output of each command in the report. The pin/snooze lines
appearing in `reap --dry-run` is the property under test: the exemption is
visible in the same output the operator reads during the soak.

- [ ] **Step 5: Confirm state is keyed by terminal id**

```bash
python3 -m json.tool "$ATTIC_HOME/state.json"
```
Expected: keys are `term_...`, never `w1:p...`.

- [ ] **Step 6: Tear down**

```bash
herdr server stop
HERDR_SOCKET_PATH=$HOME/.config/herdr/herdr.sock herdr session delete attic-exempt
rm -rf "$ATTIC_HOME"
HERDR_SOCKET_PATH=$HOME/.config/herdr/herdr.sock herdr pane list \
  | python3 -c "import sys,json;print('live panes still:', len(json.load(sys.stdin)['result']['panes']))"
```
Expected: the staging session is gone and the user's live pane count is unchanged.

- [ ] **Step 7: Commit the evidence**

Write the recorded output to `docs/verification-exemptions.md` and commit.

```bash
git add docs/verification-exemptions.md
git commit -m "docs: record end-to-end verification of pin and snooze"
```

---

## Self-Review Notes

**Spec coverage:** every requirement in the spec's backend sections maps to a task —
`PaneState` fields (1), predicates inside `decide()` with pin outranking status (2),
absolute-deadline semantics and expiry (2), duration parsing (3), terminal-ID keying
and the replace-not-stack rule with previous-value reporting (4), the four verbs and
the pinned-pane note (5), inventory verdicts (6). The spec's TUI sections are
deliberately out of scope — they are plan 2.

**Deliberate omissions:** no `attic protected` listing verb (`reap --dry-run` already
answers it); no ranges or counts; `:archive` and the resumability interaction belong
to plan 2, since nothing here can archive anything.

**Type consistency:** `PaneState(first_idle_at, last_revision, snooze_until, pinned)`
is constructed positionally in `update_state` (Task 2) and by keyword elsewhere;
the field order above is the constructor order. `set_snooze` returns `str | None`
(the previous ISO stamp), not a `datetime` — Task 5's CLI relies on that.

**One ordering change to watch:** Task 6 moves the `append_inventory` call in
`run_tick` from before `decide()` to after it. The existing pause and protocol
tests assert inventory is written on those paths; Step 5 adds an explicit
regression test because that ordering is easy to break and the failure is silent.

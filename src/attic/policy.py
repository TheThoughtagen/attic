"""Pure reap policy. No I/O, no clock, no herdr."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, tzinfo

from .models import Pane
from .store import Config, PaneState

# Both mean "an agent sitting at a prompt with a live process, not working and
# not waiting on the human". `done` is not a detection state at all — herdr's
# claude manifest defines only working/blocked/idle/unknown — it is a completion
# badge layered on top, and `herdr agent explain` reports such a pane as `idle`.
# Verified: a `done` pane held a live 9h58m claude process, and observed panes
# decaying done -> idle on their own. Excluding it would ignore ~4 of 9 real
# sessions for no semantic reason.
REAPABLE_STATUSES = frozenset({"idle", "done"})


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
    """Serialize as UTC ISO-8601 with a Z suffix, enforcing the project-wide UTC
    contract here rather than trusting every call site. Three later modules import this.

    Naive datetimes are rejected rather than guessed at: astimezone() would read them
    as system local time, shifting first_idle_at by the local UTC offset (six hours
    in the author's zone). A fast idle clock against a four-hour threshold archives
    panes that are not eligible — the exact false positive this project exists to
    prevent. Raising aborts the tick and archives nothing, which is the safe direction.
    """
    if dt.tzinfo is None:
        raise ValueError("iso() requires a timezone-aware datetime")
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def parse_quiet_hours(window: str) -> tuple[time, time]:
    """Parse "HH:MM-HH:MM" into its two local wall-clock endpoints.

    Malformed input raises rather than being guessed at. A window read wrongly
    does not fail visibly — it silently moves the hours during which sessions
    get closed, which is the failure this module exists to avoid. Raising aborts
    the tick and archives nothing, matching iso()'s treatment of naive datetimes.
    """
    if not isinstance(window, str):
        # load_config passes raw JSON through, so a number or list can arrive
        # here. Guessing at it either crashes the daemon or silently disables
        # the window; both are worse than refusing.
        raise ValueError(f"quiet_hours must be a string like '22:00-08:00', got {window!r}")
    parts = window.split("-")
    if len(parts) != 2:
        raise ValueError(f"quiet_hours must look like '22:00-08:00', got {window!r}")
    try:
        start = time.fromisoformat(parts[0].strip())
        end = time.fromisoformat(parts[1].strip())
    except ValueError as exc:
        raise ValueError(f"quiet_hours has an unparseable time: {window!r}") from exc
    if start.tzinfo is not None or end.tzinfo is not None:
        # time.fromisoformat accepts offsets, so "22:00+02:00-08:00" splits into
        # one aware and one naive endpoint and the comparison below raises
        # TypeError. The window is local wall-clock by definition.
        raise ValueError(f"quiet_hours endpoints must not carry a UTC offset: {window!r}")
    if start == end:
        # Ambiguous: it could mean "never" or "always", and the two differ by
        # whether the tool ever reaps at all. Refuse instead of picking.
        raise ValueError(f"quiet_hours start and end are identical: {window!r}")
    return start, end


def _zone_label(now: datetime, tz: tzinfo | None) -> str:
    """A human-readable name for the zone the window was resolved in.

    Prefers the IANA key ("America/Chicago") over the abbreviation ("CDT"),
    because the abbreviation alone does not tell you whether the daemon resolved
    the same zone your shell did — which is the thing worth noticing.
    """
    key = getattr(tz, "key", None)
    return str(key or now.astimezone(tz).tzname() or "local")


def in_quiet_hours(now: datetime, window: str | None, tz: tzinfo | None = None) -> bool:
    """Whether this instant falls inside the local overnight window.

    `tz` defaults to the system zone, which is what makes "22:00" mean what the
    user means by it, and handles DST without arithmetic. Tests pass an explicit
    zone so they do not depend on the machine running them.

    The window is start-inclusive and end-exclusive: 08:00 is already the working
    day, and treating it as quiet would keep resetting the clock after you sit down.
    """
    if window is None or window == "":
        return False
    start, end = parse_quiet_hours(window)
    local = now.astimezone(tz).time()
    if start <= end:
        return start <= local < end
    return local >= start or local < end   # the window wraps past midnight


def update_state(
    panes: list[Pane], state: dict[str, PaneState], now: datetime,
    config: Config, tz: tzinfo | None = None,
) -> dict[str, PaneState]:
    """Maintain the idle clock. Panes that vanished are dropped.

    During quiet hours the clock is re-stamped on every tick rather than frozen.
    That is what makes the window "reset" it: when the window ends the newest
    stamp is one tick old, so the threshold runs from roughly the end of the
    window instead of from whenever the pane happened to go idle last night.
    """
    quiet = in_quiet_hours(now, config.quiet_hours, tz)
    updated: dict[str, PaneState] = {}
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
        if (quiet or prior is None or prior.last_revision != pane.revision
                or prior.first_idle_at is None):
            updated[pane.terminal_id] = PaneState(iso(now), pane.revision, snooze_until, pinned)
        else:
            updated[pane.terminal_id] = PaneState(
                prior.first_idle_at, pane.revision, snooze_until, pinned
            )
    return updated


def _verdict(pane: Pane, state: dict[str, PaneState], now, config,
             tz: tzinfo | None = None) -> Skip | datetime:
    """Return a Skip, or the datetime the pane went idle if it qualifies."""
    if not pane.is_agent:
        return Skip(pane, "not an agent pane")
    entry = state.get(pane.terminal_id)
    if entry is not None:
        # Operator intent outranks every automatic reason, so it is reported first.
        if entry.pinned:
            return Skip(pane, "pinned")
        if entry.snooze_until:
            until = _parse(entry.snooze_until)
            if now < until:
                return Skip(pane, f"snoozed until {entry.snooze_until}")
    if in_quiet_hours(now, config.quiet_hours, tz):
        # Naming the window and the resolved zone makes a misresolved timezone
        # visible in `attic reap --dry-run` and the inventory, rather than
        # silently shifting the hours during which sessions are closed.
        return Skip(pane, f"overnight hours ({config.quiet_hours} {_zone_label(now, tz)})")
    if pane.agent_status not in REAPABLE_STATUSES:
        return Skip(pane, f"status is {pane.agent_status}")
    if not pane.session_uuid:
        return Skip(pane, "no session uuid")
    if pane.focused:
        return Skip(pane, "focused")
    if entry is None or entry.first_idle_at is None:
        return Skip(pane, "idle clock not started")
    since = _parse(entry.first_idle_at)
    if (now - since).total_seconds() < config.idle_threshold_hours * 3600:
        return Skip(pane, "not idle long enough")
    return since


def decide(
    panes: list[Pane], state: dict[str, PaneState], now: datetime, config: Config,
    tz: tzinfo | None = None,
) -> list[Action]:
    """Return one verdict per pane, preserving input order."""
    verdicts: dict[str, Skip | datetime] = {
        p.pane_id: _verdict(p, state, now, config, tz) for p in panes
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

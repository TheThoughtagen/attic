# attic — implementation decision record

Every ruling made while executing
`docs/superpowers/plans/2026-08-13-attic-herdr-agent-archiver.md` across 12 tasks.
Preserved from the subagent-driven-development ledger, which is scratch and not committed.

Branch: 38 commits, 4e89ade..928f076. 110 unit tests, 5 live-herdr integration tests.

Ruling: branch instead of git worktree — `~/repos/attic` is a fresh single-purpose repo
created for this plan, with no concurrent work to isolate from and no pre-existing code a
worktree would protect. A feature branch gives the required "not on main" isolation.
Cost if wrong: none material; a worktree can be added later without touching history.

Ruling R1: Test-count totals in the plan ("58 total", "68 total") are wrong — actual
counts are ~65 and ~75. They are informational, not requirements. Implementers verify
"the suite passes", never a specific total. Cost if wrong: none; a mismatched count could
otherwise send an implementer hunting for tests that never existed.

Ruling R2: Tasks 8 and 9 both edit `src/attic/cli.py`. Their dispatches will state
explicitly that additions are appended to the existing parser and dispatch chain, leaving
prior subcommands intact. Cost if wrong: Task 9 silently drops `list`/`show`; caught by
Task 8's tests still being in the suite.

Ruling R3: `HerdrClient.tab_create` parses a response shape (`result.tab.panes[0].pane_id`)
that was NOT verified against live herdr during design — unlike every other command in the
Task 4 table, which was. Task 9's dispatch will require verifying the real shape against
the live server and adapting the parser to match before writing the implementation.
Cost if wrong: `attic restore` fails at runtime while all unit tests pass, because the
fake encodes the same guess as the code. This is the highest-risk item in the plan.

Ruling R4: The fixture is ground truth; the brief's test expectations were stale. Cause:
plan assertions were written from an earlier `herdr pane list` run, fixture captured ~10
minutes later — herdr is live data, and pane w4:p2 went idle→working with scrollback
growing 5839→6314 in between. Corrections issued: `w4:p2.agent_status` "idle"→"working",
`w4:p2.title` gains the "◐" prefix, `w3:p1.scroll_rows` 5930→6405.
Cost if wrong: none — the fixture is frozen once committed, so these values are stable
forever. The alternative (regenerating the fixture to match the plan) would have thrown
away real captured data to protect a typo.

Ruling R5: `Pane.title` uses `terminal_title_stripped` verbatim; no custom symbol
stripping. herdr strips "✳" (idle marker) but not "◐" (working spinner) — a real quirk.
Only `working` panes carry a spinner and attic archives only `idle` panes, so archived
titles will essentially never contain one; `slugify()` collapses non-alphanumerics
downstream regardless. Cost if wrong: an archive directory name could carry a stray
glyph-derived hyphen — cosmetic only. Added a regression test documenting the quirk.

Ruling R6: Reviewer is RIGHT and the finding is my defect, inherited verbatim from the
brief. `load_state()` guards `json.loads` but not the per-entry `int(...)` conversion, so
semantically-corrupt-but-valid JSON (`{"term_x": {"last_revision": "oops"}}`) raises
uncaught. Spec is binding: "corrupt or missing files degrade to defaults, never crash",
and tick must always exit 0. Fixed in plan + brief + dispatched to implementer.
Extended the fix beyond the review's ask to also validate `first_idle_at` is str|None —
a non-string timestamp survives load_state and then raises inside
`datetime.fromisoformat()` in Task 3, further from the cause.
Cost if wrong: a malformed entry is dropped rather than repaired, resetting that pane's
idle clock — which delays archiving, never causes a premature one. Fails in the safe
direction, consistent with every other error path in the design.

Ruling R7: `ensure()` correctly does NOT create `legacy_dir` (reviewer's ⚠️ item,
resolved by me). `legacy_dir` is consumed only by Task 12, a one-time manual cleanup that
does its own `mkdir -p`; it is not a runtime path and should not be created every tick.
Cost if wrong: Task 12 would need one `mkdir -p`, which its script already has.

Ruling R8: the TEST was wrong, not the code. `decide()` keeps returning one verdict per
pane in input order, as its docstring documents. Rejected the implementer's proposed fix
(emit Archives first, then the rest) because output order is presentation, not policy:
Task 7's `_print_verdicts` renders one line per pane for `attic reap --dry-run`, which is
the artifact the operator reads for days during the soak before granting reaping
authority. Pane-order output matches what they see in herdr; archives-first does not.
Split the over-specified test into two: one asserting the SET of archived panes (the
safety property) and one asserting input-order output (the presentation property).
Also caught that the original only asserted ONE of the two capped panes — with a 4h
threshold all five in that fixture are eligible, so p0 (5h) must be capped too and never
was checked. The corrected test asserts both.
Cost if wrong: `attic reap --dry-run` output reads in a less natural order. No archiving
behavior changes under either reading.

Ruling R9: the guard test was toothless and is the single most important regression guard
in the project. Rewrote it to key the stale entry under the PANE id with a matching
revision, forcing a buggy implementation down the preserve-clock branch.
VERIFIED BY MUTATION TEST rather than by argument: built a copy of policy.py with
`state.get(pane.terminal_id)` → `state.get(pane.pane_id)`; the OLD form PASSED against it
(proving it guarded nothing), the NEW form FAILED with KeyError: 'term_new'.
Cost if wrong: none — the implementation was already correct; this restores the guard
against a future regression.

Ruling R10: hardened `iso()` to `dt.astimezone(timezone.utc)` rather than string-replacing
"+00:00". Three modules import it, so the project-wide UTC contract is enforced at the
chokepoint instead of trusted at every call site. Added a non-UTC normalization test.
Cost if wrong: `astimezone` on a NAIVE datetime assumes system local time rather than
raising — re-reviewer asked to confirm no code path can reach `iso()` naive.

Ruling R11: `iso()` must REJECT naive datetimes, not normalize them. My own R10 hardening
opened this: `astimezone` on a naive datetime reads it as system local time, shifting
`first_idle_at` by the local UTC offset (6h in MST). A clock 6h fast against a 4h
threshold archives ineligible panes — a silent false positive, the exact failure this
project prevents. Raising is caught by run_tick, aborts the tick, archives nothing.
R10 had traded a cosmetic defect for a data-destroying one.
Cost if wrong: a future caller passing naive datetimes gets a loud abort instead of
silently wrong timestamps. Correct direction.
Task 3: fix round 3/5 (iso naive guard; commits 8eb83f7..699d331)

Ruling R12: closed Task 3 on controller verification. The round-3 re-reviewer went idle
without reporting twice. Verified the two named checks directly — guard at policy.py:39-40
sits BEFORE astimezone at :41; test uses `pytest.raises(ValueError, match="timezone-aware")`
(type AND message, no bare Exception in the file) — plus a behavioral run: naive→ValueError,
UTC→Z, UTC-6 06:00→12:00Z. Suite 36 passed.
Cost if wrong: a re-review seat was replaced by controller verification on a 2-line diff I
specified verbatim and confirmed behaviorally. The final whole-branch review still covers it.

Ruling R13: all three findings valid, all my defects — same root cause as R6 (guard placed
around the operation that looked risky while the actually-risky line sat bare beside it).
Fixed: (a) protocol() int conversion guarded; (b) `_json` validates isinstance(dict) and a
new `_result()` helper guarantees the result node is a dict, routed through pane_list /
tab_create / workspace_labels; (c) workspace_labels SKIPS entries missing workspace_id
rather than raising, deliberately consistent with R6's load_state ruling. `snapshot()`
still returns `_json` directly — correct, its payload is serialized, never indexed.
Also added argv-pinning test for pane_close/pane_run/tab_create, resolving the review's ⚠️
that only pane_read's argv was asserted — load-bearing because those shapes were verified
against live herdr and nothing else stopped a refactor from drifting off them.
Cost if wrong: a malformed workspace entry costs a manifest its display label instead of
killing the tick.

Ruling R14: closed Task 4 on controller verification after its re-reviewer went idle
without reporting (4th silent reviewer). Verified the three named checks directly:
`_result()` used by all three consumers (herdr.py:80,90,114), `snapshot()` correctly still
on `_json` (:84), only surviving `.get("result")` is inside `_result` itself (:49), no bare
`pytest.raises(Exception)` anywhere.

Ruling R15: fixed the orphan via a `created` flag + shutil.rmtree(ignore_errors=True).
The flag is load-bearing: mkdir without exist_ok raises FileExistsError (an OSError), and
cleaning up in that branch would delete an archive this call did not create — the one way
the fix could be worse than the bug.
Cost if wrong: deleting a real archive. Verified the flag is set only after mkdir succeeds.

Ruling R16 (security): acted on a background security review flagging world-readable
archive writes. Label ("credential writes") was imprecise — no credential files exist —
but the substance is right and understated: scrollback.txt captures whatever was on screen
in client repos (acme-corp, globex-systems, initech), which for
developer sessions means echoed API keys, .env dumps, tokens in URLs, connection strings.
Default umask made these 0644 / dirs 0755. Now 0700 dirs and 0600 files across archive.py
AND store.py's AtticHome.ensure() (a Task 2 file, edited here deliberately as one coherent
change). chmod placed after open and before write so the file is empty during the default
-permission window. The spec already called this data sensitive — that is WHY ~/.attic
lives outside any git tree — so this completes stated intent rather than adding scope.
Cost if wrong: none material. Ceiling worth knowing: archives remain plaintext, so any
process running as this user, and any backup sweeping ~, still reads them. 0700 raises the
bar from "any local account" to "this account".

Ruling R17: extended prune_archives to RECLAIM manifest-less directories past retention,
rather than skipping them. Task 5's fix stops new orphans but any that slip through (or
predate the fix) were invisible to `attic list` AND immune to prune — immortal garbage in
a tool built to reclaim resources. Gated on mtime past retention: nothing legitimate is
manifest-less and 30 days old, and the age bar guarantees an in-flight write is untouched.
Cost if wrong: deleting a partial archive whose pane was never closed (so no work lost).

Ruling R18: both findings valid — fourth appearance of the same defect class (except
clauses covering the exception I imagined, not the ones the code can raise). Fixed by
EXTRACTING `_archived_at() -> datetime | None` rather than widening the except tuple,
around the idea that a manifest we cannot trust is equivalent to no manifest, so it falls
through to the safer mtime-gated path. While writing the replacement I found a THIRD case
neither the reviewer nor I had caught: a naive timestamp parses fine, then raises TypeError
at `stamp < cutoff` — outside the try entirely, so no widened tuple would have caught it.
That is the argument for extraction over wider excepts: it makes the shape error
unrepresentable instead of caught.
Also unified rmtree(ignore_errors=True) across both branches AND guarded removed.append
on `not path.exists()` — with errors suppressed, an unguarded append reports deletions
that never happened, and Task 7 logs that list.
Cost if wrong: a malformed manifest shortens nothing; such dirs are mtime-gated, which is
strictly more conservative than the manifest date would have been.

Ruling R19: the PAUSE guard returned BEFORE decide(), so `attic reap --dry-run` produced
ZERO verdicts while paused. attic installs PAUSED and the soak procedure is "read dry-run
output for days, then remove PAUSE to grant authority" — so the safety procedure that
justifies auto-kill authority could not be performed at all. None of 9 tests caught it
because none combined PAUSE with dry_run=True. Restructured so guards gate EXECUTION, not
EVALUATION: verdicts computed every tick, `reason` reports why reaping was withheld.
Also ruled the CODE's ordering right and my prose wrong — the idle clock must advance
during a pause or dry-run durations would be unrelated to real idle time, making the soak
output actively misleading rather than merely absent.
Cost if wrong: on unpausing, panes already past threshold archive on the next tick. Correct
— they were genuinely idle, dry-run showed it for days, and the cap of 3 bounds the burst.
This is the most consequential defect of the project: it disabled the safety mechanism
rather than the functionality, and a broken safeguard looks exactly like a working one.

Ruling R21: corrected the reviewer on (3)'s severity. It claimed a failed index append
breaks `attic restore`'s ability to find the session. It does not — Task 8's
load_manifests scans archive DIRECTORIES for manifest.json and never reads index.jsonl,
so the session stays discoverable and restorable. Real loss is the index entry and its
close_failed marker. Guarded anyway; severity Minor-to-Important, not Critical.
Cost if wrong: none — the guard is correct either way; only the rationale changed.

Ruling R20: closed Task 7 on controller verification after its re-reviewer went idle
without reporting (5th silent reviewer). Both named checks verified directly plus the
end-to-end CLI runs above. Final whole-branch review still covers it.

Ruling R22: a mechanically checkable global constraint should be checked mechanically
across the whole tree, not enforced task by task. `grep -rn 'read_text()\|open(' src/attic/
| grep -v encoding` found all three instantly and would have at any point. Per-task
discipline leaked exactly where no task was looking. Second time an implementer caught a
global-constraint gap I let through, both times while working on a different task.

Ruling R23: `load_manifests` sorted on `m.get("archived_at", "")`. The default applies only
when the key is ABSENT — a manifest with `"archived_at": null` is present, so it compares
None against the other entries' strings and raises TypeError inside sorted(). main()
swallows it, so `attic list` printed NOTHING and exited 0, hiding healthy archives with no
error a user would ever see. Reproduced against the real CLI before fixing. This is the
recovery path failing in the one way nobody would notice.
Root cause worth recording: every other guard in this codebase is per-record (skip the bad
entry, continue), but SORTING is a cross-entry operation, so one malformed value poisons
the whole batch. Record-by-record validation does not compose to operations that compare
records. Fixed by extracting `_sort_key()` with the tolerance explicit and documented.
Also: exit-0 turned a loud failure silent — that is Task 7's "never crash the timer"
hardening working correctly and being actively harmful for an interactive command. A
blanket exception policy on a mixed-mode CLI hides exactly the errors a human is present
to see. Noted for the final review; not changed here.
Cost if wrong: none — the fix only widens tolerance.

Ruling R24: `resolve_id` used startswith only, so with IDs `...Z-a` and `...Z-ab` present,
supplying the COMPLETE id `...Z-a` was reported ambiguous. Two panes archived in the same
second produce exactly that shape. Exact match now short-circuits before prefix matching.

Ruling C (R25): took NEITHER. The real defect was upstream of both — I stored the resume
command only in a form a shell can consume, forcing a choice between violating the spec
(reconstruct → an archive written today replayed with TOMORROW's flags) and shell-execing
the display string (subshell + quoting surface + a redundant cd). Manifests now record BOTH:
`resume` (human-readable, for `attic show`) and `resume_argv` (structured tokens, executed
verbatim), with a fallback when resume_argv is absent/malformed. Required a cross-task edit
to archive.py (Task 5); authorized.
Note: option A would have passed EVERY test in the brief. The design point it violated was
expressed only in prose, and prose is not executable — hence the new test that sets
resume_argv to a hypothetical future flag and asserts restore replays THAT.
Cost if wrong: one extra manifest field.

Ruling R26: (2) is the THIRD instance of one root cause — main()'s blanket `except
Exception` is correct for the unattended tick (a crashing timer stops protecting the user)
and actively harmful for interactive commands where a human is waiting to be told
something. It ate Task 8's Critical too. Patched per-site again; carrying the STRUCTURAL
issue to the final review rather than a fourth patch. attic is two programs sharing an
entry point — a daemon that must never die and a CLI that must never lie.

Ruling R27: closed Task 9 on controller verification (6th silent re-reviewer). Three named
checks verified directly: append_index still AFTER pane_run (a failed start leaves no
restored_at entry claiming otherwise), `raise ... from exc` preserves the cause, and
resolve_id's LookupError is caught in its own try so an unknown id is not reported as a
restore failure.

Ruling R28: Task 11's implementer was forbidden from running install.sh or any launchctl
command. Installing a LaunchAgent that runs every 5 minutes and can close the user's panes
is a persistent, outward-facing system change and is the user's decision, not mine.
Artifacts built; installation surfaced to the user.
Cost if wrong: none — the user runs one command when ready.

Ruling R29: split Task 12, executing only its non-destructive half. The plan authorized
deleting six dead zellij sessions, but my earlier recon (~/.cache/zellij empty) suggested
their state was gone, and that turned out to be WRONG — the data lives at
~/Library/Caches/org.Zellij-Contributors.Zellij/contract_version_1/session_info/. All six
have real session-layout.kdl + session-metadata.kdl (pane/tab positions, working dirs,
commands; no scrollback — zellij never persists terminal output). 252K copied to
~/.attic/legacy/zellij/, originals untouched. Deletion commands written to
docs/cleanup-2026-08-13.md under "Pending user approval".
Approval given under one set of facts does not extend to a different set: the user approved
a plan step written when I believed there was nothing to preserve.
Cost if wrong: one round trip; the sessions remain until they say so.

Ruling R30 (CRITICAL): `catalog.py`'s `data.setdefault("id", path.name)` only fills a
MISSING key, so `{"id": 12345}` kept the int and `resolve_id`'s `.startswith` raised
AttributeError — swallowed, exit 0, `attic restore <valid-id>` showed the user NOTHING.
Reproduced against the real CLI. Same class as R23, fixed in `_sort_key` and missed in the
sibling function four lines below.

Ruling R31 (live security issue I CREATED): `~/.attic/legacy` was drwxr-xr-x holding 252K
of zellij session layouts — working directories and command lines for ~36 sessions — inside
the one tree the project had already decided must be owner-only. Task 12's `cp -R`
inherited the default umask. Fixed on the machine immediately (`chmod -R go-rwx`) and in
`ensure()` so it cannot recur. I introduced this while being careful about a DIFFERENT risk.

Ruling R32: the final reviewer overruled my structural framing of R26 and was right. Do NOT
split the entry point — fix the exit codes (~6 lines): interactive errors return 1, the
blanket handler returns 0 only for tick/reap. The daemon still never dies; the CLI stops
lying. It also caught that the OBVIOUS fix to the terminal_id fallback is wrong: a KeyError
there escapes HerdrClient's HerdrError contract and kills the tick. Skip the pane instead.

Ruling R33: `install.sh` never put `attic` on PATH, yet the README's soak — the multi-day
procedure that earns this tool authority to close live sessions — uses bare `attic list`.
Confirmed `command -v attic` exits 1. The safety procedure failed at step 1. Fixed via
`uv tool install --editable "$REPO"`.


## Plan 2 — exemptions (2026-08-13)

Ruling S1: T5 and T6 both edit cli.py. Both dispatches state explicitly that
additions are APPENDED to the existing parser and dispatch chain and that main()
is never rewritten. cli.py currently has 5 subcommands and ~35 tests covering
them; a broken existing test means something was clobbered.
Cost if wrong: silently dropped subcommands, caught by the existing suite.

Ruling S2: T6 relocates `append_inventory` from before `decide()` to after it.
The pause and protocol-mismatch tests assert inventory is still written on those
paths — that ordering is easy to break and the failure is silent (the tool keeps
reporting success while recording nothing). The plan includes an explicit
regression test; T6's dispatch names it as the thing to not lose.
Cost if wrong: the Activity view in plan 2 has no data, discovered much later.

Ruling E1: overruled the defer. Task 5 was adding callers to _mutate at that
moment, which is exactly when a latent footgun stops being latent. Fixed with a
field-name guard that RAISES rather than degrading gracefully — deliberate break
from this codebase's drop-the-entry pattern, because a typo'd field is a
programming error in our own code, not malformed data from disk, and should fail
loudly in development rather than silently in production.
Cost if wrong: a bad call site crashes a CLI command instead of silently no-oping.


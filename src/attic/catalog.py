"""Reading the archive catalog for `list`, `show`, and `restore`."""

from __future__ import annotations

import json

from .store import AtticHome


def load_manifests(home: AtticHome) -> list[dict]:
    # Scans archive *directories* for manifest.json rather than reading
    # index.jsonl — an archive stays discoverable here even if the index
    # append failed, so this is the actual recovery path, not a convenience
    # view. A single corrupt manifest is skipped, never fatal: `list` is how
    # a user finds a session they need back.
    out: list[dict] = []
    if not home.archive_dir.exists():
        return out
    for path in home.archive_dir.iterdir():
        if not path.is_dir():
            continue
        try:
            data = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        data.setdefault("id", path.name)
        out.append(data)
    return sorted(out, key=_sort_key, reverse=True)


def _sort_key(manifest: dict) -> str:
    """Sort by archived_at, tolerating entries where it is absent or not a string.

    A bare .get(default) is NOT enough: the default only applies when the key is
    missing. A null or numeric value compares against the other entries' strings and
    raises TypeError inside sorted(), which main() swallows — so `attic list` would
    print nothing and exit 0, hiding EVERY archive rather than the one corrupt entry.
    This is the recovery path; it must degrade per-record, never wholesale.
    """
    stamp = manifest.get("archived_at")
    return stamp if isinstance(stamp, str) else ""


def resolve_id(home: AtticHome, prefix: str) -> dict:
    manifests = load_manifests(home)
    for manifest in manifests:
        if manifest["id"] == prefix:  # a complete ID is never ambiguous
            return manifest
    matches = [m for m in manifests if m["id"].startswith(prefix)]
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

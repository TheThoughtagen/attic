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


def test_semantically_corrupt_entries_are_skipped_not_raised(tmp_path):
    """Valid JSON with wrong-typed fields must not raise: attic runs unattended
    from a LaunchAgent, so an exception here silently kills every future tick."""
    home = AtticHome(tmp_path)
    home.ensure()
    home.state_path.write_text(json.dumps({
        "term_good": {"first_idle_at": "2026-08-13T00:00:00Z", "last_revision": 3},
        "term_bad_revision": {"last_revision": "oops"},
        "term_null_revision": {"last_revision": None},
        "term_bad_timestamp": {"first_idle_at": 42, "last_revision": 1},
    }))
    loaded = home.load_state()
    assert set(loaded) == {"term_good"}
    assert loaded["term_good"].last_revision == 3
    assert loaded["term_good"].first_idle_at == "2026-08-13T00:00:00Z"

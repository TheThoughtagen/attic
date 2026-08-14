"""AtticApp behaviour that needs a running Textual app to exercise.

Only imported when the tui extra is installed — nothing here is reachable
from the tick path (see tests/test_daemon_purity.py).
"""

from __future__ import annotations

from fakes import FakeHerdrClient

from attic.herdr import HerdrError
from attic.store import AtticHome
from attic.tui.app import AtticApp


async def test_a_failed_refresh_is_visible_rather_than_silent(tmp_path):
    """A dashboard showing stale data while claiming to be current is worse than
    one that visibly fails — the user reasons from numbers that are no longer true."""

    class Dead(FakeHerdrClient):
        def pane_list(self):
            raise HerdrError("socket gone")

    home = AtticHome(tmp_path)
    home.ensure()
    app = AtticApp(home, Dead(), projects_root=tmp_path / "projects")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.last_error is not None
        assert "socket gone" in app.sub_title

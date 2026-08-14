import subprocess
import sys


def test_the_tick_path_does_not_import_textual():
    """attic tick runs unattended from a LaunchAgent with no LANG and no PATH.
    A broken or missing TUI dependency must never be able to stop reaping."""
    code = (
        "import sys, attic.cli, attic.evaluate, attic.policy, attic.archive, "
        "attic.inventory, attic.store, attic.herdr, attic.resumable, attic.exempt; "
        "print('textual' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", "the daemon path imported textual"

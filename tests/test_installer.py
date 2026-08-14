"""The installer is shipped code, and nothing else in this suite touches it.

Two defects reached a published release because of that gap: `install.sh`
referenced a plist filename that did not exist, and the plist itself carried
placeholder home paths left by a history rewrite. Both produce a LaunchAgent
that loads without error and then fails every five minutes into a log the user
never opens — the worst failure shape available, because nothing announces it.

These tests read the shipped files as text. They cannot run launchctl, but they
can assert the properties whose absence caused real breakage.
"""

import plistlib
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLIST = REPO / "launchd" / "com.attic.plist"
INSTALL = REPO / "install.sh"

TOKENS = ("__ATTIC_BIN__", "__HOME__", "__PATH__")


def test_the_plist_is_valid_and_parses():
    """A malformed plist fails at launchctl load time, on the user's machine."""
    data = plistlib.loads(PLIST.read_bytes())
    assert data["Label"] == "com.attic"
    assert data["StartInterval"] == 300
    assert data["RunAtLoad"] is False  # the first run must not precede PAUSE


def test_the_plist_hardcodes_nobody_s_home_directory():
    """It ships to other machines. A real path here is either wrong for them or
    a leak of ours — the history rewrite left '/Users/you/...' behind, which is
    both."""
    text = PLIST.read_text()
    assert "/Users/" not in text, "plist contains a literal home path"
    for token in TOKENS:
        assert token in text, f"{token} missing; install.sh substitutes it"


def test_the_plist_sets_the_two_variables_launchd_does_not_provide():
    """launchd supplies neither PATH nor LANG. attic shells out to herdr, and
    herdr's pane titles contain non-ASCII status glyphs; missing either one
    fails only under launchd, never in a terminal."""
    data = plistlib.loads(PLIST.read_bytes())
    env = data["EnvironmentVariables"]
    assert "__PATH__" in env["PATH"]
    assert env["LANG"].lower().endswith("utf-8")


def test_install_substitutes_every_token_and_refuses_if_any_remain():
    text = INSTALL.read_text()
    for token in TOKENS:
        assert re.search(rf"s\|{token}\|", text), f"install.sh never substitutes {token}"
    assert "refusing to install" in text, "no guard against an unsubstituted plist"


def test_install_pauses_before_loading_the_agent():
    """Ordering is load-bearing: an agent loaded before PAUSE exists could fire
    a tick that closes sessions the user never agreed to."""
    text = INSTALL.read_text()
    assert text.index("PAUSE") < text.index("launchctl load")


def test_install_sh_is_syntactically_valid():
    """A shell syntax error surfaces only when someone runs it."""
    proc = subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_the_substituted_plist_is_still_valid(tmp_path):
    """Run the real substitution and parse the result — the check that would
    have caught a plist referencing a file that does not exist."""
    out = tmp_path / "com.attic.plist"
    text = PLIST.read_text()
    for token, value in (("__ATTIC_BIN__", "/opt/bin/attic"),
                         ("__HOME__", "/home/tester"),
                         ("__PATH__", "/opt/bin:/usr/bin")):
        text = text.replace(token, value)
    out.write_text(text)

    data = plistlib.loads(out.read_bytes())
    assert data["ProgramArguments"] == ["/opt/bin/attic", "tick"]
    assert data["StandardOutPath"] == "/home/tester/.attic/logs/launchd.out.log"
    assert "__" not in out.read_text()

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

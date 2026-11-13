"""Shared test setup.

The spread fixtures use contracts that expire in November 2026, and the spread desk refuses
actions on an expired contract. So during tests its clock reads the fixtures' day (2026-11-03),
moved by whole days only: the time of day and every interval between calls stay real.
"""
import datetime as _dt

import pytest

FIXTURE_DAY = _dt.date(2026, 11, 3)


class _FixtureDay(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        real = _dt.datetime.now(tz)
        return real + _dt.timedelta(days=(FIXTURE_DAY - real.date()).days)


@pytest.fixture(autouse=True)
def _spread_desk_on_fixture_day(monkeypatch):
    try:
        from companion.spreads import spread_desk
    except ImportError:          # a build without the spreads package
        return
    monkeypatch.setattr(spread_desk, "datetime", _FixtureDay)


@pytest.fixture(autouse=True, scope="session")
def _netlog_in_a_temp_folder(tmp_path_factory):
    """Every Schwab call a test makes is recorded (companion/netlog.py is on by default): the record
    goes to a temp folder, never to the app's own data/netlog/ or data/logs/. Set once for the
    session and never put back, so a line the writer thread writes after a test still lands here."""
    from companion import netlog
    folder = tmp_path_factory.mktemp("netlog")
    netlog.DIR, netlog.LOG_DIR = folder / "netlog", folder / "logs"

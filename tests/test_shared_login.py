"""Shared login: another app's schwab-py token file copied into our tokens.db when it changes."""
import json
import sqlite3
import time

import pytest
from cryptography.fernet import Fernet

from companion import schwab


def _write(path, key, refresh="R1", access="A1", created=None, expires_at=None):
    created = created or time.time() - 3600
    payload = {"creation_timestamp": created,
               "token": {"access_token": access, "refresh_token": refresh, "id_token": "I",
                         "expires_in": 1800, "expires_at": expires_at or time.time() + 1200,
                         "token_type": "Bearer", "scope": "api"}}
    path.write_bytes(Fernet(key).encrypt(json.dumps(payload).encode()))


def _row(db):
    with sqlite3.connect(db) as con:
        return con.execute("select refresh_token, access_token, refresh_token_issued from schwabdev").fetchall()


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(schwab, "_shared_seen", None)
    return tmp_path / "token.fernet", tmp_path / "tokens.db", Fernet.generate_key().decode()


def test_copies_the_login_and_its_issue_time(files):
    src, db, key = files
    created = time.time() - 86400
    _write(src, key, created=created)
    assert schwab.sync_shared_token(src, key, db)
    (refresh, access, issued), = _row(db)
    assert (refresh, access) == ("R1", "A1")
    age = schwab.refresh_token_age_days(db)
    assert abs(age - 1.0) < 0.01          # 7-day clock starts when the other app signed in


def test_unchanged_file_is_not_read_again(files, monkeypatch):
    src, db, key = files
    _write(src, key)
    assert schwab.sync_shared_token(src, key, db)
    monkeypatch.setattr(schwab.json, "loads", lambda *_: pytest.fail("decrypted again"))
    assert not schwab.sync_shared_token(src, key, db)


def test_only_a_new_refresh_token_replaces_ours(files):
    src, db, key = files
    _write(src, key, refresh="R1", access="A1")
    schwab.sync_shared_token(src, key, db)
    with sqlite3.connect(db) as con:      # our own access-token refresh since then
        con.execute("update schwabdev set access_token = 'OURS'")
    time.sleep(0.01)
    _write(src, key, refresh="R1", access="A2")      # the other app refreshed its access token
    assert not schwab.sync_shared_token(src, key, db)
    assert _row(db)[0][1] == "OURS"
    time.sleep(0.01)
    _write(src, key, refresh="R2", access="A3")      # the other app signed in again
    assert schwab.sync_shared_token(src, key, db)
    assert _row(db)[0][:2] == ("R2", "A3")


def test_bad_key_or_missing_file_leaves_tokens_alone(files):
    src, db, key = files
    _write(src, key)
    assert not schwab.sync_shared_token(src, Fernet.generate_key().decode(), db)
    assert not schwab.sync_shared_token(src.with_name("missing"), key, db)
    assert not db.exists()


def test_shared_mode_never_signs_in_here(monkeypatch, tmp_path):
    from companion import signin
    monkeypatch.setattr(schwab, "SHARED_TOKEN_FILE", tmp_path / "token.fernet")
    monkeypatch.setenv("SCHWAB_APP_KEY", "k" * 32)
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s" * 16)
    monkeypatch.setenv("SCHWAB_SHARED_APP_NAME", "OpenVector")
    s = signin.SignIn(exchange_fn=lambda *_: pytest.fail("signed in"))
    r = s.start()
    assert r["ok"] is False and "OpenVector" in r["error"]
    assert s.status()["shared"] is True

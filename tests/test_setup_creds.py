"""Tests for setup_creds.py — fully offline (no network, ClobClient is stubbed)."""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import setup_creds  # noqa: E402


class StubApiCreds(SimpleNamespace):
    """Stand-in for py_clob_client.clob_types.ApiCreds."""


class StubClobClient:
    """Stand-in for py_clob_client.client.ClobClient — no network calls."""

    last_kwargs: dict | None = None

    def __init__(self, host=None, key=None, chain_id=None, signature_type=None, funder=None):
        StubClobClient.last_kwargs = {
            "host": host,
            "key": key,
            "chain_id": chain_id,
            "signature_type": signature_type,
            "funder": funder,
        }
        self.key = key

    def create_or_derive_api_creds(self):
        return StubApiCreds(
            api_key="stub-api-key",
            api_secret="stub-api-secret",
            api_passphrase="stub-api-passphrase",
        )


@pytest.fixture(autouse=True)
def stub_clob_client(monkeypatch):
    """Patch the lazily-imported py_clob_client.client.ClobClient with our stub."""
    import py_clob_client.client as clob_client_module

    monkeypatch.setattr(clob_client_module, "ClobClient", StubClobClient)
    yield


# --------------------------------------------------------------------------
# upsert_env_file
# --------------------------------------------------------------------------


def test_upsert_env_file_creates_new_file(tmp_path):
    env_path = tmp_path / ".env"
    setup_creds.upsert_env_file(
        env_path,
        {
            "POLY_API_KEY": "key1",
            "POLY_API_SECRET": "secret1",
            "POLY_API_PASSPHRASE": "pass1",
        },
    )

    assert env_path.exists()
    content = env_path.read_text()
    assert "POLY_API_KEY=key1" in content
    assert "POLY_API_SECRET=secret1" in content
    assert "POLY_API_PASSPHRASE=pass1" in content


def test_upsert_env_file_sets_chmod_600(tmp_path):
    env_path = tmp_path / ".env"
    setup_creds.upsert_env_file(env_path, {"POLY_API_KEY": "key1"})

    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == (stat.S_IRUSR | stat.S_IWUSR)


def test_upsert_env_file_preserves_unrelated_lines(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "POLY_PRIVATE_KEY=0xdeadbeef\n"
        "# a comment\n"
        "STARTING_BANKROLL=1.0\n"
    )

    setup_creds.upsert_env_file(env_path, {"POLY_API_KEY": "key1"})

    content = env_path.read_text()
    assert "POLY_PRIVATE_KEY=0xdeadbeef" in content
    assert "# a comment" in content
    assert "STARTING_BANKROLL=1.0" in content
    assert "POLY_API_KEY=key1" in content


def test_upsert_env_file_is_idempotent_and_replaces_in_place(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "POLY_PRIVATE_KEY=0xdeadbeef\n"
        "POLY_API_KEY=old-key\n"
        "POLY_API_SECRET=old-secret\n"
        "POLY_API_PASSPHRASE=old-pass\n"
        "BOT_MODE=safe\n"
    )

    setup_creds.upsert_env_file(
        env_path,
        {
            "POLY_API_KEY": "new-key",
            "POLY_API_SECRET": "new-secret",
            "POLY_API_PASSPHRASE": "new-pass",
        },
    )

    lines = env_path.read_text().splitlines()

    # Exactly one occurrence of each key — no duplicate lines appended.
    assert lines.count("POLY_API_KEY=new-key") == 1
    assert sum(1 for l in lines if l.startswith("POLY_API_KEY=")) == 1
    assert sum(1 for l in lines if l.startswith("POLY_API_SECRET=")) == 1
    assert sum(1 for l in lines if l.startswith("POLY_API_PASSPHRASE=")) == 1

    assert "POLY_API_SECRET=new-secret" in lines
    assert "POLY_API_PASSPHRASE=new-pass" in lines
    # Old values gone.
    assert "POLY_API_KEY=old-key" not in lines
    # Unrelated lines untouched, and the key line stayed in the same relative
    # position (replaced in place, not moved to the end).
    assert lines[0] == "POLY_PRIVATE_KEY=0xdeadbeef"
    assert lines[1] == "POLY_API_KEY=new-key"
    assert "BOT_MODE=safe" in lines


def test_upsert_env_file_running_twice_is_stable(tmp_path):
    env_path = tmp_path / ".env"
    values = {
        "POLY_API_KEY": "key1",
        "POLY_API_SECRET": "secret1",
        "POLY_API_PASSPHRASE": "pass1",
    }
    setup_creds.upsert_env_file(env_path, values)
    first_pass = env_path.read_text()

    setup_creds.upsert_env_file(env_path, values)
    second_pass = env_path.read_text()

    assert first_pass == second_pass


# --------------------------------------------------------------------------
# resolve_private_key
# --------------------------------------------------------------------------


def test_resolve_private_key_from_cli_arg():
    assert setup_creds.resolve_private_key("0xabc") == "0xabc"


def test_resolve_private_key_from_env(monkeypatch):
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0xenvkey")
    assert setup_creds.resolve_private_key(None) == "0xenvkey"


def test_resolve_private_key_missing_raises(monkeypatch):
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    with pytest.raises(SystemExit):
        setup_creds.resolve_private_key(None)


# --------------------------------------------------------------------------
# derive_creds (stubbed ClobClient — no network)
# --------------------------------------------------------------------------


def test_derive_creds_uses_stub_and_never_touches_network():
    creds = setup_creds.derive_creds("0xdeadbeef", funder="0xfunder", signature_type=1)

    assert creds.api_key == "stub-api-key"
    assert creds.api_secret == "stub-api-secret"
    assert creds.api_passphrase == "stub-api-passphrase"
    assert StubClobClient.last_kwargs == {
        "host": setup_creds.CLOB_HOST,
        "key": "0xdeadbeef",
        "chain_id": setup_creds.CHAIN_ID,
        "signature_type": 1,
        "funder": "0xfunder",
    }


# --------------------------------------------------------------------------
# main() end-to-end with --write-env
# --------------------------------------------------------------------------


def test_main_write_env_end_to_end(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0xdeadbeef")
    monkeypatch.delenv("POLY_FUNDER_ADDRESS", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["setup_creds.py", "--write-env", "--env-file", str(env_path)]
    )

    setup_creds.main()

    out = capsys.readouterr().out
    assert "0xdeadbeef" not in out  # private key is never printed
    assert "stub-api-key" in out

    content = env_path.read_text()
    assert "POLY_API_KEY=stub-api-key" in content
    assert "POLY_API_SECRET=stub-api-secret" in content
    assert "POLY_API_PASSPHRASE=stub-api-passphrase" in content
    assert "0xdeadbeef" not in content  # private key never written either

    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == (stat.S_IRUSR | stat.S_IWUSR)


def test_main_without_write_env_does_not_touch_file(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0xdeadbeef")
    monkeypatch.setattr(sys, "argv", ["setup_creds.py", "--env-file", str(env_path)])

    setup_creds.main()

    assert not env_path.exists()
    out = capsys.readouterr().out
    assert "stub-api-key" in out

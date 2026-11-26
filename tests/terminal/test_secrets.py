"""The key lives in the Keychain (here: an in-memory keyring), DEEPSEEK_API_KEY overrides it."""

from __future__ import annotations

from conftest import TEST_KEY

from daedalus import secrets


def test_no_key_anywhere() -> None:
    assert secrets.get_key() is None and secrets.key_source() is None


def test_keychain_then_env_override(memory_keyring, monkeypatch) -> None:
    secrets.set_key("deepseek", f"  {TEST_KEY}\n")
    assert memory_keyring.store == {("daedalus", "deepseek"): TEST_KEY}
    assert secrets.get_key() == TEST_KEY and secrets.key_source() == "Keychain"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    assert secrets.get_key() == "sk-from-env" and secrets.key_source() == "DEEPSEEK_API_KEY"


def test_set_uses_getpass_and_status_never_prints_the_key(memory_keyring, monkeypatch, capsys) -> None:
    monkeypatch.setattr(secrets.getpass, "getpass", lambda prompt: TEST_KEY)
    assert secrets.main(["set", "deepseek"]) == 0
    assert secrets.main(["status"]) == 0
    out = capsys.readouterr()
    assert TEST_KEY not in out.out + out.err
    assert "deepseek: key present (Keychain)" in out.out
    assert memory_keyring.store[("daedalus", "deepseek")] == TEST_KEY


def test_status_without_key(capsys) -> None:
    secrets.main(["status"])
    assert "no key; set one with: python -m daedalus.secrets set deepseek" in capsys.readouterr().out


def test_empty_key_refused(monkeypatch, memory_keyring) -> None:
    monkeypatch.setattr(secrets.getpass, "getpass", lambda prompt: "   ")
    assert secrets.main(["set", "deepseek"]) == 1
    assert memory_keyring.store == {}

from __future__ import annotations

import pytest

from daedalus.terminal.config import Config, ConfigError, load_config


def test_missing_file_is_defaults(tmp_path) -> None:
    c = load_config(tmp_path / "none.toml")
    assert c == Config()
    assert (c.session_cap_usd, c.daily_cap_usd, c.max_tokens, c.reasoning_effort) == (1.00, 3.00, 8000, "low")


def test_values_and_int_caps(tmp_path) -> None:
    p = tmp_path / "c.toml"
    p.write_text('model = "deepseek-v4-pro"\nsession_cap_usd = 2\ndaily_cap_usd = 5.5\nmax_tokens = 4000\n')
    c = load_config(p)
    assert (c.model, c.session_cap_usd, c.daily_cap_usd, c.max_tokens) == ("deepseek-v4-pro", 2.0, 5.5, 4000)


@pytest.mark.parametrize("text, says", [
    ("dailly_cap_usd = 10\n", "unknown key"),
    ('daily_cap_usd = "lots"\n', "must be a float"),
    ('model = "gpt-9"\n', "has no price"),
    ('reasoning_effort = "medium"\n', "reasoning_effort"),
    ("max_tokens = 0\n", "max_tokens"),
    ("session_cap_usd = -1\n", "negative"),
    ("not toml", "not valid TOML"),
])
def test_bad_config(tmp_path, text, says) -> None:
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert says in str(exc.value)

from pathlib import Path

from wikilag.config import load_config


def test_loads_the_repo_default():
    config = load_config(Path("config/default.toml"))
    assert config.stream.url.startswith("https://stream.wikimedia.org")
    assert config.join.watermark_seconds == 21600
    assert "enwiki" in config.filters.wikis


def test_env_var_overrides_the_default(tmp_path, monkeypatch):
    override = tmp_path / "test.toml"
    override.write_text(
        Path("config/default.toml").read_text().replace("21600", "900"),
        encoding="utf-8",
    )
    monkeypatch.setenv("WIKILAG_CONFIG", str(override))
    assert load_config().join.watermark_seconds == 900

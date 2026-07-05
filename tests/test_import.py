"""Smoke test: importing the application must succeed (no import-time crashes)."""


def test_imports():
    from src.main import bot

    assert bot is not None

    from src.config import CONFIG

    assert isinstance(CONFIG, dict)

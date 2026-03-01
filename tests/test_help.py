import pytest
from src.constants import DISCORD_MESSAGE_LIMIT


class TestBuildHelpSections:
    def test_all_chunks_under_discord_limit(self):
        from src.commands.help_commands import _build_help_sections
        sections = _build_help_sections()
        assert len(sections) > 0
        for i, chunk in enumerate(sections):
            assert len(chunk) <= DISCORD_MESSAGE_LIMIT, (
                f"Help chunk {i} is {len(chunk)} chars, exceeds {DISCORD_MESSAGE_LIMIT}"
            )

    def test_includes_core_commands(self):
        from src.commands.help_commands import _build_help_sections
        full_text = "\n".join(_build_help_sections())
        for cmd in ["fezhelp", "track", "config", "receiver", "new thread"]:
            assert cmd in full_text.lower(), f"Expected '{cmd}' in help text"

    def test_excludes_hidden_commands(self):
        from src.commands.help_commands import _build_help_sections
        full_text = "\n".join(_build_help_sections())
        assert "addcustom" not in full_text.lower()
        assert "!add" not in full_text

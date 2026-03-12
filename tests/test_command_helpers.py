import pytest
from unittest.mock import AsyncMock, patch
from src.commands.helpers import (
    _parse_json_arg,
    _deepcopy_cfg,
    FilterType,
    _INCLUDE_KEY_MAP,
    _EXCLUDE_KEY_MAP,
    _ACTION_MAP,
    mutate_filter,
)


class TestParseJsonArg:
    def test_valid_json_string(self):
        assert _parse_json_arg('"hello"') == "hello"

    def test_valid_json_list(self):
        assert _parse_json_arg('["a", "b"]') == ["a", "b"]

    def test_valid_json_bool(self):
        assert _parse_json_arg("true") is True

    def test_valid_json_number(self):
        assert _parse_json_arg("42") == 42

    def test_invalid_json_returns_raw_string(self):
        assert _parse_json_arg("not json") == "not json"

    def test_empty_string(self):
        assert _parse_json_arg("") == ""


class TestDeepcopyConfig:
    def test_deep_copies_dict(self):
        original = {"a": [1, 2, 3]}
        copy = _deepcopy_cfg(original)
        copy["a"].append(4)
        assert original["a"] == [1, 2, 3]

    def test_non_serializable_returns_original(self):
        obj = object()
        result = _deepcopy_cfg(obj)
        assert result is obj


class TestFilterTypeMaps:
    def test_all_filter_types_have_include_key(self):
        for ft in FilterType:
            assert ft in _INCLUDE_KEY_MAP

    def test_all_filter_types_have_exclude_key(self):
        for ft in FilterType:
            assert ft in _EXCLUDE_KEY_MAP

    def test_action_map_has_all_four_actions(self):
        assert set(_ACTION_MAP.keys()) == {"add_include", "remove_include", "add_exclude", "remove_exclude"}


class TestMutateFilter:
    @pytest.mark.asyncio
    @patch("src.commands.helpers.mutate_entity_config_list")
    async def test_add_include_calls_correct_key(self, mock_mutate):
        mock_mutate.return_value = (True, ["Alice"])
        ctx = AsyncMock()
        await mutate_filter(ctx, "threads", "111", FilterType.USER, "Alice", action="add_include")
        mock_mutate.assert_called_once_with("threads", "111", "userIncludeList", add=["Alice"])
        ctx.reply.assert_called_once()
        assert "Now tracking" in ctx.reply.call_args[0][0]

    @pytest.mark.asyncio
    @patch("src.commands.helpers.mutate_entity_config_list")
    async def test_add_exclude_calls_correct_key(self, mock_mutate):
        mock_mutate.return_value = (True, ["^Bot:.*"])
        ctx = AsyncMock()
        await mutate_filter(ctx, "threads", "111", FilterType.PAGE, "^Bot:.*", action="add_exclude")
        mock_mutate.assert_called_once_with("threads", "111", "pageExcludePatterns", add=["^Bot:.*"])
        assert "Now ignoring" in ctx.reply.call_args[0][0]

    @pytest.mark.asyncio
    @patch("src.commands.helpers.mutate_entity_config_list")
    async def test_remove_include_calls_correct_key(self, mock_mutate):
        mock_mutate.return_value = (True, [])
        ctx = AsyncMock()
        await mutate_filter(ctx, "receivers", "rk", FilterType.SUMMARY, "vandal", action="remove_include")
        mock_mutate.assert_called_once_with("receivers", "rk", "summaryIncludePatterns", remove=["vandal"])
        assert "Removed from tracking" in ctx.reply.call_args[0][0]

    @pytest.mark.asyncio
    @patch("src.commands.helpers.mutate_entity_config_list")
    async def test_remove_exclude_calls_correct_key(self, mock_mutate):
        mock_mutate.return_value = (True, [])
        ctx = AsyncMock()
        await mutate_filter(ctx, "threads", "111", FilterType.USER, "Bob", action="remove_exclude")
        mock_mutate.assert_called_once_with("threads", "111", "userExcludeList", remove=["Bob"])
        assert "Removed from ignore list" in ctx.reply.call_args[0][0]

    @pytest.mark.asyncio
    @patch("src.commands.helpers.mutate_entity_config_list")
    async def test_failure_replies_with_error(self, mock_mutate):
        mock_mutate.return_value = (False, None)
        ctx = AsyncMock()
        await mutate_filter(ctx, "threads", "999", FilterType.USER, "Alice", action="add_include")
        assert "Failed to update" in ctx.reply.call_args[0][0]

    @pytest.mark.asyncio
    @patch("src.commands.helpers.mutate_entity_config_list")
    async def test_entity_label_prefix(self, mock_mutate):
        mock_mutate.return_value = (True, ["Alice"])
        ctx = AsyncMock()
        await mutate_filter(ctx, "receivers", "rk", FilterType.USER, "Alice",
                            action="add_include", entity_label="Receiver `rk`")
        assert ctx.reply.call_args[0][0].startswith("Receiver `rk`")

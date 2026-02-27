import pytest
from src.commands.helpers import (
    _parse_json_arg,
    _deepcopy_cfg,
    FilterType,
    _INCLUDE_KEY_MAP,
    _EXCLUDE_KEY_MAP,
    _ACTION_MAP,
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

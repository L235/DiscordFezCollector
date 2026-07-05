"""Tests for the FilterConfig schema and command-layer config validation."""

from src.commands.helpers import require_list_key, validate_config_value
from src.config import DEFAULT_CUSTOM_CONFIG
from src.models import CustomFilter, FilterConfig

# ── FilterConfig (de)serialization ────────────────────────────────────────── #

def test_defaults_roundtrip():
    cfg = FilterConfig()
    assert cfg.to_dict() == {
        "siteName": "",
        "pageIncludePatterns": [],
        "pageExcludePatterns": [],
        "userExcludeList": [],
        "userIncludeList": [],
        "summaryIncludePatterns": [],
        "summaryExcludePatterns": [],
        "linkStyle": "title",
    }
    assert FilterConfig.from_dict(cfg.to_dict()) == cfg


def test_from_dict_missing_keys_default():
    cfg = FilterConfig.from_dict({"userIncludeList": ["Alice"]})
    assert cfg.user_include_list == ["Alice"]
    assert cfg.page_include_patterns == []
    assert cfg.link_style == "title"


def test_from_dict_coerces_scalars_to_lists():
    cfg = FilterConfig.from_dict({"pageIncludePatterns": "Sandbox"})
    assert cfg.page_include_patterns == ["Sandbox"]


def test_from_dict_scalar_user_list_stays_whole():
    # Regression: a scalar string in userIncludeList must become ["Alice"],
    # not be iterated character-by-character when CustomFilter builds a set.
    filt = CustomFilter({"userIncludeList": "Alice"})
    assert filt.user_include == {"Alice"}


def test_from_dict_normalizes_bad_link_style():
    assert FilterConfig.from_dict({"linkStyle": "banana"}).link_style == "title"
    assert FilterConfig.from_dict({"linkStyle": "action"}).link_style == "action"


def test_from_dict_ignores_unknown_keys():
    cfg = FilterConfig.from_dict({"noSuchKey": 1, "siteName": "en.wikipedia.org"})
    assert cfg.site_name == "en.wikipedia.org"


def test_to_dict_returns_independent_lists():
    cfg = FilterConfig()
    d = cfg.to_dict()
    d["userIncludeList"].append("intruder")
    assert cfg.user_include_list == []


def test_default_custom_config_matches_schema():
    assert DEFAULT_CUSTOM_CONFIG == FilterConfig().to_dict()


def test_custom_filter_accepts_filter_config_instance():
    filt = CustomFilter(FilterConfig(user_include_list=["Alice"], link_style="action"))
    assert filt.user_include == {"Alice"}
    assert filt.link_style == "action"


# ── Command-layer validation ──────────────────────────────────────────────── #

def test_validate_unknown_key_rejected():
    err, _ = validate_config_value("pageIncludePattern", ["typo'd key"])
    assert err is not None
    assert "pageIncludePattern" in err


def test_validate_link_style():
    err, val = validate_config_value("linkStyle", "action")
    assert err is None and val == "action"
    err, _ = validate_config_value("linkStyle", "banana")
    assert err is not None


def test_validate_site_name_must_be_string():
    err, val = validate_config_value("siteName", "en.wikipedia.org")
    assert err is None and val == "en.wikipedia.org"
    err, _ = validate_config_value("siteName", 42)
    assert err is not None


def test_validate_list_key_normalizes_scalar():
    err, val = validate_config_value("pageIncludePatterns", "Sandbox")
    assert err is None and val == ["Sandbox"]


def test_require_list_key():
    assert require_list_key("pageIncludePatterns") is None
    assert require_list_key("linkStyle") is not None
    assert require_list_key("bogus") is not None


def test_validate_rejects_invalid_regex():
    # A persisted invalid regex crash-loops the pipeline; it must never
    # get past the command boundary.
    err, _ = validate_config_value("pageIncludePatterns", "(")
    assert err is not None and "Invalid regex" in err
    err, _ = validate_config_value("summaryIncludePatterns", ["ok", "*bad"])
    assert err is not None and "Invalid regex" in err


def test_validate_accepts_valid_regex_list():
    err, val = validate_config_value("pageIncludePatterns", ["^WP:AN", "Sandbox$"])
    assert err is None and val == ["^WP:AN", "Sandbox$"]


def test_validate_rejects_null_and_bool_for_list_keys():
    # `/config set userIncludeList null` must not silently track a user
    # literally named "None" (and true must not become "True").
    err, _ = validate_config_value("userIncludeList", None)
    assert err is not None and "clear" in err
    err, _ = validate_config_value("userIncludeList", True)
    assert err is not None
    err, _ = validate_config_value("userIncludeList", [None])
    assert err is not None


def test_validate_site_name_null_clears():
    # JSON null was the historical idiom for clearing the site restriction.
    err, val = validate_config_value("siteName", None)
    assert err is None and val == ""


def test_list_keys_derived_from_schema():
    from src.models import FilterConfig

    assert FilterConfig.LIST_KEYS == frozenset({
        "pageIncludePatterns", "pageExcludePatterns",
        "userIncludeList", "userExcludeList",
        "summaryIncludePatterns", "summaryExcludePatterns",
    })
    assert FilterConfig.PATTERN_KEYS == frozenset({
        "pageIncludePatterns", "pageExcludePatterns",
        "summaryIncludePatterns", "summaryExcludePatterns",
    })

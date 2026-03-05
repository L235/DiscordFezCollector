import pytest
from src.models import CustomFilter, _compile_pattern_list


# ── _compile_pattern_list ─────────────────────────────────────────────────── #

class TestCompilePatternList:
    def test_empty_list_returns_none(self):
        assert _compile_pattern_list([]) is None

    def test_single_pattern(self):
        rx = _compile_pattern_list(["foo"])
        assert rx is not None
        assert rx.search("foobar")

    def test_multiple_patterns_or_joined(self):
        rx = _compile_pattern_list(["foo", "bar"])
        assert rx.search("foo")
        assert rx.search("bar")
        assert not rx.search("baz")

    def test_case_insensitive(self):
        rx = _compile_pattern_list(["Foo"])
        assert rx.search("FOO")
        assert rx.search("foo")

    def test_string_input_coerced(self):
        rx = _compile_pattern_list("singlepattern")
        assert rx is not None
        assert rx.search("singlepattern")

    def test_empty_strings_filtered(self):
        rx = _compile_pattern_list(["", "foo", ""])
        assert rx is not None
        assert rx.search("foo")

    def test_all_empty_returns_none(self):
        assert _compile_pattern_list(["", ""]) is None

    def test_non_list_coerced(self):
        rx = _compile_pattern_list(42)
        assert rx is not None
        assert rx.search("42")


# ── CustomFilter.matches ──────────────────────────────────────────────────── #

def _make_change(user="TestUser", title="TestPage", comment="test edit",
                 server_name="en.wikipedia.org", change_type="edit", **extra):
    change = {
        "user": user,
        "title": title,
        "comment": comment,
        "server_name": server_name,
        "type": change_type,
    }
    change.update(extra)
    return change


class TestCustomFilterUserInclude:
    def test_user_in_include_matches(self):
        f = CustomFilter({"userIncludeList": ["Alice"]})
        assert f.matches(_make_change(user="Alice"))

    def test_user_not_in_include_no_match(self):
        f = CustomFilter({"userIncludeList": ["Alice"]})
        assert not f.matches(_make_change(user="Bob"))

    def test_user_include_case_sensitive(self):
        f = CustomFilter({"userIncludeList": ["Alice"]})
        assert not f.matches(_make_change(user="alice"))


class TestCustomFilterUserExclude:
    def test_user_in_exclude_blocked(self):
        f = CustomFilter({"userIncludeList": ["Alice"], "userExcludeList": ["Alice"]})
        assert not f.matches(_make_change(user="Alice"))

    def test_exclude_takes_precedence_over_include(self):
        f = CustomFilter({
            "userIncludeList": ["BadUser"],
            "userExcludeList": ["BadUser"],
        })
        assert not f.matches(_make_change(user="BadUser"))


class TestCustomFilterPagePatterns:
    def test_page_include_regex_match(self):
        f = CustomFilter({"pageIncludePatterns": ["^Talk:.*"]})
        assert f.matches(_make_change(title="Talk:Something"))

    def test_page_include_no_match(self):
        f = CustomFilter({"pageIncludePatterns": ["^Talk:.*"]})
        assert not f.matches(_make_change(title="Main Page"))

    def test_page_exclude_blocks_match(self):
        f = CustomFilter({
            "pageIncludePatterns": [".*"],
            "pageExcludePatterns": ["^User:.*"],
        })
        assert not f.matches(_make_change(title="User:Someone"))
        assert f.matches(_make_change(title="Article"))

    def test_page_patterns_case_insensitive(self):
        f = CustomFilter({"pageIncludePatterns": ["test"]})
        assert f.matches(_make_change(title="TEST PAGE"))


class TestCustomFilterSummaryPatterns:
    def test_summary_include_matches_comment(self):
        f = CustomFilter({"summaryIncludePatterns": ["vandal"]})
        assert f.matches(_make_change(comment="reverted vandalism"))

    def test_summary_include_matches_log_action_comment(self):
        f = CustomFilter({"summaryIncludePatterns": ["blocked"]})
        assert f.matches(_make_change(comment="", log_action_comment="blocked user"))

    def test_summary_exclude_blocks(self):
        f = CustomFilter({
            "summaryIncludePatterns": [".*"],
            "summaryExcludePatterns": ["bot edit"],
        })
        assert not f.matches(_make_change(comment="bot edit"))
        assert f.matches(_make_change(comment="human edit"))


class TestCustomFilterSiteName:
    def test_site_name_matches(self):
        f = CustomFilter({"siteName": "en.wikipedia.org", "userIncludeList": ["Alice"]})
        assert f.matches(_make_change(user="Alice", server_name="en.wikipedia.org"))

    def test_site_name_mismatch_blocks(self):
        f = CustomFilter({"siteName": "en.wikipedia.org", "userIncludeList": ["Alice"]})
        assert not f.matches(_make_change(user="Alice", server_name="de.wikipedia.org"))

    def test_site_name_case_insensitive(self):
        f = CustomFilter({"siteName": "EN.WIKIPEDIA.ORG", "userIncludeList": ["Alice"]})
        assert f.matches(_make_change(user="Alice", server_name="en.wikipedia.org"))

    def test_empty_site_name_matches_all(self):
        f = CustomFilter({"siteName": "", "userIncludeList": ["Alice"]})
        assert f.matches(_make_change(user="Alice", server_name="any.wiki.org"))


class TestCustomFilterCombined:
    def test_no_filters_no_match(self):
        f = CustomFilter({})
        assert not f.matches(_make_change())

    def test_empty_config_no_match(self):
        f = CustomFilter({
            "userIncludeList": [],
            "pageIncludePatterns": [],
            "summaryIncludePatterns": [],
        })
        assert not f.matches(_make_change())

    def test_multiple_include_types_any_matches(self):
        f = CustomFilter({
            "userIncludeList": ["Alice"],
            "pageIncludePatterns": ["^Talk:.*"],
        })
        assert f.matches(_make_change(user="Alice", title="Main Page"))
        assert f.matches(_make_change(user="Bob", title="Talk:Something"))
        assert not f.matches(_make_change(user="Bob", title="Main Page"))

    def test_exclude_checked_before_all_includes(self):
        f = CustomFilter({
            "userIncludeList": ["Spammer"],
            "userExcludeList": ["Spammer"],
            "pageIncludePatterns": [".*"],
        })
        # User is excluded even though page matches
        assert not f.matches(_make_change(user="Spammer", title="Anything"))

    def test_missing_title_with_site_name_returns_false(self):
        f = CustomFilter({"siteName": "en.wikipedia.org", "userIncludeList": ["Alice"]})
        change = _make_change(user="Alice", server_name="en.wikipedia.org")
        change["title"] = ""
        assert not f.matches(change)

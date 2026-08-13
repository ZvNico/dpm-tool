"""Unit tests for pure parsing functions — no Excel files required."""

from __future__ import annotations

import pytest

from dpm._text import (
    extract_metric_label,
    extract_qname,
    first_match,
    is_qname,
    nearest_text,
    norm,
)
from dpm._constants import COL_RE, ROW_RE, CODE_RE
from dpm.toc import (
    _parent_template_code,
    filter_entries_by_perimeter,
    looks_like_perimeter,
    parse_text_selection,
)
from dpm._types import TocEntry


# ── _text.py ──────────────────────────────────────────────────────────────────


class TestNorm:
    def test_none_returns_empty(self):
        assert norm(None) == ""

    def test_collapses_whitespace(self):
        assert norm("  foo   bar  ") == "foo bar"

    def test_newlines_become_spaces(self):
        assert norm("foo\nbar\r\nbaz") == "foo bar baz"

    def test_non_string_converted(self):
        assert norm(42) == "42"
        assert norm(3.14) == "3.14"

    def test_empty_string(self):
        assert norm("") == ""


class TestIsQname:
    def test_prefixed_qname(self):
        assert is_qname("s2md_met:someMetric") is True

    def test_curly_brace_qname(self):
        assert is_qname("{http://xbrl.org/ns}element") is True

    def test_dotted_qname(self):
        assert is_qname("foo.bar.baz") is True

    def test_plain_word_is_not_qname(self):
        assert is_qname("hello") is False

    def test_empty_is_not_qname(self):
        assert is_qname("") is False

    def test_row_code_is_not_qname(self):
        assert is_qname("R0010") is False


class TestExtractQname:
    def test_extracts_prefixed(self):
        assert extract_qname("s2md_met:someMetric") == "s2md_met:someMetric"

    def test_extracts_from_label_text(self):
        result = extract_qname("s2md_met:R0010G0010 (Metric: Written premiums)")
        assert result == "s2md_met:R0010G0010"

    def test_returns_empty_when_no_qname(self):
        assert extract_qname("plain text") == ""

    def test_returns_empty_for_empty_string(self):
        assert extract_qname("") == ""


class TestExtractMetricLabel:
    def test_extracts_label_from_cell(self):
        result = extract_metric_label("s2md_met:R0010 (Metric: Written premiums)")
        assert result == "Written premiums"

    def test_returns_empty_when_no_label(self):
        assert extract_metric_label("s2md_met:R0010") == ""

    def test_returns_empty_for_empty_string(self):
        assert extract_metric_label("") == ""


class TestNearestText:
    def test_returns_first_non_code_value(self):
        assert (
            nearest_text(["", "R0010", "Gross written premiums"])
            == "Gross written premiums"
        )

    def test_skips_row_codes(self):
        assert nearest_text(["R0010", "R0020", "some label"]) == "some label"

    def test_skips_col_codes(self):
        assert nearest_text(["C0010", "a label"]) == "a label"

    def test_skips_metrics_keyword(self):
        assert nearest_text(["Metrics", "a label"]) == "a label"

    def test_skips_qnames(self):
        assert nearest_text(["s2md_met:R0010G0010", "a label"]) == "a label"

    def test_returns_empty_when_all_codes(self):
        assert nearest_text(["R0010", "C0010", ""]) == ""

    def test_truncates_at_500_chars(self):
        long_text = "x" * 600
        result = nearest_text([long_text])
        assert len(result) == 500


class TestFirstMatch:
    def test_matches_row_code(self):
        assert first_match(ROW_RE, "R0010") == "R0010"

    def test_matches_col_code(self):
        assert first_match(COL_RE, "C0010") == "C0010"

    def test_returns_empty_when_no_match(self):
        assert first_match(ROW_RE, "no match here") == ""

    def test_returns_uppercase(self):
        assert first_match(ROW_RE, "r0010") == "R0010"

    def test_handles_empty_string(self):
        assert first_match(ROW_RE, "") == ""


# ── toc.py ────────────────────────────────────────────────────────────────────


class TestLooksLikePerimeter:
    def test_valid_short_lowercase(self):
        assert looks_like_perimeter("qrs") is True
        assert looks_like_perimeter("solo") is True

    def test_too_long_rejected(self):
        assert looks_like_perimeter("averylongname") is False

    def test_forbidden_words_rejected(self):
        for word in ("template", "perimeter", "code", "toc", "description"):
            assert looks_like_perimeter(word) is False, (
                f"Expected {word!r} to be rejected"
            )

    def test_contains_code_pattern_rejected(self):
        assert looks_like_perimeter("S.01.01.01") is False

    def test_empty_rejected(self):
        assert looks_like_perimeter("") is False

    def test_uppercase_accepted_via_normalisation(self):
        # norm().lower() is applied internally, so "QRS" → "qrs" → valid
        assert looks_like_perimeter("QRS") is True

    def test_starts_with_digit_rejected(self):
        assert looks_like_perimeter("1abc") is False


class TestParentTemplateCode:
    def test_short_subtemplate_returns_itself(self):
        assert _parent_template_code("S.01.01") == "S.01.01"

    def test_four_part_strips_last(self):
        assert _parent_template_code("S.01.01.01") == "S.01.01"

    def test_uses_current_template_when_prefix_matches(self):
        assert _parent_template_code("S.01.01.01", "S.01.01") == "S.01.01"

    def test_five_part_strips_last(self):
        assert _parent_template_code("S.01.01.01.01") == "S.01.01.01"


class TestParseTextSelection:
    _perimeters = ["QRS", "Solo", "Group"]

    def test_all_returns_everything(self):
        result = parse_text_selection("all", self._perimeters)
        assert result == {"qrs", "solo", "group"}

    def test_empty_returns_everything(self):
        result = parse_text_selection("", self._perimeters)
        assert result == {"qrs", "solo", "group"}

    def test_single_index(self):
        assert parse_text_selection("1", self._perimeters) == {"qrs"}

    def test_range(self):
        assert parse_text_selection("1-2", self._perimeters) == {"qrs", "solo"}

    def test_name_selection(self):
        assert parse_text_selection("solo", self._perimeters) == {"solo"}

    def test_comma_separated(self):
        result = parse_text_selection("qrs,group", self._perimeters)
        assert result == {"qrs", "group"}

    def test_unknown_token_raises(self):
        with pytest.raises(ValueError, match="Unknown perimeter selection token"):
            parse_text_selection("unknown", self._perimeters)

    def test_empty_result_raises(self):
        with pytest.raises(ValueError, match="At least one perimeter"):
            parse_text_selection("99", self._perimeters)


class TestFilterEntriesByPerimeter:
    _entries = [
        TocEntry("QRS", "S.01.01", "S.01.01.01"),
        TocEntry("Solo", "S.02.01", "S.02.01.01"),
        TocEntry("Group", "S.03.01", "S.03.01.01"),
    ]

    def test_filters_by_lowercase_perimeter(self):
        result = filter_entries_by_perimeter(self._entries, {"qrs"})
        assert len(result) == 1
        assert result[0].perimeter == "QRS"

    def test_multiple_perimeters(self):
        result = filter_entries_by_perimeter(self._entries, {"qrs", "solo"})
        assert len(result) == 2

    def test_empty_selection_returns_empty(self):
        assert filter_entries_by_perimeter(self._entries, set()) == []

    def test_no_match_returns_empty(self):
        assert filter_entries_by_perimeter(self._entries, {"nonexistent"}) == []

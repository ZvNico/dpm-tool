"""Unit tests for dimension / member extraction — no Excel files required."""

from __future__ import annotations

from dpm._text import (
    extract_dimension_token,
    extract_member_token,
    extract_paren_label,
    is_dimension_decl,
    is_member,
)
from dpm.parser import _parse_subtemplate_window


# ── _text.py dimension helpers ──────────────────────────────────────────────


class TestDimensionToken:
    def test_extracts_declaration(self):
        assert (
            extract_dimension_token("s2c_dim:BL (Line of business [general])")
            == "s2c_dim:BL"
        )

    def test_member_is_not_declaration(self):
        assert extract_dimension_token("s2c_LB:x91 (Neither unit-linked)") == ""

    def test_is_dimension_decl(self):
        assert is_dimension_decl("s2c_dim:TB (Insurance/reinsurance)") is True
        assert is_dimension_decl("s2c_LB:x91 (Direct)") is False


class TestMemberToken:
    def test_extracts_member(self):
        assert extract_member_token("s2c_LB:x91 (Neither unit-linked)") == "s2c_LB:x91"

    def test_declaration_is_not_member(self):
        # 'dim' domain is excluded, and 'BL' is not an x-code anyway.
        assert extract_member_token("s2c_dim:BL (Line of business)") == ""

    def test_is_member(self):
        assert is_member("s2c_RT:x1 (Accepted during the period)") is True
        assert is_member("s2c_dim:BL (Line of business)") is False

    def test_metric_qname_is_not_member(self):
        assert extract_member_token("s2md_met:mi503 (Metric: Money)") == ""


class TestParenLabel:
    def test_extracts_trailing_parenthetical(self):
        assert (
            extract_paren_label("s2c_dim:BL (Line of business [general])")
            == "Line of business [general]"
        )

    def test_empty_when_no_parens(self):
        assert extract_paren_label("s2c_dim:BL") == ""


# ── parser: positional dimension resolution ─────────────────────────────────


class TestMetricsColDimensions:
    """metric qnames run down a column; dim declared in the anchor row to the
    right of 'Metrics', members read per data row (row-scoped)."""

    window = [
        ["C0010hdr", "Metrics", "s2c_dim:BL (Line of business)"],
        ["C0010", "", ""],
        ["R0070", "s2md_met:mi263 (Metric: Money)", "s2c_LB:x91 (Neither)"],
    ]

    def _parse(self):
        return _parse_subtemplate_window(
            self.window, "S.02.01.01", "S.02.01.01.01", "label", frozenset()
        )

    def test_layout_detected(self):
        sub_type = self._parse()[0]
        assert sub_type == "metrics_col"

    def test_dimension_and_member_captured(self):
        _, _, _, dims, members, fact_dims = self._parse()
        assert {d["dimension_code"] for d in dims} == {"s2c_dim:BL"}
        assert {m["member_code"] for m in members} == {"s2c_LB:x91"}
        assert members[0]["dimension_code"] == "s2c_dim:BL"

    def test_member_attached_to_fact(self):
        *_, fact_dims = self._parse()
        assert fact_dims == [
            {
                "subtemplate_code": "S.02.01.01.01",
                "row_code": "R0070",
                "column_code": "C0010",
                "dimension_code": "s2c_dim:BL",
                "member_code": "s2c_LB:x91",
            }
        ]


class TestMetricsRowBothScopes:
    """metric qnames run across the anchor row; a row-scoped dimension (declared
    as a column header, member per data row) and a column-scoped dimension
    (declared below the anchor, member per data column) both apply."""

    window = [
        ["", "L1label", "s2c_dim:DI (Duration)", ""],
        ["", "C0010", "", ""],
        ["ER0010", "", "s2c_DI:x5 (Year to Date)", ""],
        ["Metrics", "s2md_met:mi1 (Metric: X)", "", ""],
        ["s2c_dim:BL (Line of business)", "s2c_LB:x91 (Neither)", "", ""],
    ]

    def _parse(self):
        return _parse_subtemplate_window(
            self.window, "E.04.01.16", "E.04.01.16.01", "label", frozenset()
        )

    def test_layout_detected(self):
        assert self._parse()[0] == "metrics_row"

    def test_both_dimensions_captured(self):
        _, _, _, dims, _, _ = self._parse()
        assert {d["dimension_code"] for d in dims} == {"s2c_dim:DI", "s2c_dim:BL"}

    def test_fact_gets_row_and_column_scoped_members(self):
        *_, fact_dims = self._parse()
        pairs = {(fd["dimension_code"], fd["member_code"]) for fd in fact_dims}
        assert pairs == {
            ("s2c_dim:DI", "s2c_DI:x5"),  # row-scoped
            ("s2c_dim:BL", "s2c_LB:x91"),  # column-scoped
        }
        assert all(fd["row_code"] == "ER0010" for fd in fact_dims)
        assert all(fd["column_code"] == "C0010" for fd in fact_dims)


class TestNoDimensions:
    """A window without any s2c_dim declaration yields no dimension rows."""

    window = [
        ["C0010hdr", "Metrics"],
        ["C0010", ""],
        ["R0010", "s2md_met:mi1 (Metric: X)"],
    ]

    def test_empty_dimension_output(self):
        _, _, facts, dims, members, fact_dims = _parse_subtemplate_window(
            self.window, "T", "T.01", "label", frozenset()
        )
        assert facts  # facts still extracted
        assert dims == []
        assert members == []
        assert fact_dims == []

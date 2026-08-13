"""Unit tests for the read-only explorer query helpers in dpm.db."""

from __future__ import annotations

from pathlib import Path

import pytest

from dpm.db import (
    insert_dimension_members,
    insert_dimensions,
    insert_fact_dimensions,
    insert_facts,
    insert_metrics,
    insert_perimeter_template,
    insert_perimeters,
    insert_subtemplates,
    insert_templates,
    load_db_stats,
    load_db_tree,
    load_dimensions_with_members,
    load_fact_context,
    load_metric_usage,
    load_metrics,
    load_subtemplate_facts,
    open_db,
)


@pytest.fixture
def conn(tmp_path: Path):
    c = open_db(tmp_path / "test.duckdb")
    insert_templates(c, [{"template_code": "T1", "template_label": "Template One"}])
    insert_subtemplates(
        c,
        [
            {
                "subtemplate_code": "S1",
                "template_code": "T1",
                "subtemplate_label": "Sub One",
                "subtemplate_type": "metrics_row",
            }
        ],
    )
    insert_perimeters(c, [{"perimeter_code": "solo"}])
    insert_perimeter_template(c, [{"perimeter_code": "solo", "template_code": "T1"}])
    insert_metrics(c, [{"metric_code": "mi1", "metric_label": "Metric One"}])
    insert_facts(
        c,
        [
            {
                "subtemplate_code": "S1",
                "row_code": "R010",
                "column_code": "C010",
                "row_label": "Row",
                "column_label": "Col",
                "metric_code": "mi1",
            }
        ],
    )
    insert_dimensions(c, [{"dimension_code": "s2c_dim:BL", "dimension_label": "Line of business"}])
    insert_dimension_members(
        c,
        [
            {
                "member_code": "s2c_LB:x91",
                "dimension_code": "s2c_dim:BL",
                "member_label": "Neither unit-linked",
            }
        ],
    )
    insert_fact_dimensions(
        c,
        [
            {
                "subtemplate_code": "S1",
                "row_code": "R010",
                "column_code": "C010",
                "dimension_code": "s2c_dim:BL",
                "member_code": "s2c_LB:x91",
            }
        ],
    )
    yield c
    c.close()


def test_load_db_stats(conn):
    stats = load_db_stats(conn)
    assert stats["templates"] == 1
    assert stats["subtemplates"] == 1
    assert stats["perimeters"] == 1
    assert stats["metrics"] == 1
    assert stats["facts"] == 1
    assert stats["dimensions"] == 1
    assert stats["dimension_members"] == 1


def test_load_db_tree(conn):
    tree = load_db_tree(conn)
    assert tree == [
        ("solo", [("T1", "Template One", [("S1", "Sub One", "metrics_row")])])
    ]


def test_load_subtemplate_facts(conn):
    facts = load_subtemplate_facts(conn, "S1")
    assert facts == [("R010", "C010", "Row", "Col", "mi1", "Metric One")]
    assert load_subtemplate_facts(conn, "missing") == []


def test_load_metrics(conn):
    assert load_metrics(conn) == [("mi1", "Metric One")]


def test_load_metric_usage(conn):
    assert load_metric_usage(conn, "mi1") == [("S1", "R010", "C010", "Row", "Col")]
    assert load_metric_usage(conn, "missing") == []


def test_load_dimensions_with_members(conn):
    assert load_dimensions_with_members(conn) == [
        ("s2c_dim:BL", "Line of business", [("s2c_LB:x91", "Neither unit-linked")])
    ]


def test_load_fact_context(conn):
    assert load_fact_context(conn, "S1", "R010", "C010") == [
        ("s2c_dim:BL", "Line of business", "s2c_LB:x91", "Neither unit-linked")
    ]
    assert load_fact_context(conn, "S1", "R999", "C999") == []

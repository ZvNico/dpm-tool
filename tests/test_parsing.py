"""Unit tests for the structure-entry perimeter filter used by the delta pipeline."""

from __future__ import annotations

from dpm.db import filter_entries_by_perimeter
from dpm._types import StructureEntry


class TestFilterEntriesByPerimeter:
    _entries = [
        StructureEntry("QRS", "S.01.01", "S.01.01.01"),
        StructureEntry("Solo", "S.02.01", "S.02.01.01"),
        StructureEntry("Group", "S.03.01", "S.03.01.01"),
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

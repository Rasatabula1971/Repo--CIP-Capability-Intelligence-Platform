"""
Tests for the extractor auto-discovery registry.

Covers:
  1. all_extractors() returns all known extractors
  2. Each returned object conforms to the Extractor protocol
  3. extractor_names() returns sorted list
  4. get_extractor() retrieves by name
  5. get_extractor() returns None for unknown name
  6. No duplicate names
  7. All expected extractors are discovered
"""
from __future__ import annotations

from analysis.extractors import all_extractors, extractor_names, get_extractor


# The set of extractors we know exist in the package
EXPECTED_NAMES = {
    "interfaces",
    "license",
    "manifests",
    "secret_indicators",
    "test_presence",
    "symbols",
    "ts_interfaces",
    "ts_symbols",
}


class TestAllExtractors:

    def test_returns_list(self):
        exts = all_extractors()
        assert isinstance(exts, list)
        assert len(exts) > 0

    def test_all_have_name(self):
        for ext in all_extractors():
            assert hasattr(ext, "name")
            assert isinstance(ext.name, str)
            assert len(ext.name) > 0

    def test_all_have_extract(self):
        for ext in all_extractors():
            assert hasattr(ext, "extract")
            assert callable(ext.extract)

    def test_no_duplicates(self):
        names = [ext.name for ext in all_extractors()]
        assert len(names) == len(set(names))

    def test_sorted_by_name(self):
        names = [ext.name for ext in all_extractors()]
        assert names == sorted(names)

    def test_discovers_all_expected(self):
        names = set(extractor_names())
        assert EXPECTED_NAMES <= names


class TestExtractorNames:

    def test_returns_sorted_strings(self):
        names = extractor_names()
        assert isinstance(names, list)
        assert all(isinstance(n, str) for n in names)
        assert names == sorted(names)

    def test_includes_known_extractors(self):
        names = extractor_names()
        assert "interfaces" in names
        assert "ts_interfaces" in names
        assert "ts_symbols" in names
        assert "manifests" in names


class TestGetExtractor:

    def test_retrieves_by_name(self):
        ext = get_extractor("interfaces")
        assert ext is not None
        assert ext.name == "interfaces"

    def test_retrieves_ts_extractor(self):
        ext = get_extractor("ts_symbols")
        assert ext is not None
        assert ext.name == "ts_symbols"

    def test_returns_none_for_unknown(self):
        ext = get_extractor("nonexistent_extractor")
        assert ext is None

    def test_returns_fresh_instance(self):
        a = get_extractor("manifests")
        b = get_extractor("manifests")
        assert a is not b
        assert a.name == b.name


class TestExtractorProtocol:

    def test_all_extract_empty_input(self):
        for ext in all_extractors():
            items = list(ext.extract([]))
            assert isinstance(items, list)
            assert len(items) == 0

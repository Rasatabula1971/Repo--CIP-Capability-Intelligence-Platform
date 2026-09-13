"""
Tests for the analyze_local module.

Covers:
  1. Scanning a directory with mixed file types
  2. Language detection from extensions
  3. Skipping large files and hidden directories
  4. Running extractors and collecting evidence
  5. Error handling (bad path, extractor failure)
  6. Result aggregation (by_extractor, by_type, by_language)
  7. include_items toggle
  8. max_files cap
  9. summary() output
  10. to_dict() serialization
"""
from __future__ import annotations

import os
import textwrap

import pytest

from analysis.analyze_local import (
    LocalAnalysisResult,
    _collect_files,
    _detect_language,
    _should_skip_dir,
    analyze_local,
)


@pytest.fixture()
def sample_dir(tmp_path):
    """Create a small directory tree with various file types."""
    (tmp_path / "main.py").write_text("def hello(): pass\n")
    (tmp_path / "utils.ts").write_text(
        "export function greet(name: string): string { return name; }\n"
    )
    (tmp_path / "app.tsx").write_text(
        "export function App() { return <div/>; }\n"
    )
    (tmp_path / "config.json").write_text('{"key": "value"}\n')
    (tmp_path / "README.md").write_text("# Hello\n")
    sub = tmp_path / "lib"
    sub.mkdir()
    (sub / "helper.js").write_text(
        "export const add = (a, b) => a + b;\n"
    )
    return tmp_path


@pytest.fixture()
def py_project(tmp_path):
    """A Python project with setup.py and a module."""
    (tmp_path / "setup.py").write_text(
        textwrap.dedent("""\
        from setuptools import setup
        setup(name="mypkg", version="1.0", install_requires=["requests>=2.0"])
        """)
    )
    src = tmp_path / "mypkg"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "core.py").write_text(
        textwrap.dedent("""\
        class MyModel:
            pass

        def process(data):
            return data
        """)
    )
    return tmp_path


class TestDetectLanguage:

    def test_python(self):
        assert _detect_language("foo.py") == "python"

    def test_typescript(self):
        assert _detect_language("bar.ts") == "typescript"

    def test_tsx(self):
        assert _detect_language("comp.tsx") == "typescript"

    def test_javascript(self):
        assert _detect_language("mod.js") == "javascript"

    def test_jsx(self):
        assert _detect_language("comp.jsx") == "javascript"

    def test_mts(self):
        assert _detect_language("mod.mts") == "typescript"

    def test_unknown(self):
        assert _detect_language("foo.xyz") is None

    def test_case_insensitive(self):
        assert _detect_language("FOO.PY") == "python"


class TestShouldSkipDir:

    def test_git(self):
        assert _should_skip_dir(".git")

    def test_node_modules(self):
        assert _should_skip_dir("node_modules")

    def test_pycache(self):
        assert _should_skip_dir("__pycache__")

    def test_venv(self):
        assert _should_skip_dir("venv")

    def test_egg_info(self):
        assert _should_skip_dir("mypkg.egg-info")

    def test_normal_dir(self):
        assert not _should_skip_dir("src")

    def test_lib(self):
        assert not _should_skip_dir("lib")


class TestCollectFiles:

    def test_collects_all_files(self, sample_dir):
        files, skipped = _collect_files(str(sample_dir))
        assert len(files) == 6
        assert skipped == 0

    def test_max_files_cap(self, sample_dir):
        files, skipped = _collect_files(str(sample_dir), max_files=3)
        assert len(files) == 3
        assert skipped >= 1

    def test_skips_hidden_dirs(self, sample_dir):
        git_dir = sample_dir / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        files, _ = _collect_files(str(sample_dir))
        paths = {f.path for f in files}
        assert not any(".git" in p for p in paths)

    def test_skips_node_modules(self, sample_dir):
        nm = sample_dir / "node_modules"
        nm.mkdir()
        (nm / "lodash.js").write_text("module.exports = {};\n")
        files, _ = _collect_files(str(sample_dir))
        paths = {f.path for f in files}
        assert not any("node_modules" in p for p in paths)

    def test_language_detected(self, sample_dir):
        files, _ = _collect_files(str(sample_dir))
        py_files = [f for f in files if f.language == "python"]
        ts_files = [f for f in files if f.language == "typescript"]
        assert len(py_files) >= 1
        assert len(ts_files) >= 1

    def test_forward_slashes(self, sample_dir):
        files, _ = _collect_files(str(sample_dir))
        for f in files:
            assert "\\" not in f.path

    def test_skips_large_files(self, sample_dir):
        big = sample_dir / "huge.py"
        big.write_bytes(b"x" * (3 * 1024 * 1024))
        files, skipped = _collect_files(str(sample_dir))
        paths = {f.path for f in files}
        assert "huge.py" not in paths
        assert skipped >= 1


class TestAnalyzeLocal:

    def test_basic_scan(self, sample_dir):
        result = analyze_local(str(sample_dir))
        assert result.files_scanned == 6
        assert result.files_skipped == 0
        assert isinstance(result.total_evidence, int)

    def test_bad_path(self):
        result = analyze_local("/nonexistent/path/that/does/not/exist")
        assert result.files_scanned == 0
        assert len(result.errors) == 1
        assert "Not a directory" in result.errors[0].get("error", "")

    def test_by_extractor_populated(self, sample_dir):
        result = analyze_local(str(sample_dir))
        if result.total_evidence > 0:
            assert len(result.by_extractor) > 0

    def test_by_type_populated(self, sample_dir):
        result = analyze_local(str(sample_dir))
        if result.total_evidence > 0:
            assert len(result.by_type) > 0

    def test_include_items_false(self, sample_dir):
        result = analyze_local(str(sample_dir), include_items=False)
        assert result.items == []

    def test_include_items_true(self, sample_dir):
        result = analyze_local(str(sample_dir), include_items=True)
        assert len(result.items) == result.total_evidence

    def test_items_structure(self, sample_dir):
        result = analyze_local(str(sample_dir), include_items=True)
        for item in result.items:
            assert "extractor" in item
            assert "evidence_type" in item
            assert "locator_kind" in item
            assert "locator" in item
            assert "value" in item

    def test_max_files(self, sample_dir):
        result = analyze_local(str(sample_dir), max_files=2)
        assert result.files_scanned == 2
        assert result.files_skipped >= 1

    def test_custom_extractors(self, sample_dir):
        class FakeExtractor:
            name = "fake"
            def extract(self, files):
                return []
        result = analyze_local(str(sample_dir), extractors=[FakeExtractor()])
        assert result.total_evidence == 0
        assert result.errors == []

    def test_extractor_error_caught(self, sample_dir):
        class BadExtractor:
            name = "bad"
            def extract(self, files):
                raise RuntimeError("boom")
        result = analyze_local(str(sample_dir), extractors=[BadExtractor()])
        assert len(result.errors) == 1
        assert result.errors[0]["extractor"] == "bad"
        assert "boom" in result.errors[0]["error"]

    def test_python_project(self, py_project):
        result = analyze_local(str(py_project), include_items=True)
        assert result.files_scanned >= 3
        extractors_used = set(result.by_extractor.keys())
        assert len(extractors_used) >= 1

    def test_ts_evidence(self, sample_dir):
        result = analyze_local(str(sample_dir), include_items=True)
        ts_items = [i for i in result.items if i["extractor"].startswith("ts_")]
        assert len(ts_items) >= 1


class TestLocalAnalysisResult:

    def test_to_dict(self):
        r = LocalAnalysisResult(
            root="/tmp/x", files_scanned=10, files_skipped=2,
            total_evidence=5,
        )
        d = r.to_dict()
        assert d["root"] == "/tmp/x"
        assert d["files_scanned"] == 10
        assert d["files_skipped"] == 2
        assert d["total_evidence"] == 5
        assert d["by_extractor"] == {}
        assert d["items"] == []

    def test_summary_basic(self):
        r = LocalAnalysisResult(
            root="/tmp/y", files_scanned=20, files_skipped=0,
            total_evidence=15,
            by_extractor={"symbols": 10, "interfaces": 5},
            by_type={"symbol": 10, "interface": 5},
            by_language={"python": 10, "typescript": 5},
        )
        s = r.summary()
        assert "20 files" in s
        assert "15" in s
        assert "symbols: 10" in s
        assert "python: 10" in s

    def test_summary_with_errors(self):
        r = LocalAnalysisResult(
            root="/tmp/z", files_scanned=5, files_skipped=0,
            total_evidence=0,
            errors=[{"extractor": "bad", "error": "fail"}],
        )
        s = r.summary()
        assert "Errors: 1" in s

    def test_to_dict_roundtrip(self, sample_dir):
        result = analyze_local(str(sample_dir))
        d = result.to_dict()
        assert isinstance(d, dict)
        assert set(d.keys()) == {
            "root", "files_scanned", "files_skipped", "total_evidence",
            "by_extractor", "by_type", "by_language", "items", "errors",
        }

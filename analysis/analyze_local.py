"""
Local analysis — run extractors against files on disk.

No database, no connectors, no revisions. Reads files from a directory,
runs every registered extractor, and returns a structured report of all
evidence found. Useful for:
  - Quick diagnostics: what does CIP see in this repo?
  - Testing new extractors against real codebases
  - CI integration without a running Postgres instance
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from analysis.evidence import EvidenceItem, SourceFile
from analysis.extractors import all_extractors

_LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".mts": "typescript", ".cts": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".sh": "shell", ".bash": "shell",
    ".md": "markdown",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".sql": "sql",
    ".html": "html", ".htm": "html",
    ".css": "css",
}

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".next", ".nuxt", "coverage", ".eggs", "*.egg-info",
}

_MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB


@dataclass
class LocalAnalysisResult:
    root: str
    files_scanned: int
    files_skipped: int
    total_evidence: int
    by_extractor: dict[str, int] = field(default_factory=dict)
    by_type: dict[str, int] = field(default_factory=dict)
    by_language: dict[str, int] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
            "total_evidence": self.total_evidence,
            "by_extractor": self.by_extractor,
            "by_type": self.by_type,
            "by_language": self.by_language,
            "items": self.items,
            "errors": self.errors,
        }

    def summary(self) -> str:
        lines = [
            f"Scanned {self.files_scanned} files in {self.root}",
            f"Total evidence items: {self.total_evidence}",
        ]
        if self.by_extractor:
            lines.append("By extractor:")
            for name, count in sorted(self.by_extractor.items()):
                lines.append(f"  {name}: {count}")
        if self.by_type:
            lines.append("By evidence type:")
            for etype, count in sorted(self.by_type.items()):
                lines.append(f"  {etype}: {count}")
        if self.by_language:
            lines.append("By language:")
            for lang, count in sorted(self.by_language.items()):
                lines.append(f"  {lang}: {count}")
        if self.errors:
            lines.append(f"Errors: {len(self.errors)}")
        return "\n".join(lines)


def _detect_language(path: str) -> str | None:
    ext = Path(path).suffix.lower()
    return _LANG_BY_EXT.get(ext)


def _should_skip_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.endswith(".egg-info")


def _collect_files(root: str, max_files: int = 5000) -> tuple[list[SourceFile], int]:
    files: list[SourceFile] = []
    skipped = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for fname in filenames:
            if len(files) >= max_files:
                skipped += 1
                continue
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                size = os.path.getsize(full)
                if size > _MAX_FILE_SIZE:
                    skipped += 1
                    continue
                with open(full, "rb") as f:
                    content = f.read()
            except (OSError, IOError):
                skipped += 1
                continue
            lang = _detect_language(rel)
            files.append(SourceFile(path=rel, content=content, language=lang))

    return files, skipped


def analyze_local(
    root: str,
    extractors: list | None = None,
    max_files: int = 5000,
    include_items: bool = True,
) -> LocalAnalysisResult:
    """
    Run all extractors against files under `root`.

    Args:
        root: directory path to scan
        extractors: optional list of extractor instances; defaults to all
        max_files: stop collecting after this many files
        include_items: if True, include individual evidence dicts in result

    Returns a LocalAnalysisResult with counts and optionally all items.
    """
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return LocalAnalysisResult(
            root=root, files_scanned=0, files_skipped=0,
            total_evidence=0,
            errors=[{"error": f"Not a directory: {root}"}],
        )

    extractors = extractors if extractors is not None else all_extractors()
    files, skipped = _collect_files(root, max_files)

    result = LocalAnalysisResult(
        root=root,
        files_scanned=len(files),
        files_skipped=skipped,
        total_evidence=0,
    )

    by_extractor: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    by_language: dict[str, int] = defaultdict(int)

    for ext in extractors:
        try:
            for item in ext.extract(files):
                by_extractor[ext.name] += 1
                by_type[item.evidence_type] += 1
                lang = (item.extracted_value.get("language")
                        or item.extracted_value.get("ecosystem")
                        or "unknown")
                by_language[lang] += 1
                result.total_evidence += 1
                if include_items:
                    result.items.append({
                        "extractor": ext.name,
                        "evidence_type": item.evidence_type,
                        "locator_kind": item.locator_kind,
                        "locator": item.locator,
                        "value": item.extracted_value,
                    })
        except Exception as exc:
            result.errors.append({
                "extractor": ext.name,
                "error": str(exc),
            })

    result.by_extractor = dict(by_extractor)
    result.by_type = dict(by_type)
    result.by_language = dict(by_language)
    return result

"""
Extractor registry — auto-discovers all Extractor implementations.

Every module in this package that defines a class conforming to the
Extractor protocol (has ``name: str`` and ``extract(files) -> Iterator``)
is registered automatically. The ``all_extractors()`` function returns
one instance of each in deterministic (alphabetical by name) order.

To add a new extractor: create a module in this package, define a class
with ``name`` and ``extract``, and it will be picked up automatically.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Any

from analysis.evidence import EvidenceItem, SourceFile


def _is_extractor(obj: Any) -> bool:
    return (
        inspect.isclass(obj)
        and hasattr(obj, "name")
        and isinstance(getattr(obj, "name", None), str)
        and hasattr(obj, "extract")
        and callable(getattr(obj, "extract", None))
    )


def _discover() -> dict[str, type]:
    registry: dict[str, type] = {}
    pkg_path = __path__
    for info in pkgutil.iter_modules(pkg_path):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if _is_extractor(obj) and obj.name not in registry:
                registry[obj.name] = obj
    return registry


_REGISTRY: dict[str, type] | None = None


def _get_registry() -> dict[str, type]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _discover()
    return _REGISTRY


def all_extractors() -> list:
    reg = _get_registry()
    return [cls() for cls in sorted(reg.values(), key=lambda c: c.name)]


def get_extractor(name: str):
    reg = _get_registry()
    cls = reg.get(name)
    return cls() if cls else None


def extractor_names() -> list[str]:
    return sorted(_get_registry().keys())

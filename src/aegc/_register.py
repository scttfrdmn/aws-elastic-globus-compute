"""Make ``globus_compute_endpoint.engines.AWSEngine`` resolvable, lazily (spike #3).

Globus Compute 4.x resolves ``engine: type:`` only as ``getattr(globus_compute_endpoint.engines,
name)``. This module is imported from ``aegc_engines.pth`` at interpreter start-up, so it imports
nothing beyond ``sys`` and only adds a meta-path finder (about 0.3 ms). When, and only when,
``globus_compute_endpoint.engines`` is imported, the finder wraps its loader so that afterwards a
PEP 562 ``__getattr__`` on the module imports ``aegc.engine`` the first time ``AWSEngine`` is asked
for.
"""

from __future__ import annotations

import sys

TYPE_CHECKING = False  # importing typing would cost ~3 ms on every interpreter start
if TYPE_CHECKING:
    from typing import Any

_TARGET = "globus_compute_endpoint.engines"
_LAZY = {"AWSEngine": "aegc.engine"}


def _install(mod: Any) -> None:
    prev_getattr = mod.__dict__.get("__getattr__")
    prev_dir = mod.__dict__.get("__dir__")

    def __getattr__(name: str) -> Any:
        modname = _LAZY.get(name)
        if modname is not None:
            import importlib

            obj = getattr(importlib.import_module(modname), name)
            setattr(mod, name, obj)
            return obj
        if prev_getattr is not None:
            return prev_getattr(name)
        raise AttributeError(f"module {mod.__name__!r} has no attribute {name!r}")

    def __dir__() -> list[str]:
        base = prev_dir() if prev_dir is not None else list(mod.__dict__)
        return sorted(set(base) | set(_LAZY))

    mod.__getattr__ = __getattr__
    mod.__dir__ = __dir__


class _Loader:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def create_module(self, spec: Any) -> Any:
        return self._inner.create_module(spec)

    def exec_module(self, module: Any) -> None:
        self._inner.exec_module(module)
        _install(module)

    def __getattr__(self, name: str) -> Any:  # get_source, get_code, is_package, ...
        return getattr(self._inner, name)


class _Finder:
    @classmethod
    def find_spec(cls, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname != _TARGET:
            return None
        for finder in sys.meta_path:
            if finder is cls or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                break
        else:
            return None
        if spec.loader is not None and hasattr(spec.loader, "exec_module"):
            spec.loader = _Loader(spec.loader)  # type: ignore[assignment]
        sys.meta_path.remove(cls)
        return spec


if _TARGET in sys.modules:
    _install(sys.modules[_TARGET])
elif _Finder not in sys.meta_path:
    sys.meta_path.insert(0, _Finder)

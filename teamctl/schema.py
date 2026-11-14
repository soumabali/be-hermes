"""Validasi argumen tool terhadap inputSchema-nya + blok bangunan skema yang dipakai bersama."""
from __future__ import annotations

from typing import Any

from .confirm import _clean
from .errors import ToolError

_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}


def _check_integer(key: str, value: Any, spec: dict) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"argumen `{key}` harus bilangan bulat, bukan {type(value).__name__}")
    low, high = spec.get("minimum"), spec.get("maximum")
    if (low is not None and value < low) or (high is not None and value > high):
        raise ToolError(f"argumen `{key}` harus di antara {low if low is not None else '-∞'} dan "
                        f"{high if high is not None else '∞'}")


def _check_string(key: str, value: Any, spec: dict) -> None:
    if not isinstance(value, str):
        raise ToolError(f"argumen `{key}` harus string (teks), bukan {type(value).__name__}")


def _check_boolean(key: str, value: Any, spec: dict) -> None:
    if not isinstance(value, bool):
        raise ToolError(f"argumen `{key}` harus boolean (true/false), bukan {type(value).__name__}")


def _check_array(key: str, value: Any, spec: dict) -> None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolError(f"argumen `{key}` harus daftar string, mis. [\"a\", \"b\"]")


_TYPE_CHECKS = {"string": _check_string, "boolean": _check_boolean, "integer": _check_integer, "array": _check_array}


def _check_value(key: str, value: Any, spec: dict) -> None:
    """Satu argumen terhadap spesifikasinya: tipe (+ rentang) lalu enum."""
    check = _TYPE_CHECKS.get(spec.get("type"))
    if check:
        check(key, value, spec)
    if "enum" in spec and value not in spec["enum"]:
        raise ToolError(f"argumen `{key}` harus salah satu: {', '.join(map(str, spec['enum']))}")


def validate_args(schema: dict, args: Any) -> dict:
    """Cek argumen tool terhadap inputSchema-nya (tipe, wajib, enum, rentang). Kembalikan salinan bersih."""
    if not isinstance(args, dict):
        raise ToolError("arguments harus object")
    props = schema.get("properties") or {}
    clean: dict = {}
    for key, value in args.items():
        if not isinstance(key, str) or key not in props:
            raise ToolError(f"argumen `{_clean(key, 40)}` tidak dikenal (pilihan: {', '.join(props) or 'tanpa argumen'})")
        if value is None:
            continue
        _check_value(key, value, props[key])
        clean[key] = value
    for key in schema.get("required") or []:
        if key not in clean:
            raise ToolError(f"argumen `{key}` wajib diisi")
    return clean

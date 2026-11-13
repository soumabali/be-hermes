"""Validasi argumen tool terhadap inputSchema-nya + blok bangunan skema yang dipakai bersama."""
from __future__ import annotations

from typing import Any

from .confirm import _clean
from .errors import ToolError

_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}


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
        spec = props[key]
        kind = spec.get("type")
        if kind == "string":
            if not isinstance(value, str):
                raise ToolError(f"argumen `{key}` harus string (teks), bukan {type(value).__name__}")
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ToolError(f"argumen `{key}` harus boolean (true/false), bukan {type(value).__name__}")
        elif kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError(f"argumen `{key}` harus bilangan bulat, bukan {type(value).__name__}")
            low, high = spec.get("minimum"), spec.get("maximum")
            if (low is not None and value < low) or (high is not None and value > high):
                raise ToolError(f"argumen `{key}` harus di antara {low if low is not None else '-∞'} dan "
                                f"{high if high is not None else '∞'}")
        elif kind == "array":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ToolError(f"argumen `{key}` harus daftar string, mis. [\"a\", \"b\"]")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolError(f"argumen `{key}` harus salah satu: {', '.join(map(str, spec['enum']))}")
        clean[key] = value
    for key in schema.get("required") or []:
        if key not in clean:
            raise ToolError(f"argumen `{key}` wajib diisi")
    return clean

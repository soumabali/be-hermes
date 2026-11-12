"""Penyamaran secret: nilai persis + bentuk turunan → «secret:NAMA»; token vendor di chat.

Murni Python. Varian harus sama dengan `secret_variants()` di hermes_team.py (pemindai skill).
"""
from __future__ import annotations

import base64
import re
import urllib.parse
from typing import Any, Iterable, Mapping

MIN_VARIANT = 6


def mark(name: str) -> str:
    return f"«secret:{name}»"


def secret_variants(value: str) -> set[str]:
    """Asli, base64 (std/urlsafe, dengan/tanpa padding), url-encode, hex, dibalik, huruf besar/kecil."""
    raw = value.encode("utf-8")
    b64 = base64.b64encode(raw).decode()
    url64 = base64.urlsafe_b64encode(raw).decode()
    found = {value, b64, b64.rstrip("="), url64, url64.rstrip("="),
             urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value), raw.hex(), raw.hex().upper(),
             value[::-1], value.upper(), value.lower()}
    return {v for v in found if len(v) >= MIN_VARIANT}


class Redactor:
    """Mengganti semua kemunculan nilai/varian secret. Varian terpanjang menang."""

    def __init__(self, secrets: Mapping[str, str], min_length: int = MIN_VARIANT):
        table: dict[str, str] = {}
        floor = max(int(min_length or 0), 1)
        for name in sorted(secrets):
            value = secrets[name]
            if not isinstance(value, str) or len(value) < floor:
                continue
            for variant in secret_variants(value):
                table.setdefault(variant, name)
        self.names = sorted({n for n in table.values()})
        self._table = table
        self._regex = (re.compile("|".join(re.escape(v) for v in sorted(table, key=len, reverse=True)))
                       if table else None)

    def __bool__(self) -> bool:
        return self._regex is not None

    def redact(self, text: str) -> str:
        if self._regex is None or not isinstance(text, str) or not text:
            return text
        return self._regex.sub(lambda m: mark(self._table[m.group(0)]), text)

    def contains(self, text: str) -> bool:
        return bool(self._regex is not None and isinstance(text, str) and self._regex.search(text))

    def redact_obj(self, obj: Any, _depth: int = 0) -> tuple[Any, bool]:
        """Salinan `obj` (dict/list/tuple/str bersarang) yang sudah disamarkan + apakah ada perubahan."""
        if self._regex is None or _depth > 40:
            return obj, False
        if isinstance(obj, str):
            new = self.redact(obj)
            return new, new != obj
        if isinstance(obj, dict):
            changed, out = False, {}
            for key, value in obj.items():
                new, did = self.redact_obj(value, _depth + 1)
                out[key] = new
                changed = changed or did
            return (out, True) if changed else (obj, False)
        if isinstance(obj, (list, tuple)):
            items = [self.redact_obj(v, _depth + 1) for v in obj]
            if not any(did for _v, did in items):
                return obj, False
            values = [v for v, _did in items]
            return (values if isinstance(obj, list) else tuple(values)), True
        return obj, False


# Token vendor yang "jelas" (dipakai untuk chat dan pesan user di request model). Label = jenisnya.
TOKEN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
                               re.S)),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}\b")),
    ("api-key", re.compile(r"\bsk-(?:proj-|ant-[a-z0-9]+-)?[A-Za-z0-9_-]{20,}\b")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
]

# Pola statis yang juga didaftarkan ke redaktor bawaan Hermes (ctx.register_redaction_patterns):
# wajib diawali >= 2 karakter literal dan tanpa alternasi di tingkat atas.
UPSTREAM_PATTERNS: list[str] = [
    r"glpat-[A-Za-z0-9_-]{20,}",
    r"github_pat_[A-Za-z0-9_]{22,}",
    r"AIza[0-9A-Za-z_-]{35}",
]


def scrub_tokens(text: str) -> tuple[str, list[str]]:
    """Samarkan token vendor yang jelas. Kembalikan (teks, daftar jenis yang ditemukan)."""
    if not isinstance(text, str) or not text:
        return text, []
    found: list[str] = []
    for label, pattern in TOKEN_PATTERNS:
        def _sub(_m: re.Match, _label: str = label) -> str:
            found.append(_label)
            return f"«disamarkan:{_label}»"
        text = pattern.sub(_sub, text)
    return text, found


def scrub_user_messages(messages: Iterable[Any]) -> tuple[list, bool]:
    """Token vendor di pesan ber-role user (format chat OpenAI/Anthropic: content str atau list blok)."""
    out, changed = [], False
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                new, hits = scrub_tokens(content)
                if hits:
                    msg, changed = {**msg, "content": new}, True
            elif isinstance(content, list):
                blocks, did = [], False
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        new, hits = scrub_tokens(block["text"])
                        if hits:
                            block, did = {**block, "text": new}, True
                    blocks.append(block)
                if did:
                    msg, changed = {**msg, "content": blocks}, True
        out.append(msg)
    return out, changed

"""Dasar kit tim: versi, TeamError, validasi Discord ID, dan tokenizer .env (sama dengan Hermes).
Tanpa dependensi lain supaya bisa diimpor oleh kit_discord.py dan hermes_team.py tanpa siklus."""
from __future__ import annotations

import re
from typing import Any, Optional

KIT_VERSION = "3.1.0"
ID_RE = re.compile(r"^[0-9]{15,21}\Z")      # Discord snowflake (hanya digit ASCII)


class TeamError(Exception):
    pass


def _id(value: Any, where: str) -> str:
    """Discord ID → str. YAML tanpa kutip memberi int; Python menyimpannya utuh."""
    if isinstance(value, bool) or value is None:
        raise TeamError(f"{where}: ID kosong atau tidak valid")
    text = str(value).strip()
    if not ID_RE.match(text):
        raise TeamError(f"{where}: '{text}' bukan Discord ID (15–21 digit)")
    return text


def _unquote_double(quoted: str) -> str:
    parts, i = [], 0
    while i < len(quoted):
        escaped = quoted[i] == "\\" and quoted[i + 1:i + 2] in ('"', "\\")
        parts.append(quoted[i + 1] if escaped else quoted[i])
        i += 2 if escaped else 1
    return "".join(parts)


def _closing_quote(text: str, quote: str, start: int) -> int:
    """Indeks tanda kutip penutup di `text` mulai `start` (-1 bila tidak ada). Di dalam "…",
    \\" dan \\\\ adalah escape."""
    i = start
    while i < len(text):
        if quote == '"' and text[i] == "\\" and text[i + 1:i + 2] in ('"', "\\"):
            i += 2
            continue
        if text[i] == quote:
            return i
        i += 1
    return -1


def _env_entries(text: str) -> list[tuple[int, int, str, str]]:
    """(baris_awal, baris_akhir_eksklusif, nama, nilai) tiap entri .env. Nilai ber-kutip boleh
    multi-baris (private key); kutip yang tidak pernah ditutup diperlakukan sebagai nilai polos satu baris."""
    lines = text.splitlines()
    out: list[tuple[int, int, str, str]] = []
    i = 0
    while i < len(lines):
        start = i
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not (sep and key):
            continue
        value = value.strip()
        if value[:1] in ('"', "'"):
            quote = value[0]
            buffer, j, end = value, i, _closing_quote(value, quote, 1)
            while end < 0 and j < len(lines):
                buffer += "\n" + lines[j]
                j += 1
                end = _closing_quote(buffer, quote, 1)
            if end >= 0:
                inner = buffer[1:end]
                out.append((start, j, key, _unquote_double(inner) if quote == '"' else inner))
                i = j
                continue
        out.append((start, i, key, re.split(r"\s+#", value, maxsplit=1)[0].strip()))
    return out


def parse_env_text(text: str) -> dict[str, str]:
    """Tokenizer .env yang sama dengan Hermes (bare, 'single', "double" dengan \\" dan \\\\),
    termasuk nilai ber-kutip yang multi-baris."""
    return {key: value for _start, _end, key, value in _env_entries(text)}


def rewrite_env_text(text: str, key: str, value: Optional[str]) -> str:
    """Ganti (value) atau hapus (None) satu key; entri lama yang multi-baris ikut terbuang utuh."""
    lines = text.splitlines()
    spans = [(start, end) for start, end, name, _v in _env_entries(text) if name == key]
    drop = {n for start, end in spans for n in range(start, end)}
    out, written = [], False
    new_line = None
    if value is not None:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        new_line = f'{key}="{escaped}"'
    for n, line in enumerate(lines):
        if n in drop:
            if new_line is not None and not written:
                out.append(new_line)
                written = True
            continue
        out.append(line)
    if new_line is not None and not written:
        out.append(new_line)
    return ("\n".join(out) + "\n") if out else ""

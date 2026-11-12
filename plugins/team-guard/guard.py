"""Guard perintah (best-effort): blokir dump environment sebelum tool dijalankan.

Murni Python. Sengaja konservatif: memblokir bentuk dump yang umum, tidak mengklaim kedap.
Lapisan utama tetap penyamaran output (redact.py); guard ini mencegah kebocoran yang paling mudah.
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Iterable, Optional

MAX_SCAN = 200_000
MAX_DEPTH = 4

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ash", "fish"}
# Pembungkus yang menjalankan perintah lain: dibuang lalu sisanya diperiksa lagi.
_WRAPPERS = {"sudo", "command", "builtin", "exec", "nohup", "time", "nice", "ionice", "stdbuf", "timeout",
             "xargs", "watch", "setsid", "chronic", "unbuffer", "busybox", "doas", "strace", "ltrace"}
_PRINTERS = {"echo", "printf", "print"}
# Tahap pipa yang hanya mengubah bentuk teks (hasilnya tetap sampai ke layar).
_FILTERS = {"cat", "tee", "base64", "base32", "basenc", "xxd", "od", "hexdump", "rev", "tr", "cut", "sed", "awk",
            "head", "tail", "fold", "grep", "egrep", "sort", "uniq", "less", "more", "nl", "tac", "strings",
            "python", "python3", "perl", "ruby", "node", "openssl", "gzip", "bzip2", "xz", "zstd", "jq", "fmt"}
_SAFE_PRINTENV = {"PATH", "HOME", "USER", "SHELL", "PWD", "LANG", "LC_ALL", "TERM", "TZ", "HOSTNAME", "LOGNAME",
                  "EDITOR", "PAGER", "TMPDIR", "OLDPWD", "SHLVL"}

_PROC_ENVIRON = re.compile(r"/proc/[^\s;|&'\"<>]*environ")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_VAR_REF = re.compile(r"\$\{?[!#]?([A-Za-z_][A-Za-z0-9_]*)")
_SECRETISH = re.compile(
    r"(?:^|_)(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|CREDENTIALS?|AUTH|PAT|PRIVATE_KEY|DSN)(?:_|$)")

# Dump environment dari bahasa pemrograman (dipakai untuk `python -c`, `node -e`, execute_code, ...).
_CODE_DUMPS: list[tuple[str, re.Pattern]] = [
    ("print(os.environ)", re.compile(
        r"(?:\bprint|\bpprint|\bdumps?|\bwrite|\brepr|\bstr|\bdisplay|\blogging\.\w+|\blog(?:ger)?\.\w+)\s*\(\s*"
        r"[^()]*?(?:\(\s*)*\b(?:os\.)?environ\b(?!\s*(?:\[|\.get\s*\(|\.setdefault|\.pop))")),
    ("os.environ.items()", re.compile(r"\b(?:os\.)?environ\s*\.\s*(?:items|values|copy)\s*\(")),
    ("for … in os.environ", re.compile(r"\bin\s+(?:os\.)?environ\b(?!\s*(?:\[|\.get))")),
    ("dict(os.environ)", re.compile(r"\b(?:print|pprint)\s*\(\s*(?:dict|list|sorted|vars|json\.dumps)\s*\(\s*(?:os\.)?environ")),
    ("console.log(process.env)", re.compile(
        r"(?:console\.\w+|JSON\.stringify|util\.inspect|process\.stdout\.write|console\.table)\s*\(\s*"
        r"[^()]*?(?:\(\s*)*\bprocess\.env\b(?!\s*(?:\.[A-Za-z_$]|\[))")),
    ("Object.entries(process.env)", re.compile(r"Object\.(?:keys|values|entries)\s*\(\s*process\.env\s*\)")),
    ("ENV (ruby)", re.compile(r"\bENV\.(?:to_h|to_a|each|inspect|map|select|sort)\b|\b(?:puts|p|pp|print)\s+ENV\b(?!\s*\[)")),
    ("%ENV (perl)", re.compile(r"%ENV\b")),
    ("$_ENV (php)", re.compile(r"\$_(?:ENV|SERVER)\b(?!\s*\[)|\bgetenv\s*\(\s*\)|\bphpinfo\s*\(")),
    ("os.Environ()", re.compile(r"\bos\.Environ\s*\(\s*\)")),
    ("System.getenv()", re.compile(r"\bSystem\.getenv\s*\(\s*\)")),
    ("ENVIRON (awk)", re.compile(r"\bin\s+ENVIRON\b")),
]
# Perintah shell yang ditanam di dalam kode: os.system("env"), subprocess.run("printenv", ...), terminal("env").
_CODE_SHELL = re.compile(
    r"\b(?:system|popen|run|call|check_output|check_call|Popen|getoutput|getstatusoutput|terminal|execSync|exec|spawn)"
    r"\s*\(\s*\[?\s*(?:[rbuf]{0,2})(\"\"\"|'''|\"|')(.*?)\1", re.S)


def message(what: str) -> str:
    return (f"Diblokir oleh team-guard: `{what}` akan menampilkan isi environment (termasuk secret). "
            "Pakai secret langsung sebagai $NAMA di dalam perintah, tanpa mencetaknya. "
            "Daftar nama yang tersedia: /env-list.")


# ----------------------------------------------------------------------------- pemecah shell
def split_pipelines(command: str) -> list[list[str]]:
    """Pecah perintah shell menjadi pipeline → tahap. Isi substitusi `$( )`/backtick dan subshell
    menjadi pipeline tersendiri (konteks kutip baru). Kutip tunggal dihormati."""
    pipelines: list[list[str]] = [[]]
    buf: list[str] = []
    stack: list[tuple[str, bool]] = []       # (penutup, status kutip ganda di luar)
    single = double = False
    i, n = 0, len(command)

    def end_stage() -> None:
        text = "".join(buf).strip()
        buf.clear()
        if text:
            pipelines[-1].append(text)

    def end_pipeline() -> None:
        end_stage()
        if pipelines[-1]:
            pipelines.append([])

    def open_sub(closer: str) -> None:
        nonlocal double
        if double:
            buf.append('"')                  # tutup kutip supaya potongan luar tetap bisa di-tokenisasi
        end_pipeline()
        stack.append((closer, double))
        double = False

    def close_sub() -> None:
        nonlocal double
        end_pipeline()
        _closer, double = stack.pop()
        if double:
            buf.append('"')

    while i < n:
        ch = command[i]
        if single:
            buf.append(ch)
            single = ch != "'"
        elif ch == "\\" and i + 1 < n:
            if command[i + 1] != "\n":
                buf.append(command[i:i + 2])
            i += 1
        elif ch == "`":
            if stack and stack[-1][0] == "`" and not double:
                close_sub()
            else:
                open_sub("`")
        elif ch == "$" and command[i + 1:i + 2] == "(":
            open_sub(")")
            i += 1
        elif ch == '"':
            double = not double
            buf.append(ch)
        elif double:
            buf.append(ch)
        elif ch == "'":
            single = True
            buf.append(ch)
        elif ch == "(":
            open_sub(")")
        elif ch == ")":
            if stack and stack[-1][0] == ")":
                close_sub()
            else:
                end_pipeline()
        elif ch in ";\n":
            end_pipeline()
        elif ch == "&":
            if command[i + 1:i + 2] == ">" or (buf and buf[-1] in "<>"):
                buf.append(ch)               # pengalihan (&>, >&), bukan pemisah perintah
            else:
                end_pipeline()
                if command[i + 1:i + 2] == "&":
                    i += 1
        elif ch == "|":
            if command[i + 1:i + 2] == "|":
                end_pipeline()
                i += 1
            else:
                end_stage()
                if command[i + 1:i + 2] == "&":
                    i += 1
        else:
            buf.append(ch)
        i += 1
    end_pipeline()
    return [p for p in pipelines if p]


_REDIRECT = re.compile(r"^(?:\d*|&)(?:>>?|<)(?:&\d*-?)?(.*)$")
_KEYWORDS = {"{", "}", "!", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "time"}


def _words(stage: str) -> list[str]:
    """Kata-kata satu tahap, tanpa pengalihan (`> file`, `2>&1`, `< in`)."""
    try:
        raw = shlex.split(stage, comments=False, posix=True)
    except ValueError:
        raw = stage.replace('"', " ").replace("'", " ").split()
    words, skip = [], False
    for word in raw:
        if skip:
            skip = False
            continue
        redirect = _REDIRECT.match(word)
        if redirect and not word.startswith("<<<"):
            skip = not redirect.group(1) and "&" not in word[1:]
            continue
        words.append(word)
    return words


def _base(word: str) -> str:
    return word.lstrip("\\").rsplit("/", 1)[-1]


def _strip_prefix(words: list[str]) -> list[str]:
    """Buang penugasan `VAR=x` di depan dan pembungkus (sudo, command, timeout 5, ...)."""
    words = list(words)
    for _ in range(12):
        while words and (_ASSIGN.match(words[0]) or words[0] in _KEYWORDS):
            words.pop(0)
        if not words or _base(words[0]) not in _WRAPPERS:
            break
        wrapper = _base(words.pop(0))
        while words and words[0].startswith("-"):
            flag = words.pop(0)
            if wrapper in ("sudo", "doas") and flag in ("-u", "-g", "-h", "-p", "-C") and words:
                words.pop(0)
            elif wrapper in ("xargs", "nice", "ionice", "timeout", "watch") and len(flag) == 2 and words \
                    and flag in ("-n", "-I", "-P", "-L", "-s", "-k", "-c", "-d") and not words[0].startswith("-"):
                words.pop(0)
        if wrapper in ("timeout", "watch") and words and re.fullmatch(r"[\d.]+[smhd]?", words[0]):
            words.pop(0)
    return words


def _has_redirect(stage: str) -> bool:
    return bool(re.search(r"(?<![<>&\d])\d?>>?(?!&)", re.sub(r"'[^']*'|\"[^\"]*\"", "", stage)))


def _stage_dump(words: list[str]) -> Optional[str]:
    """Nama bentuk dump bila tahap ini mencetak seluruh environment; None bila tidak."""
    if not words:
        return None
    cmd, args = _base(words[0]), words[1:]
    if cmd == "env":
        rest = []
        skip = False
        for arg in args:
            if skip:
                skip = False
            elif arg in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                skip = arg not in ("-S", "--split-string")
                if not skip:
                    rest.append(arg)
            elif arg.startswith("-") or _ASSIGN.match(arg):
                continue
            else:
                rest.append(arg)
        return None if rest else "env"
    if cmd == "printenv":
        names = [a for a in args if not a.startswith("-")]
        return None if names and all(n in _SAFE_PRINTENV for n in names) else "printenv"
    if cmd == "set":
        return "set" if not args else None
    if cmd == "export":
        names = [a for a in args if not a.startswith("-")]
        return "export -p" if not names else None
    if cmd in ("declare", "typeset", "local", "readonly"):
        names = [a for a in args if not a.startswith("-") and not a.startswith("+")]
        if names:
            return None
        flags = "".join(a[1:] for a in args if a.startswith("-"))
        return f"{cmd} -{flags or 'p'}" if (not args or set(flags) & set("xp")) and cmd != "local" else None
    if cmd == "ps":
        for arg in args:
            if not arg.startswith("-") and re.fullmatch(r"[A-Za-z]*e[A-Za-z]*", arg):
                return "ps e"
        return None
    return None


def _secret_refs(stage: str, names: set[str]) -> list[str]:
    # Kutip tunggal mematikan ekspansi variabel.
    text = re.sub(r"'[^']*'", "", stage)
    return [m.group(1) for m in _VAR_REF.finditer(text)
            if m.group(1) in names or (_SECRETISH.search(m.group(1)) and m.group(1) not in _SAFE_PRINTENV)]


def check_command(command: Any, secret_names: Iterable[str] = (), _depth: int = 0) -> Optional[str]:
    """Pesan blokir bila `command` (shell) men-dump environment / mencetak secret; None bila aman."""
    if not isinstance(command, str) or not command.strip():
        return None
    command = command[:MAX_SCAN]
    names = set(secret_names)
    proc = _PROC_ENVIRON.search(command)
    if proc:
        return message(proc.group(0))
    here = re.search(r"<<<\s*[\"']?\$\{?([A-Za-z_][A-Za-z0-9_]*)", command)
    if here and (here.group(1) in names or _SECRETISH.search(here.group(1))):
        return message(f"<<< ${here.group(1)}")
    for pipeline in split_pipelines(command):
        for index, stage in enumerate(pipeline):
            words = _strip_prefix(_words(stage))
            if not words:
                continue
            dump = _stage_dump(words)
            if dump:
                return message(dump)
            cmd = _base(words[0])
            if cmd in _PRINTERS:
                refs = _secret_refs(stage, names)
                later = [_base((_strip_prefix(_words(s)) or [""])[0]) for s in pipeline[index + 1:]]
                # Boleh: hasilnya dialihkan ke file atau dipipa ke pemakai sungguhan (docker login, gh auth, ...).
                if refs and not _has_redirect(stage) and all(c in _FILTERS or c in _PRINTERS for c in later):
                    return message(f"{cmd} ${refs[0]}")
            if _depth < MAX_DEPTH:
                inner: Optional[str] = None
                if cmd in _SHELLS:
                    for pos, word in enumerate(words[1:], start=1):
                        if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
                            inner = words[pos + 1] if pos + 1 < len(words) else None
                            break
                elif cmd == "eval":
                    inner = " ".join(words[1:])
                elif cmd in ("su", "runuser"):
                    if "-c" in words:
                        pos = words.index("-c")
                        inner = words[pos + 1] if pos + 1 < len(words) else None
                if inner:
                    found = check_command(inner, names, _depth + 1)
                    if found:
                        return found
    return check_code(command, names, _depth + 1, _shell=False)


def check_code(code: Any, secret_names: Iterable[str] = (), _depth: int = 0, _shell: bool = True) -> Optional[str]:
    """Pesan blokir bila `code` (python/js/ruby/…) men-dump environment; None bila aman."""
    if not isinstance(code, str) or not code.strip():
        return None
    code = code[:MAX_SCAN]
    proc = _PROC_ENVIRON.search(code)
    if proc:
        return message(proc.group(0))
    for label, pattern in _CODE_DUMPS:
        if pattern.search(code):
            return message(label)
    if _shell and _depth < MAX_DEPTH:
        for match in _CODE_SHELL.finditer(code):
            found = check_command(match.group(2), secret_names, _depth + 1)
            if found:
                return found
    return None


_COMMAND_KEYS = ("command", "cmd", "script")
_CODE_KEYS = ("code", "source")


def check_tool_call(tool_name: Any, args: Any, secret_names: Iterable[str] = ()) -> Optional[str]:
    """Periksa satu panggilan tool. Kembalikan pesan blokir atau None."""
    if not isinstance(args, dict):
        return None
    names = set(secret_names)
    for key in _COMMAND_KEYS:
        found = check_command(args.get(key), names)
        if found:
            return found
    for key in _CODE_KEYS:
        found = check_code(args.get(key), names)
        if found:
            return found
    # Tool berkas: membaca /proc/<pid>/environ sama saja dengan dump.
    for key, value in args.items():
        if key in _COMMAND_KEYS or key in _CODE_KEYS:
            continue
        if isinstance(value, str) and len(value) < 4096:
            proc = _PROC_ENVIRON.search(value)
            if proc:
                return message(proc.group(0))
    return None

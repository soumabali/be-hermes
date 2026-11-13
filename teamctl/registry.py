"""Registri tool: satu tool = SATU fungsi + SATU dekorator `@tool(...)`.

Cara menambah tool (tidak perlu menyentuh daftar skema, TeamCtl, atau protokol):

    # di teamctl/tools_team.py (atau berkas grup baru; modulnya di-impor teamctl_mcp.py)
    from .confirm import Plan, PlanFn
    from .registry import tool

    @tool("ping_team", description="Balas pong.",                       # tool BACA: kembalikan str
          schema={"type": "object", "properties": {"echo": {"type": "string"}},
                  "additionalProperties": False})
    def ping_team(ctl, args: dict) -> str:
        return "pong " + args.get("echo", "")

    @tool("set_motto", description="Ganti motto tim. Butuh konfirmasi.",  # tool UBAH: kembalikan plan()
          schema={"type": "object", "properties": {"motto": {"type": "string"}},
                  "required": ["motto"], "additionalProperties": False},
          mutating=True, event="set-motto")                              # event = nama di audit.log
    def set_motto(ctl, args: dict) -> PlanFn:
        def plan() -> Plan:              # dipanggil DUA kali: sebelum & sesudah admin menyetujui
            team = ctl.team()            # pagar: `raise ToolError(...)`; tidak ada yang perlu diubah: return "teks"
            return Plan(team, f"Ganti motto menjadi {args['motto']!r}.", lambda: "Motto diganti.", motto=args["motto"])
        return plan

Aturan yang dijaga kerangka (bukan kebiasaan penulis tool):
- `ctl` = instance TeamCtl (ctl.team(), ctl.hermes(), ctl.api(), ctl.server(team), ...); `args` sudah divalidasi
  terhadap `schema` (schema wajib type=object dan additionalProperties=false).
- Tool `mutating=True` TIDAK menjalankan apa pun sendiri: ia mengembalikan plan() (atau `Planner(plan, cleanup)`),
  dan kerangka menjalankannya lewat `_guarded` (konfirmasi admin → susun ulang → jalankan → audit). Lupa
  konfirmasi tidak mungkin; sebaliknya tool baca yang membuat Plan / memanggil `_guarded`/`confirm` ditolak
  saat registrasi (kemungkinan lupa `mutating=True`), dan mutating yang memanggil `_guarded` sendiri juga.
- `readOnlyHint` diturunkan dari `mutating`; `destructive=True/False` menambah `destructiveHint`.
- `group="discord"` (atau grup lain) menyembunyikan tool bila `group_enabled(team, group)` palsu; atur
  gerbangnya dengan `REGISTRY.declare_group(nama, gate=lambda team: ...)`. Grup tanpa gerbang selalu tampil.
- `order=` (opsional) menetapkan urutan di tools/list; tanpa itu tool baru ditaruh di akhir grupnya.
- Nama tool unik; deskripsi wajib. Pelanggaran → ValueError saat impor (uji gagal, bukan produksi).
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
from typing import Any, Callable, Optional, Union

_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")
_FORBIDDEN_MUTATING = {"_guarded", "confirm"}                         # kerangka yang memanggilnya, bukan tool
_FORBIDDEN_READ = _FORBIDDEN_MUTATING | {"Plan", "Planner"}           # tool baca tidak merencanakan aksi


class ToolSpec:
    """Satu tool terdaftar: skema (yang tampil di tools/list) + handler."""

    def __init__(self, name: str, description: str, schema: dict, handler: Callable, *, mutating: bool,
                 group: str, event: Union[None, str, Callable[[dict], str]], destructive: Optional[bool],
                 order: Optional[int], seq: int):
        self.name, self.description, self.schema, self.handler = name, description, schema, handler
        self.mutating, self.group, self.event, self.destructive = mutating, group, event, destructive
        self.order, self.seq = order, seq
        listing: dict = {"name": name, "description": description, "inputSchema": schema}
        if not mutating:
            listing["annotations"] = {"readOnlyHint": True}
        elif destructive is not None:
            listing["annotations"] = {"destructiveHint": destructive}
        self.listing = listing

    def event_for(self, args: dict) -> str:
        """Nama event audit untuk panggilan ini (str tetap, atau fungsi dari argumen)."""
        return self.event(args) if callable(self.event) else str(self.event)

    def sort_key(self) -> tuple:
        return (self.order is None, self.order or 0, self.seq)


def _called_names(handler: Callable) -> Optional[set]:
    """Nama fungsi/metode yang dipanggil di badan handler (None bila sumbernya tidak tersedia)."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    except (OSError, TypeError, SyntaxError):
        return None
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            names.add(fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else "")
    return names


class Registry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._gates: dict[str, Optional[Callable[[dict], bool]]] = {}   # urutan = urutan grup di tools/list
        self._seq = 0
        self.declare_group("team")

    # ---- grup
    def declare_group(self, name: str, gate: Optional[Callable[[dict], bool]] = None) -> None:
        """Daftarkan grup (urutannya = urutan di tools/list). `gate(team)` palsu → grup disembunyikan."""
        if name in self._gates and self._gates[name] is not None and gate is not None:
            raise ValueError(f"grup {name} sudah punya gerbang")
        self._gates[name] = gate if gate is not None else self._gates.get(name)

    def group_enabled(self, team: Optional[dict], group: str) -> bool:
        """Apakah grup tampil untuk konfigurasi tim ini. Konfigurasi tak terbaca/gerbang gagal → tampil
        (perilaku lama: keadaan tak diketahui tidak menyembunyikan tool; pagar sebenarnya ada di handler)."""
        gate = self._gates.get(group)
        if gate is None or team is None:
            return True
        try:
            return bool(gate(team))
        except Exception:
            return True

    def groups(self) -> list[str]:
        return list(self._gates)

    # ---- registrasi
    def tool(self, name: str, description: str, schema: dict, mutating: bool = False, group: str = "team", *,
             event: Union[None, str, Callable[[dict], str]] = None, destructive: Optional[bool] = None,
             order: Optional[int] = None) -> Callable[[Callable], Callable]:
        def register(handler: Callable) -> Callable:
            self._add(handler, name, description, schema, mutating, group, event, destructive, order)
            return handler
        return register

    def _add(self, handler, name, description, schema, mutating, group, event, destructive, order) -> None:
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ValueError(f"nama tool tidak sah: {name!r}")
        if name in self._specs:
            raise ValueError(f"tool {name} sudah terdaftar")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"tool {name}: deskripsi wajib")
        if not isinstance(schema, dict) or schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ValueError(f"tool {name}: schema harus type=object dengan additionalProperties=False")
        if mutating and not event:
            raise ValueError(f"tool {name}: mutating=True wajib punya event= (nama di audit.log)")
        if not mutating and (event is not None or destructive is not None):
            raise ValueError(f"tool {name}: event/destructive hanya untuk mutating=True")
        if group not in self._gates:
            self.declare_group(group)
        called = _called_names(handler)
        if called is not None:
            bad = called & (_FORBIDDEN_MUTATING if mutating else _FORBIDDEN_READ)
            if bad:
                hint = ("kerangka yang menjalankan konfirmasi; kembalikan plan()" if mutating
                        else "tool yang merencanakan aksi harus mutating=True (kalau tidak, ia melewati konfirmasi)")
                raise ValueError(f"tool {name}: handler memanggil {', '.join(sorted(bad))} — {hint}")
        self._seq += 1
        self._specs[name] = ToolSpec(name, description, schema, handler, mutating=mutating, group=group, event=event,
                                     destructive=destructive, order=order, seq=self._seq)

    # ---- baca
    def get(self, name: Any) -> Optional[ToolSpec]:
        return self._specs.get(name) if isinstance(name, str) else None

    def __contains__(self, name: Any) -> bool:
        return self.get(name) is not None

    def __len__(self) -> int:
        return len(self._specs)

    def specs(self, group: Optional[str] = None) -> list[ToolSpec]:
        """Tool terdaftar menurut urutan grup lalu `order`/urutan registrasi; `group` membatasi ke satu grup."""
        rank = {g: i for i, g in enumerate(self._gates)}
        chosen = [s for s in self._specs.values() if group is None or s.group == group]
        return sorted(chosen, key=lambda s: (rank[s.group], *s.sort_key()))

    def tools(self, group: Optional[str] = None) -> list[dict]:
        """Skema tools/list (dict yang sama tiap panggilan) untuk satu grup atau semuanya."""
        return [s.listing for s in self.specs(group)]

    def visible(self, team: Optional[dict]) -> list[dict]:
        return [s.listing for s in self.specs() if self.group_enabled(team, s.group)]


REGISTRY = Registry()
tool = REGISTRY.tool


def group_enabled(team: Optional[dict], group: str, registry: Registry = REGISTRY) -> bool:
    """Kait gerbang grup (lihat Registry.group_enabled)."""
    return registry.group_enabled(team, group)

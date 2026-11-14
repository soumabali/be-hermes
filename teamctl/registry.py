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

Yang DITEGAKKAN kerangka (jujur: sebagian lint, sebagian runtime; bukan bukti keamanan):
- `ctl` = instance TeamCtl (ctl.team(), ctl.hermes(), ctl.api(), ctl.server(team), ...); `args` sudah divalidasi
  terhadap `schema` (schema wajib type=object dan additionalProperties=false).
- Tool `mutating=True` mengembalikan plan() (atau `Planner(plan, cleanup)`); kerangka (`run_tool`) yang menjalankannya
  lewat `_guarded` (konfirmasi admin → susun ulang → jalankan → audit). Pemanggilan yang benar lewat kerangka
  ini selalu dikonfirmasi.
- LINT saat registrasi (bytecode handler, termasuk fungsi bersarang; bukan jaminan): handler harus fungsi biasa
  (bukan functools.partial/objek callable/bound method/hasil dekorator ber-`__wrapped__`) — selain itu registrasi
  GAGAL, tidak dilewati. Tool baca yang menyebut `_guarded`/`confirm`/`Plan`/`Planner`, `DiscordAPI`, `.request`,
  method API pengubah (create_*/delete_*/edit_*/add_role/send_message/...) atau string POST/PUT/PATCH/DELETE ditolak
  (kemungkinan lupa `mutating=True`); tool ubah yang menyebut `_guarded`/`confirm` ditolak; `getattr(ctl, "_guarded")`
  dan sejenisnya (konstanta string) ikut terdeteksi; eval/exec/compile/__import__ dilarang.
  Lint dangkal: TIDAK mengikuti fungsi bantu di tempat lain dan tidak bisa menangkap nama yang disusun dinamis.
- RUNTIME untuk tool baca: selama handler baca berjalan (thread yang sama), `ctl.api()` / `Server.api` mengembalikan
  klien yang menolak method selain GET sebelum keluar ke jaringan (GuardedAPI). Tidak mencakup klien yang dibuat
  sendiri (`ht.DiscordAPI(...)`), thread/proses baru, penulisan file/state lokal, atau memanggil `_guarded` lewat
  fungsi bantu tak terlihat lint. Untuk tool ubah tidak ada pagar runtime selain konfirmasi `_guarded`.
- `readOnlyHint` diturunkan dari `mutating`; `destructive=True/False` menambah `destructiveHint`.
- `group="discord"` (atau grup lain) menyembunyikan tool bila `group_enabled(team, group)` palsu; atur
  gerbangnya dengan `REGISTRY.declare_group(nama, gate=lambda team: ...)`. Grup tanpa gerbang selalu tampil.
- `order=` (opsional) menetapkan urutan di tools/list; tanpa itu tool baru ditaruh di akhir grupnya.
- Nama tool unik; deskripsi wajib. Pelanggaran → ValueError saat impor (uji gagal, bukan produksi).
"""
from __future__ import annotations

import re
import types
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


_DYNAMIC = {"getattr", "setattr", "delattr", "__getattribute__", "vars", "globals", "attrgetter", "methodcaller"}
_NEVER = {"eval", "exec", "compile", "__import__"}                     # tidak pernah sah di handler tool
_HTTP_WRITE = {"POST", "PUT", "PATCH", "DELETE"}
# Method DiscordAPI (kit_discord) yang mengubah sesuatu; tool baca tidak boleh menyebutnya.
_API_WRITERS = {"request", "DiscordAPI", "GuardedAPI", "urlopen", "create_channel", "delete_channel", "edit_channel",
                "create_role", "edit_role", "delete_role", "add_role", "remove_role", "edit_member", "kick_member",
                "send_message", "set_overwrite", "delete_overwrite", "create_private_channel"}


def _code_objects(code: types.CodeType):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _code_objects(const)


def _lint_names(handler: Callable) -> set:
    """Nama yang disebut badan handler (global, atribut, fungsi bersarang) + konstanta string yang bisa dipakai
    untuk getattr/HTTP. Dari bytecode, jadi tidak butuh kode sumber (lambda, exec) dan tidak bisa dilewati dengan
    `g = getattr(ctl, "_guarded")`. Handler yang bukan fungsi biasa → ValueError (registrasi gagal, bukan dilewati)."""
    if not isinstance(handler, types.FunctionType) or hasattr(handler, "__wrapped__"):
        raise ValueError(f"handler harus fungsi biasa (bukan {type(handler).__name__} / pembungkus): lint "
                         "registri tidak bisa memeriksanya — definisikan fungsinya langsung dengan @tool")
    names: set = set()
    consts: set = set()
    for code in _code_objects(handler.__code__):
        names.update(code.co_names)
        consts.update(c for c in code.co_consts if isinstance(c, str))
    if names & _DYNAMIC:                  # akses atribut dinamis: konstanta string bisa jadi nama yang dilarang
        names |= consts
    names |= consts & _HTTP_WRITE
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
        """Apakah grup tampil. Grup tanpa gerbang selalu tampil. Grup BERGERBANG fail-closed: konfigurasi tak
        terbaca (team None) atau gerbang gagal → disembunyikan (pagar sebenarnya tetap ada di handler)."""
        gate = self._gates.get(group)
        if gate is None:
            return True
        if team is None:
            return False
        try:
            return bool(gate(team))
        except Exception:
            return False

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
        try:
            called = _lint_names(handler)
        except ValueError as exc:
            raise ValueError(f"tool {name}: {exc}") from None
        if called & _NEVER:
            raise ValueError(f"tool {name}: handler memakai {', '.join(sorted(called & _NEVER))} — dilarang")
        bad = called & (_FORBIDDEN_MUTATING if mutating else _FORBIDDEN_READ)
        if bad:
            hint = ("kerangka yang menjalankan konfirmasi; kembalikan plan()" if mutating
                    else "tool yang merencanakan aksi harus mutating=True (kalau tidak, ia melewati konfirmasi)")
            raise ValueError(f"tool {name}: handler memanggil {', '.join(sorted(bad))} — {hint}")
        if not mutating:
            writes = called & (_API_WRITERS | _HTTP_WRITE)
            if writes:
                raise ValueError(f"tool {name}: tool baca menyebut {', '.join(sorted(writes))} — aksi yang mengubah "
                                 "Discord harus lewat tool mutating=True (dengan konfirmasi)")
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

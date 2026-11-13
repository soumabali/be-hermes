# Panduan pengembang

Cara menambah sesuatu ke kit tanpa membaca seluruh kodenya. Aturan umum: **satu tempat per hal**, dan setiap
sambungan antar komponen punya uji yang gagal bila satu sisi berubah sendirian
(lihat [Kontrak antar komponen](KONTRAK.md)).

## Peta kode

| Lokasi | Tanggung jawab |
|---|---|
| `kit_config.py` | Skema `team.yaml`: tabel `FIELDS` (key, tipe, bawaan, batas, apakah ditegakkan plugin). Validasi, peringatan salah ketik, `roster["settings"]`, dan [Referensi konfigurasi](KONFIGURASI.md) diturunkan darinya |
| `kit/` | Logika kit, satu tanggung jawab per modul: `constants`, `templates` (SOUL di `kit/templates/*.md`), `profiles` (`PROFILE_KINDS`), `util`, `config`, `derive` (menurunkan config Hermes dan roster), `hermes_cli` (pembungkus CLI `hermes`), `skills`, `provision` (`apply`), `membership` (anggota), `cli` (argparse) |
| `hermes_team.py` | Titik masuk CLI + fasad yang mempertahankan semua nama lama (`import hermes_team as ht`) |
| `kit_base.py`, `kit_discord.py`, `kit_paths.py` | Dasar tanpa dependensi, klien Discord REST, lokasi berkas |
| `teamctl_mcp.py`, `teamctl/` | Server MCP admin: `protocol`, `confirm`, `registry` (`@tool`), `tools_team`, `tools_discord`, `tools_usage`, `discord_guard` |
| `plugins/team-guard/` | Plugin Hermes. `upstream.py` adalah SATU-SATUNYA modul yang menyentuh internal Hermes; `limiters.py` berisi pembatas; sisanya per fitur |
| `usage_ledger.py`, `usage_report.py`, `tools/` | Buku besar pemakaian dan alat operasional (ringan, hanya stdlib + PyYAML) |
| `kit_setup.py`, `setup.sh` | Wizard instalasi |

## Menambah satu setelan di `team.yaml`

Satu baris di `FIELDS` (`kit_config.py`), ditambah tempat setelan itu dipakai.

```python
_plugin("scale.foo", "int", 5, "Deskripsi singkat.", key="foo", min=1)   # ditegakkan plugin → masuk roster["settings"]["foo"]
_row("scale.bar", "int", 3, "Deskripsi.", min=0)                         # hanya dipakai kit
```

Lalu: `python3 hermes_team.py config-doc > docs/KONFIGURASI.md`, tambahkan baris berkomentar di
`team.example.yaml` (uji `check_example` menolak bila ada field yang tidak terdokumentasi), dan baca nilainya
di plugin lewat `roster.settings.get("foo")`. Uji `tests/test_contract.py` memastikan setiap key yang dibaca plugin
memang dihasilkan kit.

## Menambah tool admin

Di `teamctl/tools_*.py`; satu fungsi, satu dekorator. Tool yang mengubah sesuatu **tidak bisa melewatkan
konfirmasi**: ia mengembalikan `plan()` dan kerangka yang menjalankannya lewat tombol Approve/Deny.

```python
@tool("ping_team", description="Balas pong.",
      schema={"type": "object", "properties": {"echo": {"type": "string"}}, "additionalProperties": False})
def ping_team(ctl, args: dict) -> str:
    return "pong " + args.get("echo", "")

@tool("set_motto", description="...", schema={...}, mutating=True, event="set-motto")
def set_motto(ctl, args) -> PlanFn:
    def plan() -> Plan:
        return Plan(ctl.team(), "pratinjau untuk admin", lambda: "selesai", motto=args["motto"])
    return plan
```

Nama dan skema tool dipin (`PINNED_TOOLS_SHA256` di `tests/test_teamctl.py`): mengubahnya sengaja berarti
memperbarui daftar nama dan hash itu. Platform baru = modul baru + `REGISTRY.declare_group(nama, gate=...)`.

## Menambah pembatas di plugin

Kelas dengan `check(ctx) -> Verdict | None` di `plugins/team-guard/limiters.py`, lalu satu baris di daftar
`rt.limiters` (urutan = urutan pemeriksaan). Penolakan memakai `Dispatcher._refuse` (satu tabel pesan, satu
`metrics.refuse`). Uji dengan simulasi (`tests/test_scale_sim.py`, jam tiruan).

## Menambah jenis profile

Satu entri di `PROFILE_KINDS` (`kit/profiles.py`): fungsi setelan, templat SOUL, apakah butuh channel,
toolset yang dimatikan, nama yang dicadangkan.

## Menyentuh internal Hermes

Hanya lewat `plugins/team-guard/upstream.py`. Tiap pemakaian internal mendaftarkan dirinya sebagai
*capability*; bila API hilang, fitur itu mati dengan satu baris log dan `capabilities.json` mencatatnya
(`kit_doctor.py` menampilkannya). Uji yang menyentuh Hermes asli dijaga `importorskip` dan berjalan di CI.

## Uji

```bash
python -m pytest tests -q                 # semua; ±3,5 menit
python -m pytest tests/test_plugin.py -q  # plugin
bash tests/selftest.sh                    # terhadap Hermes yang terpasang (server)
```

CI menjalankan uji unit (Python 3.10, 3.11, 3.14) dan uji integrasi terhadap Hermes yang dipin dan `main`
dengan `KIT_REQUIRE_HERMES=1` (integrasi tidak boleh dilewati diam-diam). Uji yang menimpa fungsi di kit memakai
`patch_kit(nama, nilai)` dari `tests/conftest.py`, bukan `monkeypatch.setattr(ht, ...)`: kode di `kit/` mencari nama di modulnya sendiri.

## Rilis

1. Ubah `VERSION` dan `version:` di `plugins/team-guard/plugin.yaml` (uji `test_release.py` memeriksa keduanya).
2. Pindahkan isi "Belum dirilis" di `CHANGELOG.md` ke bagian `[X.Y.Z]` baru.
3. CI hijau di cabang → gabungkan → beri tag `vX.Y.Z`. Operator memperbarui lewat bagian *Upgrade* di
   [Instalasi manual](INSTALL.md#upgrade-dari-v30).

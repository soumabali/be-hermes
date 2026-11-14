# Panduan pengembang

Cara menambah sesuatu ke kit tanpa membaca seluruh kodenya. Aturan umum: **satu tempat per hal**, dan setiap
sambungan antar komponen punya uji yang gagal bila satu sisi berubah sendirian
(lihat [Kontrak antar komponen](KONTRAK.md)).

## Peta kode

| Lokasi | Tanggung jawab |
|---|---|
| `kit_config.py` | Skema `team.yaml`: tabel `FIELDS` (key, tipe, bawaan, batas, apakah ditegakkan plugin). Validasi, peringatan salah ketik, `roster["settings"]`, dan [Referensi konfigurasi](KONFIGURASI.md) diturunkan darinya |
| `kit/` | Logika kit, satu tanggung jawab per modul: `constants`, `templates` (SOUL di `kit/templates/*.md`), `profiles` (`PROFILE_KINDS`, tampilan `SYSTEM_KINDS`/`MODEL_TARGETS`), `util`, `config`, `derive` (menurunkan config Hermes dan roster), `hermes_cli` (pembungkus CLI `hermes`), `skills`, `provision` (`apply`), `membership` (anggota), `cli` (argparse) |
| `hermes_team.py` | Titik masuk CLI + fasad yang mempertahankan semua nama lama (`import hermes_team as ht`) |
| `kit_base.py`, `kit_discord.py`, `kit_paths.py` | Dasar tanpa dependensi, klien Discord REST, lokasi berkas |
| `teamctl_mcp.py`, `teamctl/` | Server MCP admin: `protocol`, `confirm`, `registry` (`@tool`), `tools_team`, `tools_discord`, `tools_usage`, `discord_guard` |
| `plugins/team-guard/` | Plugin Hermes. `upstream.py` adalah SATU-SATUNYA modul yang menyentuh internal Hermes; `limiters.py` berisi pembatas; sisanya per fitur |
| `usage_ledger.py`, `usage_report.py`, `tools/` | Buku besar pemakaian dan alat operasional (ringan, hanya stdlib + PyYAML) |
| `kit_setup.py`, `setup.sh` | Wizard instalasi |

## Menambah satu setelan di `team.yaml`

Intinya satu baris di `FIELDS` (`kit_config.py`) plus titik pakainya, tetapi beberapa berkas lain HARUS ikut
disentuh. Daftar jujurnya (uji yang gagal memberi tahu langkah yang kurang):

```python
_plugin("scale.foo", "int", 5, "Deskripsi singkat.", key="foo", min=1)   # ditegakkan plugin → masuk roster["settings"]["foo"]
_row("scale.bar", "int", 3, "Deskripsi.", min=0)                         # hanya dipakai kit
```

| # | Berkas | Bila | Diingatkan oleh |
|---|---|---|---|
| 1 | `kit_config.py` — baris di `FIELDS` | selalu | — |
| 2 | titik pakai: kode kit (`team["scale"]["bar"]`) atau plugin (`roster.settings.get("foo")`) | selalu | uji plugin: `settings.get("x")` yang tidak dihasilkan kit |
| 3 | `plugins/team-guard/roster.py` — `SETTINGS_DEFAULTS` / `ONBOARDING_DEFAULTS` | hanya baris `_plugin` | `tests/test_kit_units.py::TestExtensionGuides` mencetak BARIS PERSIS yang ditambahkan |
| 4 | `team.example.yaml` — baris (boleh berkomentar `# foo: 5`) | selalu | `TestConfigSchema::test_example_file_documents_exactly_the_schema`; bahan: `python3 kit_config.py --emit-example-section scale` |
| 5 | `docs/KONFIGURASI.md` | selalu | `tests/test_release.py` mencetak perintahnya: `python3 hermes_team.py config-doc --write docs/KONFIGURASI.md` |
| 6 | uji yang menghitung setelan dengan tangan (mis. daftar `NEW_SETTINGS` di `tests/test_contract.py`) | bila ada yang gagal | pesan kegagalan uji itu |

Duplikasi bawaan di plugin (butir 3) disengaja: plugin tidak boleh meng-import kit. Yang diturunkan otomatis dari
satu baris: validasi + pesan galat, peringatan salah ketik, isi `roster.json`, tabel `KONFIGURASI.md`.

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

Daftarkan satu `ProfileKind` di `PROFILE_KINDS` (`kit/profiles.py`). Argumen `soul` (teks `SOUL.md`, dengan
placeholder `{team_name}`) WAJIB: ia tidak bisa diturunkan, jadi tambahkan `kit/templates/soul_<jenis>.md` dan
ekspor lewat `kit/templates/__init__.py` (atau pakai ulang teks yang ada). Contoh:

```python
OPS = ProfileKind("ops", SOUL_OPS, _shared_settings, needs_channel=True, reserved=True, model_target=True,
                  note="ops, @mention", channel_label="channel ops", model_label="ops")
# role="admin" / admin_routes=True / free_response=True bila channel-nya khusus admin & tanpa @mention
```

Yang IKUT OTOMATIS dari entri itu (diuji di `TestRegisteredKindWorksEndToEnd` dengan jenis palsu):
blok `ops: {profile, channel_id, auto_skills}` di `team.yaml` dan baris `llm.ops_model` (skema dibangun dari
registri), peringatan salah ketik, rute gateway (satu rute per admin bila `admin_routes`), `allowed_channels` /
`free_response_channels`, `channel_skill_bindings`, `models:` di `members.yaml` + alias `set-model`/`llm-key`,
`channels` + `kind` di `roster.json`, `check`/`describe`/`status`, cek channel `kit_doctor`, urutan provisioning
(`apply` membuat profile + SOUL + setelan), keunikan channel/profile, ID contoh (`placeholder_ids`), nama yang
dicadangkan, plafon RAM sandbox, target `set_model` di teamctl (dari `MODEL_TARGETS`), dan perlindungan channel
tim di guard Discord admin (`Server.team_channels` dari `SYSTEM_KINDS`; label `channel_label` + " tim"). Dua yang
terakhir diuji di `tests/test_teamctl_kinds.py` dengan jenis palsu; deskripsi/skema tool `set_model` tetap teks
tetap (di-pin hash), jadi tidak menyebut jenis baru.

Yang TIDAK otomatis (cek satu per satu; tidak ada uji yang mengingatkan, kecuali disebut):

1. `team.example.yaml` + `docs/KONFIGURASI.md` — tambahkan blok contoh; tabel dokumen dibangkitkan (lihat di atas).
   `test_example_file_documents_exactly_the_schema` hanya memeriksa jenis bawaan.
2. `plugins/team-guard` (sisi plugin sudah generik dan diuji, `TestRegisteredKindInPlugin`): cron guard, rute contoh
   live reload, dan placeholder sambutan `{<jenis>_channel}` membaca roster, bukan nama jenis; `roster_data` menulis
   kunci opsional `role` / `cron_exempt` dari `ProfileKind` (diuji kit → roster, `TestRegisteredKindWorksEndToEnd`).
   `commands.py::resolve_env_target` (kind `personal` = anggota) tidak berubah.
3. `kit/derive.py::host_settings` — `DISCORD_HOME_CHANNEL` dan `no_thread_channels` memakai channel `admin`.
4. Pesan/doc yang menyebut "shared + admin" secara harfiah (README, docs/FITUR.md).
5. Uji yang mengunci registri bawaan (`TestProfileKinds::test_registry_shape`) bila jenis ditambah permanen.

Uji jenis palsu memakai fixture `register_kind` (`tests/conftest.py`): ia mendaftarkan di salinan-uji registri dan
memanggil `kit_config.rebuild_schema()`; `SYSTEM_KINDS`/`MODEL_TARGETS`/`RESERVED_NAMES` adalah tampilan malas, jadi
tidak ada salinan basi.

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

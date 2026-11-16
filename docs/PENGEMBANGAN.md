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

Setelan yang nilai bawaannya berarti "tanpa batas" atau "pemeriksaan mati" sebaiknya punya peringatan di
`check`/`apply`/`kit_doctor` (fungsi `*_warnings(team)` di `kit/derive.py`, digabung di `kit/cli.py::_print_warnings`
dan `kit_doctor.check_config`). Contoh: `budget_warnings` (tiga batas biaya `scale.*` semuanya 0) dan
`unmanaged_warnings` (`sandbox.allow_unmanaged: true`). Bawaan kode tidak diubah demi peringatan.

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

Menambah (atau mengganti nama/skema/deskripsi) tool berarti memperbarui **tepat tiga hal** — tidak ada angka
jumlah tool yang ditulis tangan di uji lain; hitungan dan himpunan tool baca diturunkan dari registri:

1. `PINNED_TOOL_NAMES` di `tests/test_teamctl.py` — tambahkan namanya sesuai urutan `tools/list` (tanpa
   `order=`, tool baru berada di akhir grupnya: tool tim sebelum `discord_overview`).
2. `PINNED_TOOLS_SHA256` di berkas yang sama — hash daftar nama + skema + deskripsi + anotasi.
3. `kit/templates/soul_admin.md` — sebut tool-nya dengan nama lengkap `mcp_teamctl_<nama>` (satu baris di daftar
   "Tool admin"), supaya AI admin tahu tool itu ada. SOUL.md profile admin ikut berubah saat provisioning berikutnya.

Jalankan `pytest tests/test_teamctl.py`: tepat dua uji gagal dan pesannya memuat nilai yang harus ditempel —
`test_tool_names_and_schema_hash_are_pinned` (mencetak daftar nama baru + hash baru) dan
`test_admin_soul_mentions_exactly_the_registered_tools` (menyebut `mcp_teamctl_<nama>` yang belum ada di
`soul_admin.md`). Setelah tiga hal di atas diperbarui, semuanya lulus.

Keluaran tool yang memuat teks buatan anggota (nama/deskripsi skill, nama tampilan, isi berkas di profile
anggota, teks dari Discord) harus dibungkus `_untrusted_block(...)` / `_data_block(...)` (`teamctl/discord_guard.py`)
dan tiap potongannya dibersihkan dengan `_clean` / `_q` / `_name` (`teamctl/confirm.py`); pratinjau konfirmasi
tool ubah harus memperlihatkan apa yang benar-benar akan dipasang/dikirim, bukan hanya namanya.

Platform baru = modul baru + `REGISTRY.declare_group(nama, gate=...)`.

## Menambah pembatas di plugin

Tiga langkah, semuanya di `plugins/team-guard/`:

1. **Kelas pembatas** di `limiters.py`: atribut `name` dan `async check(ctx) -> Verdict | None` (`None` = lolos).
2. **Satu baris di `default_limiters()`** (`limiters.py`); urutan daftar = urutan pemeriksaan. `rt.limiters`
   hanyalah hasil fungsi itu saat runtime dibuat — uji boleh menggantinya, kode produksi tidak menulis ke sana.
3. **Daftarkan jenis penolakannya** bila `kind` di `Verdict(kind, reason, fmt=...)` belum ada: satu entri di
   tabel `REFUSALS` (`refusals.py`) — `Refusal(kunci_jeda, templat_pesan, metrik)`; templat wajib memuat `{uid}`,
   `metrik=None` berarti tidak dihitung. Itu SATU-SATUNYA tabel: pesan ke pengguna dan kunci `refused` di
   `metrics.json` (`scale.refusal_kinds()`) sama-sama diturunkan darinya. Metrik baru = kunci baru di
   `metrics.json → refused`; pembaca di kit (`teamctl/tools_usage.py`, `tools/metrics_export.py`) dan uji kontrak
   `tests/test_contract.py` perlu ikut diperbarui.

Penolakan selalu lewat `Dispatcher._refuse`. `kind` yang lupa didaftarkan TETAP menolak (pesan umum, metrik
`other`, satu baris log) dan `TestRefusalRegistry::test_every_verdict_kind_produced_in_limiters_is_registered`
menggagalkan build. `check` yang melempar galat dilewati sendiri-sendiri (gagal-terbuka per pembatas, log sekali,
`limiter.<name>` di `capabilities.json`); pembatas berikutnya tetap diperiksa — jadi pembatas tidak boleh dipakai
sebagai batas keamanan (itu tugas gerbang akses di `dispatch.py`, yang gagal-tertutup). Uji dengan simulasi
(`tests/test_scale_sim.py`, jam tiruan) dan `TestLimiterPipeline` / `TestDispatchStageIsolation`.

## Menambah jenis profile

Daftarkan satu `ProfileKind` di `PROFILE_KINDS` (`kit/profiles.py`). Argumen `soul` (teks `SOUL.md`, dengan
placeholder `{team_name}`) WAJIB: ia tidak bisa diturunkan, jadi tambahkan `kit/templates/soul_<jenis>.md` dan
ekspor lewat `kit/templates/__init__.py` (atau pakai ulang teks yang ada). Contoh:

```python
OPS = ProfileKind("ops", SOUL_OPS, _shared_settings, needs_channel=True, reserved=True, model_target=True,
                  optional=True,      # WAJIB untuk jenis ber-channel baru, kecuali semua tim memang harus migrasi
                  note="ops, @mention", channel_label="channel ops", model_label="ops")
# role="admin" / admin_routes=True / free_response=True bila channel-nya khusus admin & tanpa @mention
# allowed_toolsets=(...) bila jenis ini boleh lebih sedikit toolset daripada pekerja biasa (lihat di bawah)
```

**`optional` — jenis baru tidak boleh merusak tim yang sudah ada.** Jenis ber-channel mendapat blok
`<jenis>: {profile, channel_id, auto_skills}` di skema. Dengan `optional=True` blok itu boleh tidak ada (atau
kosong / `false`) di `team.yaml`: jenisnya lalu TIDAK diprovisi untuk tim itu — tanpa profile, tanpa rute, tanpa
entri `channels` di roster, tidak dihitung di plafon RAM, bukan target `set-model` / `llm-key`, dan `models.<jenis>`
di `members.yaml` ditolak dengan pesan yang menyebut sebabnya. Blok yang diisi sebagian (tanpa `channel_id`) tetap
galat. Diuji di `TestOptionalProfileKind`: mendaftarkan jenis opsional tidak mengubah validasi, roster, maupun
config host dari `team.yaml` yang sama.

Tanpa `optional` (bawaan `False`, dipakai `shared` dan `admin`), `<jenis>.channel_id` WAJIB: **setiap `team.yaml`
yang sudah ada gagal dimuat** (`ops.channel_id: ID kosong`) sampai operator menambahkan bloknya. Itu perubahan yang
merusak — bila memang disengaja, ia butuh langkah migrasi di CHANGELOG + bagian *Upgrade* `docs/INSTALL.md`, dan
wizard harus membuat channel-nya (lihat butir 6 di bawah).

Kode yang berjalan untuk SATU tim memakai `active_system_kinds(team)` / `active_model_targets(team)`
(`kit/profiles.py`), bukan `SYSTEM_KINDS` / `MODEL_TARGETS`: dua yang terakhir berisi semua jenis TERDAFTAR,
termasuk jenis opsional yang tidak dipakai tim itu (untuk jenis itu `team[<jenis>]` tetap ada tetapi
`profile`/`channel_id`-nya string kosong). `SYSTEM_KINDS` hanya untuk hal yang tidak bergantung pada tim: skema,
nama yang dicadangkan, teks bantuan.

Yang IKUT OTOMATIS dari entri itu (diuji di `TestRegisteredKindWorksEndToEnd` dan `TestOptionalProfileKind` dengan
jenis palsu): blok `ops: {profile, channel_id, auto_skills}` di `team.yaml` dan baris `llm.ops_model` (skema
dibangun dari registri; tanpa baris itu modelnya `llm.default_model`), peringatan salah ketik, rute gateway (satu
rute per admin bila `admin_routes`), `allowed_channels` / `free_response_channels`, `channel_skill_bindings`,
`models:` di `members.yaml` + alias `set-model`/`llm-key`, `channels` + `kind` di `roster.json`,
`check`/`describe`/`status`, cek channel `kit_doctor`, urutan provisioning (`apply` membuat profile + SOUL +
setelan), keunikan channel/profile, ID contoh (`placeholder_ids`), nama yang dicadangkan, plafon RAM sandbox,
target `set_model` di teamctl (dari `MODEL_TARGETS`), dan perlindungan channel tim di guard Discord admin
(`Server.team_channels` dari `SYSTEM_KINDS`; label `channel_label` + " tim"). Dua yang terakhir diuji di
`tests/test_teamctl_kinds.py` dengan jenis palsu; deskripsi/skema tool `set_model` tetap teks tetap (di-pin hash),
jadi tidak menyebut jenis baru.

**Toolset.** Profile pekerja memakai daftar toolset yang DIIZINKAN: `ProfileKind.allowed_toolsets` (bawaan
`WORKER_ALLOWED_TOOLSETS` di `kit/constants.py`). Toolset yang dikenal kit (`KNOWN_TOOLSETS`) tetapi tidak
diizinkan ditulis ke `agent.disabled_toolsets`; `discord`/`discord_admin` tidak pernah bisa diizinkan, browser /
computer-use hanya dengan `allow_browser`, `cronjob` hanya untuk jenis `cron_exempt` atau dengan
`scale.allow_member_cron`. Saat Hermes menambah toolset, uji integrasi
`TestWorkerToolsetAllowlist::test_every_upstream_toolset_is_decided_for_workers` (dan uji lobby di
`tests/test_hermes_config.py`) gagal di CI sampai toolset itu DIPUTUSKAN: tambahkan ke `LOBBY_DISABLED_TOOLSETS`
(selalu), dan ke `WORKER_ALLOWED_TOOLSETS` hanya bila pekerja boleh memakainya.

Yang TIDAK otomatis (cek satu per satu; tidak ada uji yang mengingatkan, kecuali disebut):

1. `team.example.yaml` + `docs/KONFIGURASI.md` — tambahkan blok contoh (untuk jenis opsional: berkomentar); tabel
   dokumen dibangkitkan (lihat di atas). `test_example_file_documents_exactly_the_schema` hanya memeriksa jenis bawaan.
2. `plugins/team-guard` (sisi plugin sudah generik dan diuji, `TestRegisteredKindInPlugin`): cron guard, rute contoh
   live reload, dan placeholder sambutan `{<jenis>_channel}` membaca roster, bukan nama jenis; `roster_data` menulis
   kunci opsional `role` / `cron_exempt` dari `ProfileKind` (diuji kit → roster, `TestRegisteredKindWorksEndToEnd`).
   `commands.py::resolve_env_target` (kind `personal` = anggota) tidak berubah. Jenis opsional yang tidak dipakai
   tim tidak pernah muncul di roster, jadi plugin tidak perlu tahu soal `optional`.
3. `kit/derive.py::host_settings` — `DISCORD_HOME_CHANNEL` dan `no_thread_channels` memakai channel `admin`.
4. Pesan/doc yang menyebut "shared + admin" secara harfiah (README, docs/FITUR.md).
5. Uji yang mengunci registri bawaan (`TestProfileKinds::test_registry_shape`) bila jenis ditambah permanen.
6. **Wizard (`kit_setup.py`) menulis `shared` dan `admin` secara harfiah** di tiga tempat, dan tidak membaca registri:
   langkah `DiscordServer` (membuat tepat dua channel privat: tupel `("shared", …)`, `("admin", …)` dan kunci state
   `shared_channel_id` / `admin_channel_id`), penulis `team.yaml` (`team["shared"] = …`, `team["admin"] = …`), dan
   ringkasan akhir langkah `verify` (`("shared", "Channel bersama"), ("admin", "Channel admin"), …`). Jenis opsional
   boleh dibiarkan (operator mengisi bloknya sendiri lalu `apply`); jenis WAJIB harus ditambahkan di ketiganya,
   kalau tidak instalasi baru lewat wizard menghasilkan `team.yaml` yang tidak valid.
7. **Migrasi untuk jenis non-opsional**: tiap tim yang sudah berjalan harus (a) membuat channel privatnya di Discord,
   (b) menambah blok `<jenis>: {channel_id: …}` ke `team.yaml`, (c) `apply`. Sebelum (b) semua perintah kit — termasuk
   onboarding otomatis dari plugin dan tool admin — gagal memuat konfigurasi. Tulis langkah ini di CHANGELOG dan di
   *Upgrade* `docs/INSTALL.md`; tidak ada uji yang mengingatkan.
8. `teamctl/` membaca `SYSTEM_KINDS` / `MODEL_TARGETS` (semua jenis terdaftar). Untuk jenis opsional yang tidak
   dipakai tim, kit menolak di lapisannya sendiri (`set_model` → "jenis … tidak dipakai tim ini"), tetapi pesan
   pilihan target di teamctl masih menyebutnya; bila mengganggu, ganti ke `active_model_targets(team)` /
   `active_system_kinds(team)` di `teamctl/tools_team.py` dan `teamctl/discord_guard.py`.

Uji jenis palsu memakai fixture `register_kind` (`tests/conftest.py`): ia mendaftarkan di salinan-uji registri dan
memanggil `kit_config.rebuild_schema()`; `SYSTEM_KINDS`/`MODEL_TARGETS`/`RESERVED_NAMES` adalah tampilan malas, jadi
tidak ada salinan basi. Untuk jenis `optional=True` fixture `kit` TIDAK perlu ditulis ulang (itu intinya); untuk
jenis wajib uji harus memanggil `kit.write_team(<jenis>={...})` setelah mendaftar.

## Menyentuh internal Hermes

Hanya lewat `plugins/team-guard/upstream.py`. Pendaftaran *capability* TIDAK otomatis; yang ada adalah dua
langkah manual yang dijaga uji:

1. satu entri di tabel `_PROBES` (kunci, modul tempat mencarinya, kelas pemilik, nama, untuk apa);
2. `@uses("<kunci>", …)` di metode `HermesGateway` yang menyentuhnya.

`TestUpstreamProbeCoverage` memindai AST `upstream.py` dan gagal bila sebuah metode `HermesGateway` mengimpor
paket Hermes, membaca atribut objek Hermes (parameter `gateway` / `runner` / `adapter` / `event` / `config` /
`source` dan turunannya), atau menyebut nama privat tanpa probe yang dideklarasikannya — juga bila ada probe
tanpa pemakai atau parameter baru yang belum digolongkan. `TestUpstreamAdapterLayer` menjaga modul LAIN tidak
menyentuh internal sama sekali. Bila API hilang, fitur itu mati dengan satu baris log dan `capabilities.json`
mencatatnya (`kit_doctor.py` menampilkannya). Batasnya: probe hanya memeriksa bahwa NAMA itu ada (di objek yang
sudah dimuat atau di teks sumber), bukan tanda tangan atau perilakunya; probe atribut data (`….config.profile_routes`,
`….config.extra`) hanya mencari pemakaian nama itu di sumber. `capabilities.json` juga memuat entri kesehatan
plugin yang bukan API Hermes (`budget.ledger`, `limiter.<nama>`, `dispatch.<tahap>`; `upstream.note_health`).
Uji yang menyentuh Hermes asli dijaga `importorskip` dan berjalan di CI.

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

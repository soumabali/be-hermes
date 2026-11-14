# Kontrak antar komponen

Satu halaman untuk semua berkas dan baris yang dipertukarkan antara **kit** (`hermes_team.py` + paket `kit/`),
**plugin** team-guard, **teamctl** (server MCP admin), dan **tools/**. Bentuknya didefinisikan sebagai `TypedDict`
di [`kit_contracts.py`](../kit_contracts.py) (stdlib saja), dan diuji di `tests/test_contract.py` serta
`tests/test_kit_units.py` — kalau satu sisi berubah tanpa yang lain, ujinya gagal.

## Aturan versi (berlaku untuk semua kontrak berkas)

1. **Pembaca mengabaikan kunci yang tidak dikenal.** Produsen boleh menambah kunci kapan saja tanpa menaikkan versi.
2. **Pembaca yang melihat `schema` lebih besar dari yang didukungnya** mencatat SATU kali di log lalu tetap
   berjalan sebisanya (best-effort). Tidak menolak, tidak crash.
3. **Pembaca tanpa kunci `schema`** menganggapnya 0 (berkas dari kit lama) dan memakai nilai bawaan untuk kunci
   yang hilang.
4. **Produsen menaikkan `schema`** hanya untuk perubahan yang merusak: kunci dihapus, arti/satuan berubah, bentuk
   berubah. Kenaikan dicatat di `CHANGELOG.md` dan di tabel bawah.
5. Berkas ditulis **atomik** (tulis ke berkas sementara + `os.replace`), jadi pembaca tidak pernah melihat isi
   setengah jadi. Pembaca tetap harus tahan terhadap berkas hilang atau tidak terbaca.

## Ringkasan

| Kontrak | Produsen | Pembaca | Versi | Diuji di |
|---|---|---|---|---|
| `state/roster.json` | kit (`kit/derive.py::roster_data`, ditulis `kit/provision.py::write_roster`) | plugin (`roster.py`, `livereload.py`, `onboarding.py`, `budget.py`, ...), teamctl, `kit_doctor.py` | `schema: 1` (+ `version: 3` lama) | `test_kit_units.py::TestRosterContract`, `test_contract.py` |
| `state/metrics.json` | plugin (`scale.py::build_metrics`, ditulis `Monitor` tiap 30 dtk) | teamctl (`team_status`), `tools/metrics_export.py` | `schema: 1` | `test_contract.py`, `test_scale_sim.py` |
| `state/budget_overrides.json` | teamctl (`tools_usage.py`) | plugin (`budget.py::overrides`) | tanpa `schema` (dipetakan per user ID) | `test_contract.py` |
| `state/removed_channels.json` | kit (`kit/membership.py::remove_member`, di bawah kunci tim) | teamctl (`Server.personal`: channel bekas anggota tetap dianggap pribadi) | `schema: 1` | `test_kit_units.py::TestRemoveMember` |
| `--events` (JSON per baris, stdout) | kit CLI (`onboard`, `offboard`, `onboard-batch`) | plugin (`onboarding.py`) | tanpa `schema` (per baris; kunci `event` membedakan jenis) | `test_onboarding.py`, `test_kit_units.py` |
| `state/usage.db` (SQLite) | `usage_ledger.py` (kolektor, dijalankan plugin/cron) | plugin (anggaran), teamctl, `usage_report.py`, `tools/usage_post.py` | `meta.schema_version = 1` (`usage_ledger.SCHEMA_VERSION`) | `test_usage_ledger.py`, `test_contract.py` |
| `team.yaml` / `members.yaml` | manusia, `kit` (members), teamctl | kit (`kit_config` = skema), `kit_paths.load_raw` untuk alat ringan | skema = tabel `kit_config.FIELDS` | `test_kit_units.py::TestConfigSchema` |
| `VERSION` | manusia (rilis) | `kit_base.KIT_VERSION`; plugin dan teamctl membandingkan dengannya | satu baris `X.Y.Z` | `test_kit_units.py::TestVersionSource` |

## `state/roster.json`

* **Produsen:** `roster_data()` di `kit/derive.py`; ditulis paling akhir oleh `apply` (rute baru tidak boleh terbit
  sebelum profile tujuannya ada).
* **Bentuk:** `kit_contracts.Roster` — `schema`, `version` (lama, 3), `kit_version`, `generated`, `team_name`,
  `guild_id`, `hermes_root`, `hermes_bin`, `admins`, `roles`, `shared_channel`, `admin_channel`, `category_id`,
  `channels` (channel → profile/kind), `users`, `inactive_users`, `reserved_env`, `gateway` (rute + channel yang
  diinginkan), `live_reload`, `restart_fallback`, `onboarding`, `kit`, `settings`, `usage`.
* **Kunci opsional entri `channels` (aditif, `schema` tetap 1):** `role` (`"admin"` bila jenis ber-role admin; hilang =
  bukan admin) dan `cron_exempt` (`true` = boleh membuat cron rapat). Hilang pada roster lama: plugin jatuh ke perilaku
  lama (hanya `kind: "admin"`). Plugin juga membentuk placeholder `{<kind>_channel}` dari `kind` di `channels`.
* **`settings`, `onboarding`, `usage`, `live_reload`, `restart_fallback` DITURUNKAN dari tabel**
  `kit_config.FIELDS`: hanya baris yang punya `plugin_key` (dan `plugin_section` yang sesuai) yang masuk. Menambah
  setelan yang ditegakkan plugin = satu baris di tabel + titik pakainya di plugin; tidak ada daftar kunci lain yang
  harus disamakan.
* **Pembaca:** plugin membuka lewat `roster.py` (mengabaikan kunci tak dikenal; `schema` lebih besar → log sekali).
  Nilai bawaan plugin untuk kunci yang hilang ada di `plugins/team-guard/roster.py`.
* **Versi:** `schema` naik bila kunci dihapus atau artinya berubah. `version: 3` tetap ada demi kompatibilitas dan
  TIDAK boleh dipakai untuk keputusan baru.

## `state/metrics.json`

* **Produsen:** `plugins/team-guard/scale.py::build_metrics`, diperbarui `Monitor` tiap 30 detik (atomik).
* **Bentuk:** `kit_contracts.Metrics` — `schema`, `ts`, `active_sessions`, `active_by_user`, `llm_inflight`,
  `llm_inflight_peak_5m`, `queue_len`, `queue_wait_p95_s`, `refused` (`rate`, `queue_full`, `budget`,
  `user_concurrent`), `http_429_last_minute`, `effective_max_active`, `configured_max_active`, `throttled`,
  `budget` (`day`, `used_by_user`, `team_used`, `stale`, `last_collect`).
* **Pembaca:** teamctl `team_status` (berkas lebih tua dari beberapa menit diberi label "DATA BASI"),
  `tools/metrics_export.py` (Prometheus). Keduanya mengabaikan kunci tak dikenal.

## `state/budget_overrides.json`

* **Produsen:** teamctl `set_usage_budget_extra` / `clear_usage_budget_extra`.
* **Bentuk:** `{user_id: {"day": "YYYY-MM-DD", "extra_tokens": int > 0}}` (`kit_contracts.BudgetOverride`).
  Hanya berlaku pada `day` (zona waktu `usage.timezone`); menaikkan batas ORANG itu saja, bukan plafon tim.
* **Versi:** tanpa `schema` karena kunci tingkat atas adalah user ID; bila bentuknya perlu berubah, berkas baru
  dengan nama baru dipakai berdampingan dulu.

## `state/removed_channels.json`

* **Produsen:** `remove-member` (`kit/membership.py::record_removed_channel`), di bawah kunci tim, atomik, mode 0600.
  Ditulis SEBELUM entri dibuang dari `members.yaml` (crash di antaranya tidak meninggalkan channel tanpa
  perlindungan) dan tetap ditulis bila `--delete-channel` gagal. Hanya bila channel benar-benar berhasil dihapus
  entrinya dibuang lagi. Selain itu TIDAK pernah dihapus otomatis. Berkas hilang / rusak diganti yang baru.
* **Bentuk:** `kit_contracts.RemovedChannels` — `{"schema": 1, "channels": {"<channel_id>": {"name": "<nama channel
  atau \"\">", "member": "<nama anggota>", "removed_at": "<ISO UTC Z>"}}}`.
* **Pembaca:** teamctl `Server.personal`: channel di berkas ini diperlakukan sebagai channel pribadi (ditolak untuk
  baca/tulis/hapus lewat tool admin) walau anggotanya sudah tidak ada. Pembaca mengabaikan kunci tak dikenal.

## `--events` (JSON per baris)

Dipakai `hermes_team.py onboard|offboard|onboard-batch --events` (stdout; stderr tidak dipakai). Satu objek per
baris, dibedakan oleh `event`:

| `event` | Kunci | Keterangan |
|---|---|---|
| `channel` | `channel_id`, `created`, `reused?`, `user_id?` | channel pribadi siap (`user_id` hanya di `onboard-batch`) |
| `result` | `status`, `user_id`, `name`, `display_name`, `profile`, `channel_id`, `model`, `notes`, `restart_needed`, `live_changed`, `failures` | satu anggota (`kit_contracts.ResultEvent`) |
| `error` | `message`, `log?` | kegagalan; `log` = 15 baris log terakhir |
| `summary` | `total`, `counts`, `failed`, `restart_needed`, `live_changed`, `failures` | hanya `onboard-batch` |

Kode keluar: 0 sukses, 1 gagal (ada baris `error`), 3 ada `failures` pada hasil, 2 kesalahan tak terduga.
Pembaca (plugin) mengabaikan `event` yang tidak dikenal dan kunci tambahan.

## `state/usage.db`

SQLite; tabel `meta` menyimpan `schema_version` (`usage_ledger.SCHEMA_VERSION`, kini 1), dicatat saat berkas dibuat.
**Pembaca dan penulis memeriksanya** (`usage_ledger._check_schema_version`): buku besar dengan versi LEBIH BARU
ditolak dengan `LedgerError` ("perbarui kit"), tanpa menulis apa pun; versi sama, lebih lama, atau tanpa baris
versi diterima. Bentuk tabel SQL tidak bisa dibaca "sebisanya", jadi perubahan merusak harus menaikkan
`SCHEMA_VERSION`. Satu-satunya penulis adalah kolektor `usage_ledger.py` (kunci tulis `usage.db.lock`).

## `team.yaml` dan `members.yaml`

* **Skema:** satu tabel, `kit_config.FIELDS` (path YAML, tipe, default, batas/pilihan, deskripsi, `plugin_key`).
  Satu validator (`kit_config.parse_section` / `parse_top_level`) menghasilkan nilai bertipe dan pesan `TeamError`
  yang menyebut key-nya; `unknown_key_warnings` memberi saran "maksudnya …?".
* **Dokumentasi otomatis:** `python3 hermes_team.py config-doc` (tabel Markdown; `--write docs/KONFIGURASI.md` memperbarui berkasnya), `python3 kit_config.py
  --emit-example-section <bagian>` (cuplikan YAML). `tests/test_kit_units.py::TestConfigSchema` gagal bila
  `team.example.yaml` tidak mendokumentasikan sebuah field atau memuat key yang tidak ada di skema.
* **Alat ringan** (`usage_ledger`, `usage_report`, `tools/usage_post.py`, `kit_doctor`) membaca YAML lewat SATU
  pembaca, `kit_paths.load_raw()`, dan tampilan `usage.*` tervalidasi lewat `kit_paths.usage_view()` (skema
  diimpor malas dari `kit_config`).

## Versi kit

`VERSION` di akar repo adalah satu-satunya sumber. `kit_base.KIT_VERSION` membacanya (konstanta cadangan hanya bila
berkas hilang; ujinya memastikan keduanya sama). Plugin (`plugin.yaml`, `shared.py`) dan `teamctl_mcp.py` harus
membaca atau membandingkan dengan berkas itu, bukan menyalin string-nya.

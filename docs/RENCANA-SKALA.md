# Rencana perbaikan: skala 20 orang

Dokumen ini menilai tabel "Skala 20 orang" di README §10 terhadap kode kit v3.1 dan Hermes
(commit `5d87ea2`, 12 Nov 2026), lalu menyusun perbaikannya menurut prioritas.

Status tiap temuan: **terbukti** = terlihat langsung di kode; **perlu diukur** = masuk akal dari
kode/dokumentasi tetapi dampaknya baru jelas setelah diuji di server.

## Status pengerjaan

Rencana ini sudah dijalankan. "Selesai" berarti kodenya ada dan lulus uji unit serta simulasi;
**tidak ada** yang sudah dijalankan terhadap Hermes, Discord, Docker, atau endpoint LLM sungguhan.

| # | Butir | Status | Sisa |
|---|---|---|---|
| 1 | Batas sesi per orang | Selesai | Baru aktif penuh setelah satu sesi selesai sejak gateway start |
| 2 | Cron, `/goal`, subagent | Selesai | Panggilan auxiliary (judul, kompresi) belum dihitung |
| 3 | Fallback dan 429 | Selesai | Pembatas adaptif menurunkan batas sesi, bukan `max_llm_parallel` |
| 4 | Ukur kapasitas endpoint | Selesai (`tools/probe_llm.py`) | Harus dijalankan di server; angka bawaan belum diganti |
| 5 | Anggaran token | Selesai | Mati secara bawaan; status "sudah diperingatkan" hilang saat restart |
| 6 | Sandbox menganggur dan RAM | Sebagian | `sandbox_idle.py` hanya melaporkan; `memory_mb` per orang dan bawaan 1024 belum |
| 7 | Metrik | Selesai | Belum ada ekspor ke Prometheus/DataDog |
| 8 | Antrean adil | Selesai | Antrean sesi-sibuk milik Hermes tetap di luar pembatas |
| 9 | Kompresi per model | Belum | Butuh data pemakaian nyata dulu |
| 10 | Retensi | Sebagian | Retensi sesi selesai; durasi arsip thread belum diatur |
| 11 | Onboarding massal | Selesai (`onboard-batch`) | — |
| 12 | Uji beban | Sebagian | Simulasi dan LLM tiruan ada; penggerak 20 user ke gateway sungguhan belum |

Langkah berikutnya yang paling bernilai: jalankan `tools/probe_llm.py` di server, isi
`max_llm_parallel`, lalu lakukan uji manual di `tests/loadtest/README.md`.

## Ringkasan

Mekanisme yang ada membatasi **jumlah pesan** dan **jumlah sesi**, tetapi tiga hal yang paling
menentukan biaya dan kestabilan belum dibatasi: sesi paralel per orang, pekerjaan tanpa pesan
(cron, `/goal`, subagent), dan pemakaian token. Selain itu belum ada cara mengukur apakah angka di
`team.yaml` cocok dengan kapasitas endpoint LLM dan RAM server.

| # | Celah | Dampak | Prioritas |
|---|---|---|---|
| 1 | Satu orang bisa memakai banyak slot sekaligus | Antrean untuk orang lain | P0 |
| 2 | Cron, `/goal`, subagent tidak lewat pembatas | Beban LLM tak terlihat dan tak terbatas | P0 |
| 3 | Fallback memakai endpoint yang sama | 429 tidak tertolong | P0 |
| 4 | Kapasitas endpoint tidak pernah diukur | `max_concurrent_sessions` hanya tebakan | P0 |
| 5 | Tidak ada anggaran token per orang | Biaya tak terkendali | P1 |
| 6 | Container sandbox tidak pernah dimatikan | RAM dan disk tumbuh terus | P1 |
| 7 | Tidak ada metrik antrean/429/sesi | Masalah baru ketahuan dari keluhan | P1 |
| 8 | Antrean FIFO murni, hilang saat restart | Tidak adil, pesan lenyap | P2 |
| 9 | Ambang kompresi satu angka untuk semua model | Boros atau terlalu agresif | P2 |
| 10 | Riwayat sesi dan thread tidak pernah dibersihkan | `state.db` melambat | P2 |
| 11 | Onboarding massal lambat | 20 orang ≈ 10–30 menit | P3 |
| 12 | Belum ada uji beban | Semua di atas belum terbukti | P0 (prasyarat) |

## Temuan dan perbaikan

### 1. Satu orang bisa memakai banyak slot sekaligus — P0, terbukti

**Sekarang.** Pembatas per orang hanya token bucket pesan (`RateLimiter` di
`plugins/team-guard/ratelimit.py`): 4 pesan langsung, lalu 6 per menit. Antrean masuk
(`Dispatcher._admit` di `dispatch.py`) menghitung sesi aktif **seluruh tim**, bukan per orang.
Sejak `threads.personal: true`, setiap pesan baru di channel pribadi membuka sesi baru. Jadi satu
orang yang menulis 4 pesan beruntun memegang 4 dari 8 slot, masing-masing bisa berjalan sampai 60
langkah tool.

**Perbaikan.**
- Setelan baru `scale.user_max_concurrent` (bawaan 2): sesi aktif per orang. Pesan ke-3 menunggu di
  antrean orang itu sendiri sampai salah satu sesinya selesai.
- `TurnTracker` sudah menyimpan `uid` per sesi aktif; tinggal menambah hitungan per `uid` dan
  memeriksanya di `_admit` sebelum masuk antrean global.
- Pesan ke sesi yang sedang berjalan (balasan di thread yang sama) tetap lolos, seperti sekarang.

**Selesai bila.** Uji: satu user mengirim 4 pesan ke 4 thread → 2 berjalan, 2 antre; user lain
tetap langsung dilayani.

### 2. Pekerjaan tanpa pesan tidak lewat pembatas — P0, terbukti

**Sekarang.** `Dispatcher._handle` langsung melewatkan `event.internal`, dan cron tidak melalui
`pre_gateway_dispatch` sama sekali. Toolset `cronjob`, `delegation`, serta perintah `/goal`,
`/subgoal`, dan `/bg` tersedia untuk anggota. Akibatnya:
- cron job tiap profile berjalan di luar `max_concurrent_sessions` versi kit dan di luar rate limit;
- `/goal` bisa melanjutkan sendiri sampai 8 kali × 60 langkah = 480 langkah tool dari satu pesan;
- tiap giliran boleh memanggil 2 subagent, jadi 8 sesi bisa berarti 24 percakapan LLM paralel.

**Perbaikan.**
- Hitung beban dalam **panggilan LLM paralel**, bukan sesi. Tambah pelacak di hook
  `pre_api_request`/`post_api_request` (pengamat, sudah ada di Hermes) dan pakai angka itu di
  `LoadProbe` sebagai sumber ketiga.
- Cron: set `cron.model` ke model murah di managed config, batasi `HERMES_CRON_MAX_PARALLEL=1`
  per profile, dan tolak jadwal lebih rapat dari 15 menit lewat `pre_tool_call` pada tool cron.
  Pilihan paling aman untuk awal: matikan toolset `cronjob` bagi anggota
  (`scale.allow_member_cron: false`) dan buka per orang bila diminta.
- `/goal`: turunkan `goals_max_turns` ke 4 dan keluarkan `goal`, `subgoal` dari
  `DEFAULT_USER_COMMANDS` kecuali `scale.allow_goals: true`.
- Subagent: `delegation_max_children: 1` sebagai bawaan; 2 hanya untuk model `td-normal`.

**Selesai bila.** Panggilan LLM paralel terukur tidak pernah melebihi batas di uji beban (lihat #12),
termasuk saat cron dan `/goal` aktif.

### 3. Fallback memakai endpoint yang sama — P0, terbukti

**Sekarang.** `model_settings()` menyusun `fallback_providers` dari `llm.fallback`. Di contoh
konfigurasi isinya `td-simple`, yaitu model lain di **provider dan endpoint yang sama**
(`custom:td`). Bila endpoint itu mengembalikan 429 atau mati, fallback ikut gagal. Anggota yang
modelnya sudah `td-simple` tidak punya fallback sama sekali (model yang sama dilewati).

**Perbaikan.**
- `check` memberi peringatan bila semua fallback berada di provider yang sama dengan model utama.
- Dokumentasikan dan beri contoh provider kedua di `team.example.yaml` (endpoint cadangan atau
  key kedua), serta credential pool Hermes (`credential-pools`) untuk beberapa key di satu endpoint.
- Pembatas adaptif: hook `api_request_error` menghitung 429; bila melewati ambang dalam 60 detik,
  plugin menurunkan batas sesi efektif (mis. separuh) dan menaikkannya lagi perlahan setelah
  tenang. Admin diberi kabar sekali per kejadian.

**Selesai bila.** Uji dengan LLM tiruan yang mengembalikan 429 pada >N paralel: tidak ada giliran
yang gagal total, batas efektif turun, lalu pulih.

### 4. Kapasitas endpoint tidak pernah diukur — P0, terbukti

**Sekarang.** README hanya memberi patokan "`max_concurrent_sessions` ≤ jumlah request paralel
yang sanggup dilayani endpoint". Tidak ada alat untuk mengetahui angka itu, dan bawaan 8 + 2 admin
dipakai apa adanya. Hermes sendiri tidak membatasi: executor giliran tidak berbatas dan
`max_concurrent_sessions` adalah satu-satunya gerbang (`gateway/run.py`, `_get_executor`).

**Perbaikan.** Skrip `tools/probe_llm.py`: mengirim permintaan pendek ke endpoint dengan paralel
1, 2, 4, 8, 16, 32; mencatat latensi p50/p95, token per detik, dan tingkat 429; lalu mencetak saran
`max_concurrent_sessions` (paralel tertinggi tanpa 429 dan p95 < 2× dasar, dibagi faktor
subagent dari #2). Jalankan saat instalasi dan tiap ganti endpoint.

**Selesai bila.** Angka di `team.yaml` berasal dari keluaran skrip, bukan bawaan.

### 5. Tidak ada anggaran token per orang — P1, terbukti

> **Sebagian sudah ada:** pencatatannya (`usage_ledger.py`, README §10b) sudah dibuat. Yang belum:
> penegakan anggaran (peringatan 80%, penolakan 100%) di plugin.

**Sekarang.** Pembatas menghitung pesan. Satu pesan bisa menghabiskan 60 langkah tool dengan
konteks 64k token, jadi 6 pesan per menit bisa berarti jutaan token. `usage_report.py` hanya
melaporkan setelah kejadian.

**Perbaikan.**
- `scale.daily_token_budget` (per orang) dan `scale.team_daily_token_budget`. Plugin membaca
  `session_model_usage` di `state.db` profile itu (kode yang sama dengan `usage_report.py`,
  hasilnya di-cache 60 detik).
- 80% → peringatan sekali di thread; 100% → pesan baru ditolak sopan sampai tengah malam, admin
  dikabari. Admin bisa menambah jatah: "tambah kuota budi 2 juta token".
- Laporan mingguan otomatis ke #hermes-admin (cron profile admin menjalankan `usage_report.py`).
- Bila gateway LLM mendukung key per orang, aktifkan `llm.per_user_keys` supaya kuota juga
  ditegakkan di sisi server.

**Selesai bila.** Uji: user melewati anggaran → pesan berikutnya ditolak, user lain tidak terpengaruh.

### 6. Container sandbox tidak pernah dimatikan — P1, perlu diukur

**Sekarang.** Managed config memakai `container_persistent: true`. Menurut dokumentasi Hermes,
pada mode ini container bertahan dari pembersih idle (`lifetime_seconds` hanya berlaku bila
`docker_persist_across_processes: false`). Dengan 20 anggota + shared + admin, ada sampai 22
container yang terus hidup, masing-masing dibatasi 2 GB. Batas itu plafon, bukan cadangan, tetapi
proses yang tertinggal (server dev, watcher) tetap memakan RAM. `/workspace` juga tidak berkuota.
Baris "Beban RAM" di tabel hanya mematikan browser.

**Perbaikan.**
- Ukur dulu: `docker stats` setelah seminggu pemakaian nyata.
- Timer systemd `hermes-sandbox-idle`: `docker stop` container berlabel `hermes-profile=*` yang
  tidak punya proses aktif selama N jam. Perlu dibuktikan bahwa Hermes menyalakannya lagi saat
  dipakai (dokumentasi menyebut ia menempel ke container yang ada; perilaku untuk container
  berstatus *Exited* harus diuji).
- Turunkan bawaan `memory_mb` ke 1024 dan beri pengecualian per orang
  (`members.yaml: sandbox: {memory_mb: 4096}`).
- `kit_doctor.py --docker`: pemakaian RAM dan disk per profile, peringatan di atas ambang.
- Tabel ukuran server di README: RAM ≈ 4 GB (gateway) + `max_concurrent_sessions` × `memory_mb`.

**Selesai bila.** Server tetap di bawah 80% RAM setelah seminggu tanpa campur tangan.

### 7. Tidak ada metrik — P1, terbukti

**Sekarang.** Panjang antrean, lama tunggu, jumlah penolakan rate limit, 429, dan sesi aktif hanya
ada di memori plugin. Admin tidak bisa melihatnya.

**Perbaikan.**
- Plugin menulis `state/metrics.json` tiap 30 detik (atomik): sesi aktif, antrean, tunggu p95,
  penolakan per jenis, 429 per menit, batas efektif.
- Tool teamctl `team_status` membacanya ("bagaimana beban sekarang?").
- Peringatan ke #hermes-admin sekali per kejadian: antrean > 5 selama 2 menit, 429 beruntun,
  penolakan `queue-full`.
- Pertimbangkan ekspor kesehatan bawaan Hermes (`gateway-monitoring`) bila tim punya DataDog/Prometheus.

**Selesai bila.** Admin bisa menjawab "kenapa bot lambat tadi siang?" dari data.

### 8. Antrean FIFO murni dan hilang saat restart — P2, terbukti

**Sekarang.** `AdmissionQueue` adalah daftar di memori. Orang yang mengirim 3 pesan menempati 3
posisi. Restart gateway mengosongkan antrean tanpa pemberitahuan. Pesan yang dikirim saat sesi
sibuk masuk antrean Hermes tanpa lewat hook (dicatat di README §8), jadi tidak terkena rate limit.

**Perbaikan.**
- Giliran adil: satu tiket per orang di antrean global, sisanya menunggu di belakang tiket itu.
- Saat `on_unload`/restart, kirim "antrean dibatalkan, kirim ulang" ke pemilik tiket.
- Batasi antrean sesi-sibuk Hermes lewat setelan upstream bila tersedia; kalau tidak, catat
  sebagai batasan.

### 9. Ambang kompresi satu angka — P2, perlu diukur

**Sekarang.** `compression_threshold_tokens: 64000` dan `proactive_prune_tokens: 48000` berlaku
untuk semua profile lewat managed config. Catatan dari bawaan Hermes: tiap pemangkasan menulis
ulang riwayat dan memutus prefix prompt cache, sehingga terlalu sering memangkas justru menaikkan
biaya. Dengan thread per percakapan, sesi cenderung lebih pendek, jadi ambang ini lebih jarang
tercapai daripada saat tabel itu dibuat.

**Perbaikan.**
- Ukur: `usage_report.py` ditambah kolom rasio `cache_read_tokens` dan jumlah kompresi per sesi.
- Jadikan ambang per model di `llm.models.<alias>` (mis. `context_tokens`), bukan global.
- Tinjau `idle_compact_after_seconds`: thread lama yang dibuka lagi akan diringkas; pastikan itu
  yang diinginkan untuk thread referensi.

### 10. Riwayat tidak pernah dibersihkan — P2, terbukti

**Sekarang.** `sessions.auto_prune` Hermes mati secara bawaan dan kit tidak menyetelnya. Dengan
thread per percakapan, jumlah sesi per profile tumbuh jauh lebih cepat. Discord juga membatasi
sekitar 1000 thread aktif per server; thread yang diarsipkan tidak dihitung.

**Perbaikan.** Managed config: `sessions.auto_prune: true`, `retention_days: 90`, arsip otomatis
30 hari. Setel durasi arsip otomatis thread ke 3 hari. `kit_doctor.py` melaporkan ukuran `state.db`.

### 11. Onboarding massal lambat — P3, terbukti

**Sekarang.** Onboarding diproses satu per satu di bawah kunci tim, 30–90 detik per orang.

**Perbaikan.** Untuk hari pertama, isi `members.yaml` lalu satu kali `apply` (lebih cepat daripada
20 kali event role). Tambah `onboard --batch <berkas>` yang membuat semua channel dulu, lalu satu
`apply`, lalu satu live reload.

### 12. Belum ada uji beban — P0, prasyarat

**Sekarang.** 944 uji memeriksa logika satu per satu dengan jam tiruan. Tidak ada uji yang
menjalankan 20 pengguna bersamaan, dan kit belum pernah dijalankan terhadap Hermes sungguhan.

**Perbaikan.** `tests/loadtest/`: LLM tiruan OpenAI-compatible (latensi dan 429 bisa diatur) +
skrip yang mengirim pesan sebagai 20 user ke gateway Hermes sungguhan lewat Discord tiruan.
Mengukur: panggilan LLM paralel puncak, lama tunggu antrean, penolakan, RAM. Dijalankan manual di
server uji dan sebagai job CI malam.

## Urutan pengerjaan

| Tahap | Isi | Perkiraan |
|---|---|---|
| A | #12 uji beban + #4 `probe_llm.py` (alat ukur dulu) | 2–3 hari |
| B | #1 batas sesi per orang, #2 pembatas panggilan LLM + bawaan cron/goal, #3 peringatan fallback + pembatas adaptif | 3–4 hari |
| C | #7 metrik + `team_status`, #5 anggaran token | 3 hari |
| D | #6 sandbox idle + ukuran server, #10 retensi | 2 hari |
| E | #8 antrean adil, #9 kompresi per model, #11 onboarding batch | 2–3 hari |

Tahap A harus pertama: tanpa alat ukur, tahap B–E tidak bisa dibuktikan berhasil.

## Setelan baru yang diusulkan (`team.yaml` → `scale`)

```yaml
scale:
  user_max_concurrent: 2          # sesi aktif per orang
  max_llm_parallel: 12            # panggilan LLM paralel seluruh tim (dari probe_llm.py)
  daily_token_budget: 3000000     # per orang; 0 = tanpa batas
  team_daily_token_budget: 0
  allow_member_cron: false
  allow_goals: false
  adaptive_throttle: true         # turunkan batas saat 429 beruntun
```

## Yang belum diketahui

- Apakah `on_session_end` dipanggil tiap giliran gateway (dipakai `TurnTracker`); bila tidak,
  hitungan sesi aktif hanya bergantung pada `_running_agent_count()`.
- Apakah Hermes menyalakan kembali container sandbox yang dihentikan.
- Batas paralel dan kuota endpoint LLM tim yang sebenarnya.
- Apakah cron profile yang dilayani multiplexer dihitung dalam `max_concurrent_sessions` Hermes.

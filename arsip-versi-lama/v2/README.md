# Hermes untuk tim backend — kit v2

Satu instalasi Hermes, satu bot Discord, satu gateway (mode multiplex), dan **satu profile per
orang**. Tiap channel dirutekan ke profile-nya, jadi memory, skill, SOUL.md, config, model,
secret, sesi, dan sandbox Docker tiap orang terpisah.

```
#hermes-be1   (privat: BE1 + bot)    → profile be-be1      model td-normal, secret & sandbox BE1
#hermes-budi  (privat: Budi + bot)   → profile be-budi     model td-simple
#hermes-shared (semua @backend)      → profile team-shared skill & konteks tim, sesi per orang
#hermes-admin  (hanya user admin)    → profile team-admin  + tool admin (teamctl)
channel lain / DM                    → diabaikan / ditolak
thread di channel mana pun           → ikut profile channel induknya, sesi terpisah
```

## Yang baru di v2

| Kebutuhan | Cara kit memenuhinya |
|---|---|
| Admin minta bot menambah user + channel | Tool `add_member` di #hermes-admin: buat channel privat `#hermes-<nama>` di kategori Hermes, profile dari template, model, rute gateway. Lalu `/restart`. |
| Skill satu orang dibagikan ke orang lain / semua | Tool `share_skill`: skill dipindai dulu (nilai secret pemilik, pola kredensial, pemindai keamanan Hermes), lalu disalin ke kategori `shared/` milik penerima. Anggota baru otomatis dapat skill yang dibagikan ke "semua". |
| Model per orang (BE1 td-normal, lainnya td-simple) | `members.yaml` → `model:` per orang, atau tool `set_model` di #hermes-admin. |
| User menambah env secara aman lewat command | `/env-set` membuka form Discord pribadi (nama + nilai). Nilai tidak pernah lewat chat. Juga `/env-list`, `/env-unset`. |
| Isi .env tidak diambil / dibagikan AI | Delapan lapis perlindungan, lihat [§7](#7-secret-pribadi-anggota). |
| User tidak bisa mengganti model | `/model`, `/moa`, `/reasoning`, `/fast`, `/tools`, `/config` diblok untuk anggota; kebijakan global dipin di `/etc/hermes/config.yaml`. |
| 20 orang: konteks tidak bengkak, memory tidak campur, tahan throttle | Batas kompresi, antrean & rate limit per orang, model auxiliary murah, fallback model. Lihat [§9](#9-skala-20-orang). |

Setiap aksi admin yang mengubah sesuatu meminta **tombol konfirmasi** di #hermes-admin, dan
#hermes-admin hanya melayani user ID yang ada di `admins`.

## Isi kit

| File | Fungsi |
|---|---|
| `hermes_team.py` | CLI provisioning & operasi (check, apply, managed, add-member, set-model, share-skill, …). Juga dipakai tool admin. |
| `teamctl_mcp.py` | Server MCP tool admin; dipasang otomatis hanya di profile `team-admin`. |
| `plugins/team-guard/` | Plugin Hermes: `/env-set`, penyamaran secret, guard perintah, rate limit & antrean. Dipasang ke semua profile. |
| `team.example.yaml` | Setelan tim (guild, role, admin, channel, katalog model, sandbox, skala, kebijakan secret). |
| `members.example.yaml` | Anggota + model per orang. |
| `egress/` | Opsional: proxy allowlist supaya sandbox hanya bisa ke domain tertentu. |
| `tests/selftest.sh` | Uji seluruh kit terhadap Hermes di server ini, di HOME sementara. |

## 0. Prasyarat

- Server Linux dengan Docker. User Linux khusus, misal `hermes` (bukan root), anggota grup `docker`.
- Hermes terpasang di user itu, versi terbaru, platform Discord sudah disetel:
  ```bash
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
  hermes update
  hermes gateway setup        # pilih Discord, tempel token bot
  ```
- `python3` dengan PyYAML (`sudo apt install python3-yaml`).
- Endpoint LLM OpenAI-compatible untuk model `td-normal` / `td-simple` dan API key-nya.

## 1. Discord

1. Satu aplikasi bot di Developer Portal, **Message Content Intent** dan **Server Members Intent**
   aktif. Link undangan harus memuat scope `bot` dan `applications.commands`.
2. Role `@backend` (semua anggota) dan `@hermes-admin`.
3. Kategori **Hermes** berisi `#hermes-shared` (dilihat `@backend`) dan `#hermes-admin` (hanya
   `@hermes-admin`). Channel pribadi boleh dibuat manual (`@everyone` deny View Channel; orangnya +
   bot allow) atau dibiarkan dibuat oleh tool `add_member`.
4. Izin role bot di kategori Hermes: View Channel, Send Messages, Send Messages in Threads, Read
   Message History, Embed Links, Attach Files, Add Reactions, Create Public Threads, plus
   **Manage Channels, Manage Roles (Permissions), Manage Messages, Manage Threads**. Empat terakhir
   dipakai untuk membuat channel anggota baru dan menghapus pesan yang berisi secret.
   Supaya bot bisa memberi role `@backend` ke anggota baru, role bot harus di atas `@backend`.
5. Developer Mode → salin ID server, kategori, channel, role, dan user.

## 2. Pasang kit

Kode kit dimiliki root (tidak bisa diubah user `hermes` maupun agent); data tim milik `hermes`.

```bash
sudo mkdir -p /opt/hermes-team
sudo cp -r hermes-team-kit/. /opt/hermes-team/
sudo chown -R root:root /opt/hermes-team && sudo chmod -R a+rX,go-w /opt/hermes-team

# sebagai user hermes
mkdir -p ~/.hermes-team && chmod 700 ~/.hermes-team
cp /opt/hermes-team/team.example.yaml    ~/.hermes-team/team.yaml
cp /opt/hermes-team/members.example.yaml ~/.hermes-team/members.yaml
# isi semua ID, URL endpoint LLM, dan anggota
```

Skill tim read-only (opsional, sama seperti v1): repo git di `/srv/hermes-team/skills` milik root,
`git pull` lewat cron root. Isi `team_skills_dir` di team.yaml.

## 3. API key LLM

Key disimpan di `.env` managed: milik root, berlaku untuk semua profile, selalu menang atas `.env`
profile, dan namanya ditolak oleh `/env-set`.

```bash
sudo install -d -m 0755 /etc/hermes
sudo install -m 0640 -o root -g hermes /dev/null /etc/hermes/.env
sudoedit /etc/hermes/.env          # TD_API_KEY=xxxxx   (nama = key_env di team.yaml)
```

Mau kuota per orang di gateway LLM? Set `llm.per_user_keys: true`, jangan taruh `TD_API_KEY` di
`/etc/hermes/.env`, lalu isi per profile: `python3 /opt/hermes-team/hermes_team.py llm-key be1`
(input tersembunyi).

## 4. Provisioning

```bash
cd ~/.hermes-team
python3 /opt/hermes-team/hermes_team.py check          # validasi + peta channel → profile → model
python3 /opt/hermes-team/hermes_team.py apply --dry-run
python3 /opt/hermes-team/hermes_team.py apply          # pertama kali: beberapa menit

python3 /opt/hermes-team/hermes_team.py managed > /tmp/hermes-managed.yaml
sudo install -D -m 0644 -o root -g root /tmp/hermes-managed.yaml /etc/hermes/config.yaml

sudo loginctl enable-linger hermes
hermes gateway install            # sekali
hermes doctor && hermes gateway restart && hermes gateway status
```

`apply` hanya menjalankan `hermes config set` untuk nilai yang berbeda, jadi aman diulang (kedua
kalinya selesai dalam hitungan detik). SOUL.md yang sudah ada tidak ditimpa kecuali `--force-soul`.
Sebelum production, `bash /opt/hermes-team/tests/selftest.sh` menguji semuanya terhadap Hermes di
server ini memakai HOME sementara.

## 5. Tool admin di #hermes-admin

Tulis biasa, Hermes memanggil tool `teamctl`, menjelaskan rencananya, lalu menampilkan tombol
**Approve / Deny**. Tanpa klik admin tidak ada yang berubah.

| Contoh permintaan | Yang terjadi |
|---|---|
| "Tambahkan anggota baru: nama citra, Discord user ID 5000…, model td-simple" | Channel privat `#hermes-citra` dibuat di kategori Hermes, role `@backend` diberikan, profile `be-citra` dibuat dari template (SOUL dipersonalisasi, plugin terpasang, skill yang dibagikan ke "semua" ikut disalin), rute ditambahkan. Lalu ketik `/restart`. |
| "Bagikan skill deploy-checklist milik be1 ke semua anggota" | Skill dipindai; kalau aman, disalin ke `skills/shared/deploy-checklist` tiap penerima dan #hermes-shared. Kalau memuat nilai secret pemilik atau pola kredensial, dibatalkan. |
| "Bagikan skill itu ke budi dan citra saja" | Sama, hanya ke dua orang. |
| "Ganti model be1 ke td-normal" / "semua anggota pakai td-simple" | `members.yaml` + config profile diperbarui. Berlaku di sesi baru (`/new`). |
| "Nonaktifkan citra" | Rute dicabut, data & channel tetap. `/restart`. |
| "Status tim" | Anggota, model per orang, plugin, skill yang dibagikan. |

Padanan CLI (di server, user `hermes`):

```bash
H=/opt/hermes-team/hermes_team.py
python3 $H add-member citra 500000000000000003 --display-name Citra --model td-simple
python3 $H add-member dodi 500000000000000004 --channel-id 600000000000000004   # channel sudah ada
python3 $H set-model be1 td-normal          # atau: all | shared | admin | lobby
python3 $H set-active citra off
python3 $H list-skills be1
python3 $H share-skill deploy-checklist --from be1 --to all      # atau --to budi,citra / --to shared
python3 $H unshare-skill deploy-checklist
python3 $H status
```

Jejak semua aksi (tanpa nilai secret): `~/.hermes-team/state/audit.log`.

## 6. Untuk anggota: model terkunci

- Model ditentukan admin per profile. `/model`, `/moa`, `/reasoning`, `/fast`, `/tools`,
  `/toolsets`, `/config`, `/reload`, `/skills`, `/busy`, `/yolo`, `/restart`, `/update` hanya untuk
  admin (`/whoami` menampilkan level akses).
- Agent tidak bisa mengubah konfigurasinya sendiri: terminal & file berjalan di sandbox Docker
  tanpa akses ke `~/.hermes`.
- Provider, model auxiliary, sandbox, dan batas skala dipin di `/etc/hermes/config.yaml`;
  `hermes config set` menolak mengubahnya.
- Bila model utama error atau kena throttle, Hermes otomatis pindah ke `llm.fallback`
  (td-normal → td-simple).

## 7. Secret pribadi anggota

Di channel pribadimu (atau thread di dalamnya):

- `/env-set` → form pribadi: **Nama** (mis. `GH_PAT`) dan **Nilai**. Balasan hanya terlihat olehmu.
- `/env-list` → daftar nama secret (tanpa nilai). `/env-unset key:<nama>` → hapus.
- Di chat cukup sebut namanya: "clone repo pakai `$GH_PAT`". AI memakai `$GH_PAT` di perintah
  sandbox tanpa pernah melihat nilainya.
- Nama yang dipakai Hermes atau sistem ditolak beserta sarannya. Contoh: `GITHUB_TOKEN` dipakai
  Hermes sebagai kredensial provider Copilot, jadi pakai `GH_PAT`, lalu di sandbox
  `GH_TOKEN=$GH_PAT gh pr list`.
- Nilai multi-baris (private key) simpan sebagai base64 dengan nama berakhiran `_B64`.

Lapisan perlindungan:

1. **Input di luar chat** — modal Discord, balasan ephemeral. Nilai tidak masuk transcript, sesi,
   memory, atau log Hermes.
2. **Tersimpan per profile** — `~/.hermes/profiles/<profile>/.env` mode 0600. Profile lain (dan
   sandbox-nya) tidak bisa membacanya.
3. **Masuk sandbox hanya sebagai env var** lewat `terminal.env_passthrough`. Nama yang dipakai
   Hermes (kredensial provider, gerbang akses Discord, setelan sandbox, `TD_API_KEY`) ditolak.
4. **Output disamarkan** — nilai, base64, url-encode, dan hex-nya diganti `«secret:NAMA»` di output
   terminal, hasil tool, dan jawaban AI.
5. **Lapis terakhir sebelum provider** — setiap request ke LLM disaring lagi, jadi model tidak
   menerima nilainya dari jalur mana pun.
6. **Guard perintah** — `env`, `printenv`, `set`, `export -p`, `/proc/*/environ`, `echo $SECRET`
   diblokir. AI hanya diberi tahu *nama* secret plus aturan untuk tidak mencetaknya.
7. **Chat** — token/password yang terlanjur ditempel di chat disamarkan sebelum sampai ke AI, pesan
   aslinya dihapus, dan pengirim diingatkan memakai `/env-set`.
8. **Skill yang dibagikan dipindai** — ditolak kalau memuat nilai secret pemiliknya.
9. Opsional: **egress allowlist** (`egress/`) — sandbox hanya bisa menghubungi domain yang diizinkan.

Batas yang perlu diketahui tim:

- Penyamaran mencocokkan nilai persis (dan encode umum). Potongan atau transformasi lain (mis.
  10 karakter pertama) bisa lolos; perintah yang *memakai* secret memang mengirimnya ke tujuan itu.
  Karena itu: token dengan scope minimal, dan pertimbangkan egress allowlist.
- Pesan susulan yang dikirim saat bot sedang mengerjakan sesuatu diproses Hermes lewat jalur lain,
  jadi tidak melewati penyaring chat. Nilai yang sudah tersimpan tetap disamarkan sebelum ke model
  (lapis 5), tapi teks mentahnya tersimpan di riwayat sesi di server. Aturan tim: jangan tempel
  secret di chat.
- "Privat" berarti tertutup dari rekan tim, **bukan** dari operator server (root / user `hermes`).

## 8. Konteks & memory tidak campur aduk

- Satu profile per orang: memory (`MEMORY.md`, `USER.md`), skill, dan riwayat sesi sendiri.
- #hermes-shared: tiap orang sesi sendiri per thread (`group_sessions_per_user`). Memory & skill
  bersama hanya ditulis setelah admin menyetujui (`/memory pending`, `/skills pending`); profil
  user dimatikan di sana supaya fakta pribadi tidak masuk.
- Memory bawaan Hermes dibatasi (±2.200 karakter MEMORY + ±1.400 USER), jadi tidak membengkak.
- Topik baru → thread baru (`/thread`) atau `/new`; SOUL anggota menyarankan itu saat topik
  berpindah jauh.

## 9. Skala 20 orang

| Masalah | Mekanisme | Setelan (`team.yaml` → `scale`) |
|---|---|---|
| Riwayat chat membengkak | Kompresi paling lambat di 64k token; output tool lama dipangkas tanpa LLM; sesi yang dibuka lagi setelah 1 jam diringkas dulu | `compression_threshold_tokens`, `proactive_prune_tokens`, `idle_compact_after_seconds` |
| Agent berputar terlalu lama | Maks 60 langkah tool per giliran, `/goal` maks 8 lanjutan, subagent maks 2 | `agent_max_turns`, `goals_max_turns`, `delegation_max_children` |
| Satu orang membanjiri | Token bucket per orang: 4 pesan langsung, lalu 6/menit; kelebihannya ditunda otomatis, ditolak sopan bila harus menunggu > 45 detik | `user_rate_per_minute`, `user_burst` |
| Semua orang bersamaan | Maks 8 sesi aktif; pesan berikutnya antre FIFO dengan pemberitahuan posisi, maks 3 menit | `max_concurrent_sessions`, `queue_max_wait_seconds` |
| Mengirim pesan saat bot sibuk | Masuk antrean sesi, tidak memotong pekerjaan | `busy_input_mode: queue` |
| Throttle / 429 dari provider | Retry + backoff bawaan Hermes, fallback ke td-simple, semua tugas auxiliary (kompresi, review memory, approval, judge) ke td-simple, judul sesi tanpa LLM | `llm.fallback`, `llm.aux_model`, `title_llm` |
| Beban RAM | Browser otomatis & computer use (jalan di host) mati | `allow_browser: false` |

Patokan: `max_concurrent_sessions` ≤ jumlah request paralel yang sanggup dilayani endpoint LLM
(tiap sesi aktif kira-kira satu request berjalan, ditambah tugas auxiliary). Kalau gateway LLM
membatasi per key, tambahkan beberapa key ke credential pool provider (`hermes auth add …`) atau
pakai key per orang (§3). Admin tidak terkena rate limit dan antrean; slash command juga tidak.

Ingin sesi otomatis baru setelah lama diam? Pasang plugin katalog
`hermes plugins install hermes-session-reset-policy` dan atur blok `session_reset`.

## 10. Uji di Discord

- BE1 menulis di `#hermes-be1` → `/status` menyebut profile `be-be1` dan model `td-normal`.
- Budi tidak bisa melihat `#hermes-be1`; `/model` dari Budi ditolak; `/whoami` → user.
- `/env-set` di channel sendiri → form muncul; `/env-list` → nama saja; minta AI "jalankan
  `printenv`" → diblokir; minta "tampilkan GH_PAT" → yang muncul `«secret:GH_PAT»`.
- Tempel `ghp_…` palsu di chat → pesan dihapus, bot mengingatkan.
- Di #hermes-admin: "status tim", lalu tambah anggota uji dan klik Approve → channel baru → `/restart`.
- Non-admin yang menulis di #hermes-admin (kalau izin channel bocor) → dijawab lobby, bukan admin.

## 11. Operasional

| Kebutuhan | Caranya |
|---|---|
| Tambah / nonaktifkan anggota | Chat di #hermes-admin, atau CLI `add-member` / `set-active` → `/restart` |
| Ganti model | Chat atau `set-model`; berlaku di sesi baru |
| Ubah batas skala / sandbox / provider | Edit `team.yaml` → `apply` → `managed` → pasang ulang `/etc/hermes/config.yaml` → `/restart` |
| Update kit | Salin versi baru ke `/opt/hermes-team` → `apply` → `/restart` |
| Update Hermes | `/update` (admin) atau `hermes update`; lalu `bash tests/selftest.sh` |
| Log | `hermes logs -f`, `tail -f ~/.hermes/logs/gateway.log`; cari `team-guard` |
| Audit | `~/.hermes-team/state/audit.log` |
| Hapus profile anggota yang keluar | Nonaktifkan dulu, lalu `hermes profile delete be-<nama>` |

## 12. Batasan

- Perubahan rute/channel baru aktif setelah gateway restart (`/restart` drain sesi aktif dulu).
- `/env-set`, `/env-list`, `/env-unset` adalah guild command: terlihat semua anggota server, tapi
  hanya berfungsi di channel pribadi pemanggil (dan untuk admin di #hermes-shared / #hermes-admin).
- Antrean memakai fungsi internal gateway Hermes (`_running_agent_count`, dll.). Kalau update
  Hermes mengubahnya, antrean nonaktif sendiri dan yang berlaku penolakan bawaan
  `max_concurrent_sessions` — cek log setelah update dan jalankan `selftest.sh`.
- `strict_admin_routes: true` membuat hasil cron profile `team-admin` tidak bisa dikirim ke
  #hermes-admin. `strict_personal_routes: true` berlaku sama untuk channel pribadi.
- Satu bot = satu nama/avatar dan satu menu `/`. Skill pribadi dipanggil dengan bahasa biasa.
- Dari v1: `team.yaml` lama yang memuat `members:` masih terbaca; pindahkan anggota ke
  `members.yaml`. `forward_env` lama masih didukung, tapi ganti ke `/env-set`.

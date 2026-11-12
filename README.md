# Hermes untuk tim backend — kit v3.1

Satu instalasi Hermes, satu bot Discord, satu gateway (mode multiplex), dan **satu profile per
orang**. Tiap channel dirutekan ke profile-nya, jadi memory, skill, SOUL.md, config, model,
secret (`.env`), sesi, dan sandbox Docker tiap orang terpisah.

```
user baru masuk server ──► role @entry (otomatis)
admin memberi @backend  ──► #hermes-<username> privat + profile be-<username> + rute + sambutan @mention
@backend dicabut / keluar ─► dinonaktifkan (profile, memory, secret, isi channel disimpan)

#hermes-be1   (privat: BE1 + bot)    → profile be-be1      model td-normal, secret & sandbox BE1
#hermes-budi  (privat: Budi + bot)   → profile be-budi     model td-simple
#hermes-shared (semua @backend)      → profile team-shared skill & konteks tim, sesi per orang
#hermes-admin  (hanya user admin)    → profile team-admin  + tool admin & kelola server (teamctl)
channel lain / DM                    → diabaikan / ditolak
thread di channel mana pun           → ikut profile channel induknya, sesi terpisah
```

## Yang baru di v3

| Kebutuhan | Cara kit memenuhinya |
|---|---|
| User baru di server langsung dapat role entry | Plugin `team-guard` mendengar event *member join* dan memberi `roles.entry` (setelah lolos membership screening bila server memakainya). |
| Setelah admin memberi @backend: channel, env, profile, sandbox sendiri | Event *role berubah* → plugin memeriksa siapa pemberinya (audit log), lalu menjalankan `hermes_team.py onboard`: channel privat `#hermes-<username>` (hanya dia + bot), profile `be-<username>` dari template (SOUL, memory kosong, `.env` sendiri, sandbox Docker sendiri, plugin, model default), rute gateway. |
| Masukkan dia & bot ke channel, langsung sapa dengan mention | Izin channel hanya untuk orang itu + bot. Bot mengirim pesan sambutan yang me-mention dia (bisa diubah di `team.yaml`); role entry dicabut; admin dikabari di #hermes-admin. |
| Tanpa restart | **Live reload**: plugin menerapkan rute & daftar channel baru ke gateway yang sedang jalan dan memverifikasinya. Gagal (mis. Hermes berubah) → restart halus otomatis saat tidak ada sesi aktif. |
| Hermes bisa mengelola server Discord | 14 tool `discord_*` di #hermes-admin: lihat server/anggota, baca pesan channel non-pribadi, buat/ubah/hapus channel & role, atur izin, beri/cabut role, timeout/kick, kirim pengumuman. Semua dengan tombol konfirmasi dan pagar keamanan. |

Tetap ada dari v2: tool admin (tambah anggota, bagikan skill, model per orang), `/env-set` aman,
model terkunci untuk anggota, penyamaran secret berlapis, dan mekanisme skala 20 orang.

## Isi kit

| File | Fungsi |
|---|---|
| `kit_base.py`, `kit_discord.py` | Dasar (versi, `TeamError`, tokenizer `.env`) dan klien Discord REST ringan; diimpor `hermes_team.py` dan tetap terlihat sebagai `hermes_team.DiscordAPI` dst. |
| `hermes_team.py` | CLI provisioning & operasi (check, apply, managed, add-member, onboard, offboard, set-model, share-skill, …). Juga dipakai plugin dan tool admin. |
| `teamctl_mcp.py` | Server MCP tool admin + kelola server; dipasang otomatis hanya di profile `team-admin`. |
| `plugins/team-guard/` (v3.1.0, modul kecil: `onboarding`, `livereload`, `secrets`, `guard`, `redact`, `ratelimit`, …) | Plugin Hermes: `/env-set`, penyamaran secret, guard perintah, rate limit & antrean, **onboarding otomatis, live reload rute**. Dipasang ke semua profile. |
| `team.example.yaml` | Setelan tim (guild, role, admin, channel, model, sandbox, skala, secret, onboarding, kelola server). |
| `members.example.yaml` | Anggota + model per orang (juga ditulis otomatis oleh onboarding). |
| `kit_doctor.py` | `doctor`: membandingkan members.yaml ↔ profile di disk ↔ rute gateway ↔ channel Discord (`--discord`), hanya membaca. |
| `usage_report.py` | Laporan token & biaya LLM per anggota dari `state.db` tiap profile (read-only). |
| `docker/sandbox/` | Dockerfile image sandbox tim (PHP, Go, Java, Node, Python, klien DB, `gh`). |
| `.github/workflows/ci.yml` | CI: uji unit + uji integrasi terhadap Hermes yang dipin dan `main` (mingguan). |
| `tests/` | `selftest.sh` menguji seluruh kit memakai Python milik Hermes di server ini (HOME sementara, Discord tiruan). Uji integrasi (resolver rute, loader plugin) dilewati bila Hermes tidak bisa di-import. |

## 0. Prasyarat

- Server Linux dengan Docker. User Linux khusus, misal `hermes` (bukan root), anggota grup `docker`.
- Hermes terpasang di user itu (Hermes terbaru hanya mendukung Python 3.14; installer mengurusnya), platform Discord sudah disetel. Catat versi yang kamu uji (`hermes update --check`) dan naikkan hanya setelah CI hijau:
  ```bash
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
  hermes update
  hermes gateway setup        # pilih Discord, tempel token bot
  ```
- `python3` dengan PyYAML (`sudo apt install python3-yaml`).
- Endpoint LLM OpenAI-compatible untuk model `td-normal` / `td-simple` dan API key-nya.

## 1. Discord

1. Satu aplikasi bot di Developer Portal, **Message Content Intent** dan **Server Members Intent**
   aktif (yang kedua wajib untuk event join/role). Link undangan memuat scope `bot` dan
   `applications.commands`.
2. Role: `@entry` (user baru), `@backend` (anggota tim), `@hermes-admin`. Di
   **Server Settings → Roles**, seret role bot **di atas** `@entry` dan `@backend` — Discord hanya
   mengizinkan bot memberi/mencabut role yang posisinya di bawah role bot.
3. Kategori **Hermes** berisi `#hermes-shared` (dilihat `@backend`) dan `#hermes-admin` (hanya
   `@hermes-admin`). Channel pribadi dibuat otomatis di kategori ini. Discord membatasi 50 channel
   per kategori; bila penuh, buat kategori baru dan ganti `category_id` di team.yaml.
4. Izin role bot (tingkat server):

   | Izin | Dipakai untuk |
   |---|---|
   | View Channels, Send Messages, Send Messages in Threads, Read Message History, Embed Links, Attach Files, Add Reactions, Create Public Threads | percakapan biasa |
   | **Manage Roles** | memberi `@entry`, `@backend`; tool role |
   | **Manage Channels** | membuat channel pribadi; tool channel & izin |
   | **View Audit Log** | memastikan `@backend` diberikan oleh admin (`onboarding.verify_actor`) |
   | **Manage Messages**, **Manage Threads** | menghapus pesan berisi secret, thread |
   | Moderate Members, Kick Members | hanya bila memakai tool moderasi (`server_admin.allow_moderation`) |

   Jangan beri bot **Administrator**: izin di atas sudah cukup dan jauh lebih sempit.
5. Developer Mode → salin ID server, kategori, channel, role, dan user.
6. Opsional: kalau server memakai *Membership Screening / Rules*, role entry diberikan setelah user
   menyetujui aturan.

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
# isi semua ID (termasuk roles.entry), URL endpoint LLM, dan anggota yang sudah ada
```

Skill tim read-only (opsional): repo git di `/srv/hermes-team/skills` milik root, `git pull` lewat
cron root. Isi `team_skills_dir` di team.yaml.

Alternatif bawaan Hermes untuk mendistribusikan **profile template** (SOUL, skill, setelan dasar) ke
server lain atau memperbarui anggota sekaligus: *profile distributions* — `hermes profile install
github.com/org/be-template --name be-template` lalu `hermes profile update be-template`. Distribusi tidak
pernah membawa `.env`, memory, atau sesi, jadi aman untuk repo git tim. Pakai itu untuk template; skill
buatan anggota tetap dibagikan lewat `share-skill` (dipindai dulu) atau repo skill tim di atas.

## 3. API key LLM

Key disimpan di `.env` managed: milik root, berlaku untuk semua profile, selalu menang atas `.env`
profile, dan namanya ditolak oleh `/env-set`.

```bash
sudo install -d -m 0755 /etc/hermes
sudo install -m 0640 -o root -g hermes /dev/null /etc/hermes/.env
sudoedit /etc/hermes/.env          # TD_API_KEY=xxxxx   (nama = key_env di team.yaml)
```

Jangan taruh `DISCORD_BOT_TOKEN` di `/etc/hermes/.env` (file itu terbaca semua profile). Token bot
hanya di `~/.hermes/.env` milik profile host.

Mau kuota per orang di gateway LLM? Set `llm.per_user_keys: true`, jangan taruh `TD_API_KEY` di
`/etc/hermes/.env`, lalu isi per profile: `python3 /opt/hermes-team/hermes_team.py llm-key be1`.

## 4. Provisioning

```bash
cd ~/.hermes-team
python3 /opt/hermes-team/hermes_team.py check          # validasi + peta channel → profile → model
python3 /opt/hermes-team/hermes_team.py apply --dry-run
python3 /opt/hermes-team/hermes_team.py apply          # pertama kali: beberapa menit

python3 /opt/hermes-team/hermes_team.py managed > /tmp/hermes-managed.yaml
sudo install -D -m 0644 -o root -g root /tmp/hermes-managed.yaml /etc/hermes/config.yaml

sudo loginctl enable-linger hermes
hermes gateway install            # sekali (systemd: restart otomatis bila keluar dengan kode 75)
hermes doctor && hermes gateway restart && hermes gateway status
```

`apply` hanya menjalankan `hermes config set` untuk nilai yang berbeda, jadi aman diulang. SOUL.md
yang sudah ada tidak ditimpa kecuali `--force-soul`. Sebelum production jalankan
`bash /opt/hermes-team/tests/selftest.sh` (memakai HOME sementara dan Discord tiruan).

**Upgrade dari v2:** salin kit baru ke `/opt/hermes-team`, tambahkan `roles.entry`, blok
`onboarding`, `live_reload`, `restart_fallback`, dan `server_admin` dari `team.example.yaml` ke
team.yaml-mu, jalankan `apply`, lalu `/restart` **sekali** (plugin v3 dan tool baru dimuat saat
start). Setelah itu penambahan anggota tidak butuh restart lagi. SOUL.md yang sudah ada tidak
ditimpa; untuk memakai teks SOUL admin v3 (daftar tool baru), hapus
`~/.hermes/profiles/team-admin/SOUL.md` sebelum `apply`.

## 5. Onboarding otomatis

### Alurnya

1. **User masuk server** → bot memberi `@entry`. Dengan role ini ia hanya melihat channel umum;
   Hermes tidak melayaninya (`DISCORD_ALLOWED_ROLES` hanya `@backend` dan `@hermes-admin`).
2. **Admin memberi `@backend`** (di Discord, atau minta Hermes di #hermes-admin: "beri role
   backend ke @dodi").
3. Plugin membaca audit log Discord: siapa yang memberi role itu? Admin tim (`admins`), pemilik
   server, bot ini, atau `trusted_actor_ids` → lanjut. Orang lain → **ditahan**, admin diberi tahu
   ("⏸️ … ditahan"). Selama ditahan, orang itu tidak dilayani Hermes di channel mana pun (role saja
   tidak cukup, termasuk di #hermes-shared). Admin bisa melanjutkan: "lanjutkan onboarding @dodi".
4. `hermes_team.py onboard` (memegang kunci tim, jadi aman berbarengan dengan tool admin/CLI):
   - channel privat `#hermes-<username>` di kategori Hermes: `@everyone` tidak bisa melihat,
     orangnya + bot bisa. Bot langsung menulis "⏳ sedang disiapkan…";
   - profile `be-<username>` di-clone dari template: SOUL dipersonalisasi, memory kosong,
     `.env` sendiri (scope secret per profile), sandbox Docker sendiri (`/workspace` persisten,
     dibuat saat pertama dipakai), plugin team-guard, model `llm.default_model`, skill yang pernah
     dibagikan ke "semua";
   - anggota ditambahkan ke `members.yaml`, rute & daftar channel gateway diperbarui, roster ditulis.
5. Plugin menerapkan rute baru ke gateway yang **sedang jalan** (live reload), memastikan profile
   baru dilayani multiplexer dan plugin team-guard aktif di sana, lalu memverifikasi bahwa pesan di
   channel itu benar-benar dirutekan ke profile baru.
6. Bot mengirim **pesan sambutan yang me-mention orangnya** (hanya dia yang ter-ping), menghapus
   pesan "sedang disiapkan", mencabut `@entry`, dan mengabari admin
   ("✅ @dodi aktif: #hermes-dodi · profile be-dodi · model td-simple · 41 dtk").

Biasanya selesai dalam 30–90 detik (sebagian besar untuk membuat profile). Beberapa role yang
diberikan bersamaan diproses berurutan.

### Keluar tim

- `@backend` dicabut, atau user keluar/di-kick dari server → anggota **dinonaktifkan**: rute dicabut
  (live), izin melihat channel pribadinya dilepas, ia kembali mendapat `@entry` bila masih di server.
  Profile, memory, secret, dan isi channel **tidak dihapus**.
- `@backend` diberikan lagi → aktif kembali dengan channel dan memory lamanya, disambut
  "Halo lagi …". Kalau channelnya sudah dihapus, dibuatkan yang baru.
- Menghapus permanen: nonaktifkan dulu, lalu `hermes profile delete be-<nama>` dan hapus channel
  manual.

### Susulan saat bot offline

Saat gateway start (dan tiap `onboarding.reconcile_minutes`), plugin mencari user ber-`@backend`
yang belum jadi anggota dan memprosesnya (tetap dengan verifikasi pemberi role, jendela audit log
45 hari), serta memberi `@entry` ke user baru tanpa role. Susulan **tidak pernah** menonaktifkan
anggota; kalau anggota aktif kehilangan `@backend` saat bot offline, admin hanya dikabari.

### Mengubah pesan sambutan

Di `team.yaml` → `onboarding.welcome_message` / `welcome_back_message`. Placeholder: `{mention}`
`{display_name}` `{name}` `{team_name}` `{model}` `{channel}` `{shared_channel}` `{admin_channel}`
`{profile}`. Pesan hanya bisa me-ping orang yang disambut (bukan @everyone/role).

### Nama

Nama anggota diambil dari username Discord (`dodi.pratama` → `dodi-pratama`, huruf kecil ASCII,
unik, bukan nama sistem seperti `admin`/`shared`). Mau nama lain? Pakai tool
`onboard_member`/`add_member` dengan `name`, sebelum memberi role.

### Live reload & restart

Hermes membaca rute dan daftar channel saat start. Plugin menulis ulang tepat objek yang dibaca per
pesan (`GatewayRunner.config.profile_routes` dan `PlatformConfig.extra` adapter Discord) lalu
**memverifikasi** hasilnya dengan resolver rute Hermes sendiri. Profile baru dilayani multiplexer
tanpa restart (Hermes memindai `profiles/` saat profile dibuat). Kalau verifikasi gagal (mis.
update Hermes mengubah internal itu): `restart_fallback: idle` → gateway restart halus begitu tidak
ada sesi aktif (maks ±10 menit; supervisor systemd menyalakannya lagi), `manual` → admin diminta
`/restart`. Kalau verifikasi sudah gagal sesaat setelah gateway start, restart tidak akan menolong:
kit tidak me-restart lagi (mencegah restart berulang), mengabari admin, dan beralih ke `/restart`
manual. Sambutan baru dikirim setelah rute benar-benar berlaku, jadi orangnya tidak pernah
menulis ke channel yang belum dijawab bot. Perubahan yang memang butuh restart (role yang dilayani,
gating slash command, batas sesi, MCP admin) tetap ditandai "perlu /restart".

## 6. Tool admin di #hermes-admin

Tulis biasa, Hermes memanggil tool `teamctl`, menjelaskan rencananya, lalu menampilkan tombol
**Approve / Deny**. Tanpa klik admin tidak ada yang berubah. #hermes-admin hanya melayani user ID di
`admins`.

### Tim

| Contoh permintaan | Yang terjadi |
|---|---|
| "Status onboarding" | Onboarding yang ditahan/gagal, user ber-`@backend` yang belum anggota, jumlah `@entry`, status live reload. |
| "Lanjutkan onboarding @dodi" / "jadikan @citra anggota, model td-normal" | `onboard_member`: channel + profile + rute (+ `@backend` bila belum), lalu sambutan otomatis. |
| "Tambahkan anggota: nama be3, user ID 5000…, pakai channel 6000…" | `add_member` dengan nama/channel/model tertentu. |
| "Ganti model be1 ke td-normal" / "semua anggota pakai td-simple" | `members.yaml` + config profile. Berlaku di sesi baru (`/new`). |
| "Nonaktifkan citra" | Rute dicabut (live), akses channel dilepas, data tetap. |
| "Bagikan skill deploy-checklist milik be1 ke semua anggota" | Dipindai dulu (secret pemilik, pola kredensial, pemindai Hermes, symlink), lalu disalin ke `skills/shared/`. |

### Mengelola server Discord

| Contoh permintaan | Tool |
|---|---|
| "Ringkas struktur server" / "siapa saja yang punya role designer?" | `discord_overview`, `discord_list_members` |
| "Apa diskusi terakhir di #general?" | `discord_read_messages` (channel pribadi anggota selalu ditolak; token di pesan disamarkan) |
| "Buat channel #incident privat untuk @backend di kategori Hermes" | `discord_create_channel` |
| "Ganti topik #incident", "pindahkan #random ke kategori Arsip", "slowmode 10 detik" | `discord_edit_channel` |
| "Hapus #random" | `discord_delete_channel` |
| "Izinkan @designer pin pesan di #general" / "jadikan #rapat hanya bisa dilihat @backend" | `discord_set_channel_permissions` (target `@everyone` untuk semua orang; izin digabung, bukan ditimpa) |
| "Buat role @oncall warna merah, bisa di-mention" | `discord_create_role` / `discord_edit_role` / `discord_delete_role` |
| "Beri role backend ke @eka" / "cabut role backend dari @budi" | `discord_assign_role` / `discord_remove_role` (memicu onboarding / penonaktifan) |
| "Timeout @spammer 30 menit" / "kick @x" | `discord_moderate_member` |
| "Umumkan di #general: deploy ditunda, ping @be1" | `discord_send_message` (tanpa @everyone/@here/role ping) |

Pagar yang tidak bisa dibujuk lewat chat (ditolak sebelum tombol konfirmasi muncul):

- Channel pribadi anggota (dan thread di dalamnya) tidak bisa dibaca, diubah (nama, topik, posisi),
  dikirimi pesan, diubah izinnya, atau dihapus. Channel bersama/admin dan kategori tim tidak bisa
  dihapus, dipindah, atau diganti namanya (topik dan slowmode boleh), dan overwrite-nya tidak bisa
  dihapus; overwrite milik bot sendiri tidak bisa dihapus/ditolak. Role tim (`@backend`,
  `@hermes-admin`, `@entry`) tidak bisa dihapus dan izinnya tidak bisa diubah.
- Izin tinggi (Administrator, Manage Server/Roles/Channels/Webhooks/Messages/Threads, ban/kick/
  moderate, mention everyone, …) tidak bisa diberikan — lewat role, izin channel, maupun memberi
  role yang memuatnya, termasuk role yang membuka #hermes-admin atau channel pribadi lewat overwrite.
  Role berizin tinggi juga tidak bisa dicabut, dihapus, atau diubah izinnya lewat Hermes, dan role
  tidak bisa dicabut dari admin/pemilik server. Role `@hermes-admin` hanya diberikan manual (dan user
  ID-nya di `admins`). Ubah dengan `server_admin.allow_elevated_permissions: true` bila perlu.
- Admin tim, pemilik server, semua bot, dan pemegang izin tinggi tidak bisa di-timeout/kick. Ban selalu manual.
- Pengguna harus disebut dengan ID, mention, atau username persis (nama tampilan/nickname tidak dicocokkan
  lagi, supaya tidak salah orang). Konfirmasi selalu memuat ID dan teks lengkap yang akan dikirim.
- Setelah klik Approve, keadaan server diperiksa ulang; bila berubah dari yang disetujui, aksi
  dibatalkan ("keadaan berubah, ulangi"). Membatalkan permintaan di klien juga membatalkan konfirmasinya.
- Isi pesan yang dibaca dari Discord dibungkus penanda `<<<DATA-DISCORD …>>>` dengan nonce acak dan
  tiap baris diberi awalan, sehingga satu pesan tidak bisa memalsukan pesan lain; semuanya data,
  bukan instruksi. Argumen tool diperiksa tipenya secara ketat (ID harus string).
- Tidak ada lagi cara mematikan konfirmasi (`TEAMCTL_REQUIRE_CONSENT` dihapus; variabelnya diabaikan
  dengan peringatan). `TEAMCTL_MAX_MESSAGE_CHARS` (opsional) menurunkan batas 2000 karakter pesan.
- `add_member` kini butuh Discord terjangkau dan user ada di server walau `channel_id` diberikan;
  channel itu harus channel teks di server ini, bukan channel tim/anggota lain.
- Audit log mencatat juga aksi yang gagal/ditolak: sufiks `-blocked`, `-refused`, `-stale`, dan
  kolom `outcome: ok|failed`.

Kenapa bukan toolset bawaan `discord` / `discord_admin`? Di mode multiplex, profile `team-admin`
tidak bisa melihat token bot (token ada di scope profile host), dan toolset itu tidak punya
konfirmasi maupun pagar per tim. Keduanya tetap dimatikan untuk semua profile; tool `discord_*`
berjalan di proses MCP terpisah yang membaca token dari `~/.hermes/.env` host.

Padanan CLI (di server, user `hermes`):

```bash
H=/opt/hermes-team/hermes_team.py
python3 $H onboard --user-id 500000000000000004 --username dodi     # sama seperti saat role diberikan
python3 $H offboard --user-id 500000000000000004
python3 $H add-member citra 500000000000000003 --display-name Citra --model td-simple
python3 $H set-model be1 td-normal          # atau: all | shared | admin | lobby
python3 $H set-active citra off
python3 $H share-skill deploy-checklist --from be1 --to all
python3 $H status
```

Jejak semua aksi (tanpa nilai secret): `~/.hermes-team/state/audit.log` dan audit log Discord
(alasan "Hermes (teamctl): …"). Status onboarding plugin: `~/.hermes-team/state/onboarding.json`.

## 7. Untuk anggota: model terkunci

- Model ditentukan admin per profile. `/model`, `/moa`, `/reasoning`, `/fast`, `/tools`,
  `/toolsets`, `/config`, `/reload`, `/skills`, `/busy`, `/yolo`, `/restart`, `/update` hanya untuk
  admin (`/whoami` menampilkan level akses).
- Agent tidak bisa mengubah konfigurasinya sendiri: terminal & file berjalan di sandbox Docker
  tanpa akses ke `~/.hermes`.
- Provider, model auxiliary, sandbox, dan batas skala dipin di `/etc/hermes/config.yaml`;
  `hermes config set` menolak mengubahnya.
- Bila model utama error atau kena throttle, Hermes otomatis pindah ke `llm.fallback`.

## 8. Secret pribadi anggota

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
4. **Output disamarkan** — nilai dan bentuk turunannya (base64, url-encode, hex, dibalik, huruf
   besar/kecil) diganti `«secret:NAMA»` di output terminal, hasil tool, dan jawaban AI.
5. **Saringan terakhir sebelum provider** — setiap request agent ke model disaring lagi: nilai
   tersimpan di seluruh payload, plus token vendor yang jelas (`ghp_…`, `sk-…`, `AKIA…`, JWT, private
   key) di pesan user. Input kompresi konteks disaring Hermes memakai nilai yang didaftarkan plugin.
6. **Guard perintah (best-effort)** — dump environment yang umum diblokir: `env`/`printenv`,
   `set`, `export -p`, `declare -x`, `/proc/*/environ`, `echo $SECRET`, `print(os.environ)`,
   `console.log(process.env)`, dll.
7. **Chat** — token/password yang ditempel di chat disamarkan sebelum sampai ke AI; token yang jelas
   dihapus dari channel dan pengirimnya diingatkan memakai `/env-set`.
8. **Skill yang dibagikan dipindai** dari salinan beku — ditolak kalau memuat nilai secret pemiliknya,
   pola kredensial, atau symlink.
9. Opsional: **egress proxy bawaan Hermes** (`hermes egress install/setup/start`, iron-proxy) — sandbox hanya memegang token proxy, kunci provider asli tetap di host, dan lalu lintas keluar dibatasi ke host yang diizinkan (`proxy.extra_allowed_hosts`). Gantikan `sandbox.network/http_proxy` buatan sendiri bila memakainya. Alternatif keras: `terminal.docker_network: false` (tanpa jaringan sama sekali).

Batas yang perlu diketahui tim:

- Penyamaran mencocokkan nilai persis dan bentuk turunan di atas. Potongan atau transformasi lain
  bisa lolos, dan guard perintah bisa diakali; perintah yang *memakai* secret memang mengirimnya ke
  tujuan itu. Karena itu: token dengan scope minimal, dan pertimbangkan egress allowlist.
- Pesan yang dikirim saat bot sedang sibuk masuk antrean Hermes tanpa lewat hook dispatch. Token yang
  jelas tetap dihapus dari channel dan disamarkan sebelum ke model, tapi teks mentahnya tersimpan
  di riwayat sesi di server. Aturan tim: jangan tempel secret di chat.
- "Privat" berarti tertutup dari rekan tim, **bukan** dari operator server (root / user `hermes`)
  maupun admin Discord yang punya izin Administrator.

## 9. Konteks & memory tidak campur aduk

- Satu profile per orang: memory (`MEMORY.md`, `USER.md`), skill, dan riwayat sesi sendiri. Profile
  baru dari onboarding selalu mulai dengan memory kosong.
- #hermes-shared: tiap orang sesi sendiri per thread (`group_sessions_per_user`). Memory & skill
  bersama hanya ditulis setelah admin menyetujui (`/memory pending`, `/skills pending`).
- Memory bawaan Hermes dibatasi (±2.200 karakter MEMORY + ±1.400 USER), jadi tidak membengkak.
- Topik baru → thread baru (`/thread`) atau `/new`.

## 10. Skala 20 orang

| Masalah | Mekanisme | Setelan (`team.yaml` → `scale`) |
|---|---|---|
| Riwayat chat membengkak | Kompresi paling lambat di 64k token; output tool lama dipangkas tanpa LLM; sesi yang dibuka lagi setelah 1 jam diringkas dulu | `compression_threshold_tokens`, `proactive_prune_tokens`, `idle_compact_after_seconds` |
| Agent berputar terlalu lama | Maks 60 langkah tool per giliran, `/goal` maks 8 lanjutan, subagent maks 2 | `agent_max_turns`, `goals_max_turns`, `delegation_max_children` |
| Satu orang membanjiri | Token bucket per orang: 4 pesan langsung, lalu 6/menit; kelebihan ditunda, ditolak sopan bila harus menunggu > 45 detik | `user_rate_per_minute`, `user_burst` |
| Semua orang bersamaan | Maks 8 sesi anggota aktif; berikutnya antre FIFO dengan posisi, maks 3 menit. 2 slot dicadangkan untuk admin | `max_concurrent_sessions`, `admin_reserved_slots`, `queue_max_wait_seconds` |
| Mengirim pesan saat bot sibuk | Masuk antrean sesi, tidak memotong pekerjaan | `busy_input_mode: queue` |
| Throttle / 429 dari provider | Retry + backoff bawaan Hermes, fallback model, tugas auxiliary ke model murah, judul sesi tanpa LLM | `llm.fallback`, `llm.aux_model`, `title_llm` |
| Banyak orang di-onboard sekaligus | Diproses berurutan di bawah kunci tim; tidak ada restart gateway | `onboarding.*` |
| Beban RAM | Browser otomatis & computer use (jalan di host) mati | `allow_browser: false` |

Patokan: `max_concurrent_sessions` ≤ jumlah request paralel yang sanggup dilayani endpoint LLM.

## 11. Uji di Discord

- Akun uji masuk server → dapat `@entry`, tidak dilayani bot.
- Admin memberi `@backend` → dalam ±1 menit muncul `#hermes-<username>` dengan sambutan yang
  me-mention akun uji; `@entry` hilang; #hermes-admin menampilkan ✅. Tanpa `/restart`.
- Akun uji menulis di channelnya (tanpa mention) → `/status` menyebut profile `be-<username>`.
- `/env-set` → form; `/env-list` → nama saja; "jalankan `printenv`" → diblokir.
- Non-admin dengan izin Manage Roles memberi `@backend` → ditahan, admin dikabari; "lanjutkan
  onboarding @x" → berjalan.
- Cabut `@backend` → akun uji kehilangan akses channelnya, dapat `@entry` lagi; beri lagi → "Halo lagi".
- Di #hermes-admin: "buat channel #uji privat untuk @backend", lalu "hapus #uji" → dua kali tombol
  konfirmasi. "Baca #hermes-<username>" → ditolak.

## 12. Operasional

| Kebutuhan | Caranya |
|---|---|
| Tambah anggota | Beri role `@backend` (atau minta di #hermes-admin) |
| Nonaktifkan anggota | Cabut `@backend`, atau "nonaktifkan <nama>" |
| Onboarding ditahan / gagal | "status onboarding" → "lanjutkan onboarding @x"; detail di audit.log |
| Ganti model | Chat atau `set-model`; berlaku di sesi baru |
| Ubah batas skala / sandbox / provider | Edit `team.yaml` → `apply` → `managed` → pasang ulang `/etc/hermes/config.yaml` → `/restart` |
| Update kit | Salin versi baru ke `/opt/hermes-team` → `apply` → `/restart` |
| Update Hermes | `/update` (admin) atau `hermes update`; lalu `bash tests/selftest.sh` (memeriksa live reload) |
| Periksa konsistensi | `python3 /opt/hermes-team/kit_doctor.py [--discord]` (members ↔ profile ↔ rute ↔ channel) |
| Pemakaian & biaya per orang | `python3 /opt/hermes-team/usage_report.py --days 7 --members-file ~/.hermes-team/members.yaml` |
| Log | `hermes logs -f`; cari `team-guard` |
| Audit | `~/.hermes-team/state/audit.log`, `onboarding.json` |

## 12b. Perubahan perilaku v3.1 (hasil audit)

Yang perlu diketahui admin saat naik dari v3.0:

- **Profile baru mulai dengan `.env` kosong.** Hasil clone tidak lagi mewarisi isi `~/.hermes/.env`
  (Hermes hanya membuang kredensial channel). Key LLM bersama taruh di `/etc/hermes/.env` atau pakai `llm-key`.
- **Nama anggota baru menghindari profile sisa.** Folder profile yang sudah ada (mis. anggota lama yang
  dihapus dari members.yaml) dianggap terpakai: nama turunan diberi akhiran `-2`, nama eksplisit ditolak.
- **Onboarding transaksional.** Semua divalidasi sebelum channel dibuat; channel yang cocok dipakai ulang;
  bila gagal di tengah, entri members.yaml, folder profile baru, dan channel baru dibatalkan. `roster.json`
  ditulis terakhir. Jalur "sudah anggota" memperbaiki diri (profile hilang / rute usang → apply).
- **Channel pribadi ditegakkan, bukan diasumsikan:** saat dipakai ulang atau lewat `--channel-id`, channel
  harus teks di server ini, bukan milik orang lain/tim, dan seluruh izin privat ditegaskan ulang
  (overwrite pihak lain dibuang dan dicatat).
- **`share-skill` gagal tertutup.** Hanya verdict `safe` dari pemindai Hermes yang lolos; pemindai hilang/error
  menolak kecuali `--allow-unscanned` (di chat: `allow_unscanned`, tetap menolak verdict berbahaya). Nilai `.env`
  pemilik sepanjang ≥ `secrets.min_length` memblokir pembagian (non-secret bisa false-positive). Kiriman bernama
  sama dari pemilik lain butuh `--overwrite`.
- **`llm-key`** butuh TTY atau `--stdin`, dan memegang kunci tim.
- **Key rahasia di `llm.providers.*`** (`api_key`, `token`, …) ditolak: blok itu lewat argv `hermes config set`.
- **Parkir resmi.** Anggota nonaktif diparkir (`gateway.parked`): adapter dan cron job profile-nya berhenti,
  data utuh. `apply` menyelaraskan penanda dengan members.yaml (aktif → dilepas).
- **Lobby hanya-teks** kini menutup semua toolset Hermes saat ini; CI gagal bila Hermes menambah toolset baru.
- Discord API: `Retry-After` dihormati, 5xx dicoba ulang (GET/PUT/DELETE saja), timeout menjadi `TeamError`.
- `apply --only` dengan nama tak dikenal/nonaktif sekarang error; kode keluar 3 = sebagian gagal, 2 = galat tak terduga
  (`HERMES_TEAM_DEBUG=1` menampilkan traceback). Event `--events` `channel` bisa membawa `reused: true`.
- Plugin 3.1.0 butuh `plugins.isolation: in_process` (bawaan Hermes). Satu salinan modul dimuat per profile,
  jadi status lintas-profile disimpan di entri `sys.modules` sintetis (`shared.py`).
- `hold_stream` (opsional, default mati): menahan jawaban AI sampai disamarkan; tanpa itu balasan yang
  mengandung secret bisa terlihat sesaat saat streaming.

> **Belum pernah dijalankan terhadap Hermes/Discord sungguhan.** 938 uji lulus memakai Discord tiruan dan stub
> API plugin; 14 uji integrasi (resolver rute, loader plugin, daftar toolset/config Hermes) hanya jalan di CI
> atau `selftest.sh` di server yang punya Hermes. Jalankan itu dulu, lalu tes manual §11 di server uji.

## 13. Batasan

- Live reload memakai struktur internal gateway Hermes (dicek dengan resolver rute Hermes sendiri
  setiap kali). Bila update Hermes mengubahnya, kit jatuh ke restart halus (atau `/restart` manual)
  — jalankan `selftest.sh` setelah update Hermes.
- Verifikasi pemberi role butuh izin **View Audit Log**. Tanpa itu semua onboarding ditahan sampai
  admin menyetujui (atau set `verify_actor: false`, artinya siapa pun yang bisa memberi `@backend`
  di Discord dipercaya).
- Bot hanya bisa memberi/mencabut role di bawah role-nya sendiri, dan hanya membuat channel bila
  punya Manage Channels di kategori itu.
- Sandbox Docker anggota baru dibuat saat pertama kali ia memakai terminal/file (beberapa detik).
- `/env-set`, `/env-list`, `/env-unset` adalah guild command: terlihat semua anggota server, tapi
  hanya berfungsi di channel pribadi pemanggil (dan untuk admin di #hermes-shared / #hermes-admin).
- Antrean memakai fungsi internal gateway Hermes (`_running_agent_count`, dll.). Kalau update
  Hermes mengubahnya, antrean nonaktif sendiri dan yang berlaku penolakan bawaan.
- `strict_admin_routes: true` membuat hasil cron profile `team-admin` tidak bisa dikirim ke
  #hermes-admin. `strict_personal_routes: true` berlaku sama untuk channel pribadi.
- Jangan jalankan gateway dengan log level DEBUG untuk `discord.py`: pada level itu discord.py
  mencatat payload mentah dari Discord, termasuk isi form `/env-set`.
- Satu bot = satu nama/avatar dan satu menu `/`. Skill pribadi dipanggil dengan bahasa biasa.

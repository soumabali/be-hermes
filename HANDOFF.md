# HANDOFF — Hermes Team Kit (Discord, tim backend)

Berkas ini untuk melanjutkan pekerjaan di Claude lain. Tempel isinya di awal chat baru dan
lampirkan folder `v3/` (dan `arsip-versi-lama/` bila perlu).

## 1. Tujuan proyek
Satu instalasi **Hermes Agent** (Nous Research) untuk tim backend (~20 orang) di Discord:
- 1 bot, 1 gateway (mode multiplex), **1 profile per anggota**; channel privat tiap orang
  dirutekan ke profile-nya (`gateway.profile_routes`).
- Channel bersama `#hermes-shared` dan channel admin `#hermes-admin`.
- Sandbox terpisah per orang (`terminal.backend: docker`; profile saja BUKAN sandbox).

Riwayat versi:
- **v1**: provisioning dasar (`hermes-team.py`).
- **v2**: admin bisa tambah user, bagikan skill, set model per user (mis. BE1 `td-normal`,
  lainnya `td-simple`); anggota set `.env` aman lewat perintah Discord; AI tidak boleh
  mengambil/membagikan secret; user tidak bisa ganti model; batas konteks/memori dan throttling
  untuk 20 anggota.
- **v3** (terbaru): saat user baru masuk server dapat role `@entry`; begitu diberi `@backend`
  otomatis dibuat channel pribadi, profile, scope env, sandbox, user + bot dimasukkan ke channel,
  bot mengirim pesan pembuka dengan mention. Hermes juga bisa mengelola server Discord
  (14 tool `discord_*`) dengan guardrail.

## 2. Isi folder
```
v3/
  hermes_team.py          # CLI provisioning (~2397 baris, KIT_VERSION 3.0.0)
  teamctl_mcp.py          # server MCP admin, 25 tool
  team.example.yaml       # konfigurasi (roles.entry, onboarding, live_reload, server_admin, ...)
  members.example.yaml
  README.md               # dokumentasi lengkap (Indonesia)
  tests/fake_discord.py   # Discord REST tiruan untuk uji
arsip-versi-lama/v1, v2   # versi lama (referensi)
```

## 3. Status pembangunan ulang (diperbarui)
Plugin `plugins/team-guard` (v3.1.0) dan seluruh uji **sudah dibangun ulang** terhadap API plugin Hermes
terbaru (adapter Discord kini plugin platform di `plugins/platforms/discord/`). Lihat README §12b untuk
perubahan perilaku. Uji: `python -m pytest tests -q` (938 lulus, 14 integrasi dilewati tanpa Hermes).
Belum diuji: Hermes sungguhan, Discord sungguhan, Docker sungguhan.

## 4. Fakta teknis kunci
- Profile = `HERMES_HOME` terpisah (`~/.hermes/profiles/<name>`).
- Rute multiplex: `gateway.multiplex_profiles` + `gateway.profile_routes`; spesifisitas
  `user_id` 16 > `thread_id` 8 > `chat_id` 4 > `guild_id` 2.
- Control socket: `rescan-profiles`, `serve-profile`, `reload-plugins`; watcher rescan tiap 30 dtk.
- Intent Members aktif hanya bila ada allowlist role/username (`DISCORD_ALLOWED_ROLES`).
- Toolset `discord`/`discord_admin` bawaan Hermes tidak bisa dipakai di multiplex untuk profile
  admin (token ada di scope host) — makanya kit punya tool `discord_*` sendiri di `teamctl_mcp.py`.
- Restart: exit code 75 di bawah systemd (`RestartForceExitStatus=75`).
- Scope terkelola: `/etc/hermes/config.yaml`; **token bot jangan** di `/etc/hermes/.env`,
  taruh di `~/.hermes/.env` (`DISCORD_BOT_TOKEN`).
- Hermes terbaru mendukung Python 3.14 saja untuk instalasi baru (3.11–3.13 hanya agar update lama bisa lewat).
- Uji: `import discord` harus sebelum `import gateway.run` (yang menggeser `sys.path`).
- Guardrail server admin: channel pribadi tak tersentuh, role/channel tim terlindungi, izin
  elevated diblokir, tidak boleh moderasi admin/owner/bot, ban manual; hasil baca ditandai
  sebagai data dan token dimasking.

## 5. Status
- Teruji (tiruan Discord + resolver rute Hermes asli): 375 uji lulus, saat plugin & tests masih ada.
- **Belum teruji**: server Discord sungguhan, pembuatan sandbox Docker sungguhan, egress proxy.

## 6. Langkah setup bot Discord (sudah dipandu pada chat ini)
Developer Portal → New Application → tab **Bot**:
- Public Bot: **mati** (bila error "Private application cannot have a default authorization
  link": tab *Installation* → Install Link = **None**, lalu simpan; atau matikan User Install).
- Presence Intent: mati. **Server Members Intent: ON. Message Content Intent: ON.**
- Reset Token → simpan ke `~/.hermes/.env` sebagai `DISCORD_BOT_TOKEN`. Jangan tempel token di chat.
- OAuth2 → URL Generator: scope `bot` + `applications.commands`; izin: View Channels, Manage
  Channels, Manage Roles, View Audit Log, Send Messages, Send Messages in Threads, Create Public
  Threads, Manage Messages, Manage Threads, Embed Links, Attach Files, Read Message History,
  Add Reactions, Use Slash Commands. **Tanpa Administrator.** Redirects/Client Secret tidak dipakai.
- Undang bot, lalu di Server Settings → Roles seret role bot **di atas** `entry` dan `backend`.
- Aktifkan Developer Mode, salin ID server/role/user ke `team.yaml`.
- Jalankan: `hermes gateway setup` → `hermes_team.py check team.yaml` → `apply` → `hermes gateway run`.
- Tes: beri role `@entry`, lalu `@backend`; channel pribadi harus terbentuk dan bot menyapa.

Posisi user terakhir: sudah membuat aplikasi "TanuKode", sedang di halaman Bot dan OAuth2 URL
Generator; belum selesai mengundang bot dan menjalankan gateway.

## 7. Saran langkah berikutnya untuk Claude baru
1. Baca `v3/README.md` dan `v3/team.example.yaml`.
2. Bangun ulang `plugins/team-guard` dan `tests/` sesuai bagian 3, jalankan hingga hijau.
3. Bantu user menyelesaikan setup bot (bagian 6) dan uji di server nyata.

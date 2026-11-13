# Onboarding otomatis

## Alurnya

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

## Keluar tim

- `@backend` dicabut, atau user keluar/di-kick dari server → anggota **dinonaktifkan**: rute dicabut
  (live), izin melihat channel pribadinya dilepas, profile-nya diparkir (`gateway.parked`, sehingga cron
  job-nya berhenti), dan ia kembali mendapat `@entry` bila masih di server.
  Profile, memory, secret, dan isi channel **tidak dihapus**.
- `@backend` diberikan lagi → aktif kembali dengan channel dan memory lamanya, disambut
  "Halo lagi …". Kalau channelnya sudah dihapus, dibuatkan yang baru.
- Menghapus permanen: nonaktifkan dulu, lalu `hermes profile delete be-<nama>` dan hapus channel
  manual.

## Susulan saat bot offline

Saat gateway start (dan tiap `onboarding.reconcile_minutes`), plugin mencari user ber-`@backend`
yang belum jadi anggota dan memprosesnya (tetap dengan verifikasi pemberi role, jendela audit log
45 hari), serta memberi `@entry` ke user baru tanpa role. Susulan **tidak pernah** menonaktifkan
anggota; kalau anggota aktif kehilangan `@backend` saat bot offline, admin hanya dikabari.

## Mengubah pesan sambutan

Di `team.yaml` → `onboarding.welcome_message` / `welcome_back_message`. Placeholder: `{mention}`
`{display_name}` `{name}` `{team_name}` `{model}` `{channel}` `{shared_channel}` `{admin_channel}`
`{profile}`. Pesan hanya bisa me-ping orang yang disambut (bukan @everyone/role).

## Nama

Nama anggota diambil dari username Discord (`dodi.pratama` → `dodi-pratama`, huruf kecil ASCII,
unik, bukan nama sistem seperti `admin`/`shared`). Mau nama lain? Pakai tool
`onboard_member`/`add_member` dengan `name`, sebelum memberi role.

## Live reload & restart

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

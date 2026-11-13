# HANDOFF — Hermes Team Kit

Berkas ini dulu dipakai untuk memindahkan pekerjaan antar sesi. Isinya sudah digantikan oleh:

- `README.md` — gambaran dan instalasi satu perintah (`sudo bash setup.sh`); rinciannya di `docs/` (INSTALL, ADMIN, ANGGOTA, SKALA, PEMAKAIAN, OPERASI, BATASAN)
- `CHANGELOG.md` — perubahan per versi
- `docs/RENCANA-SKALA.md` — rencana skala dan status tiap butir

Fakta teknis tentang Hermes yang masih berguna saat mengembangkan kit:

- Profile = `HERMES_HOME` terpisah (`~/.hermes/profiles/<nama>`); multiplex adalah bawaan Hermes.
- Rute: `gateway.profile_routes`, spesifisitas `user_id` 16 > `thread_id` 8 > `chat_id` 4 > `guild_id` 2.
- Control socket: `rescan-profiles`, `serve-profile`, `unserve-profile`, `reload-plugins`; rescan tiap 30 dtk.
- Adapter Discord adalah plugin platform (`plugins/platforms/discord/`); intent Members aktif hanya bila ada
  allowlist role/username.
- Restart: kode keluar 75 di bawah systemd.
- Token bot hanya di `~/.hermes/.env`, jangan di `/etc/hermes/.env`.
- Hermes mendukung Python 3.14 untuk instalasi baru; uji: `import discord` harus sebelum `import gateway.run`.

Status pembuktian: lihat catatan di awal README.

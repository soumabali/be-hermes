# Tool admin di #hermes-admin

Tulis biasa, Hermes memanggil tool `teamctl`, menjelaskan rencananya, lalu menampilkan tombol
**Approve / Deny**. Tanpa klik admin tidak ada yang berubah. #hermes-admin hanya melayani user ID di
`admins`.

## Tim

| Contoh permintaan | Yang terjadi |
|---|---|
| "Status onboarding" | Onboarding yang ditahan/gagal, user ber-`@backend` yang belum anggota, jumlah `@entry`, status live reload. |
| "Lanjutkan onboarding @dodi" / "jadikan @citra anggota, model td-normal" | `onboard_member`: channel + profile + rute (+ `@backend` bila belum), lalu sambutan otomatis. |
| "Tambahkan anggota: nama be3, user ID 5000…, pakai channel 6000…" | `add_member` dengan nama/channel/model tertentu. |
| "Ganti model be1 ke td-normal" / "semua anggota pakai td-simple" | `members.yaml` + config profile. Berlaku di sesi baru (`/new`). |
| "Nonaktifkan citra" | Rute dicabut (live), akses channel dilepas, profile diparkir, data tetap. |
| "Bagaimana beban sekarang?" | `team_status`: sesi aktif, antrean, panggilan LLM, 429, anggaran hari ini ([Skala](SKALA.md)). |
| "Tambah kuota budi 2 juta token" | `set_usage_budget_extra`: jatah tambahan hari ini, dengan konfirmasi. |
| "Pemakaian token minggu lalu" / "pemakaian budi bulan ini" | `usage_report`: request, token, sesi, biaya per orang. Hanya-baca, tanpa tombol konfirmasi ([Pemakaian](PEMAKAIAN.md)). |
| "Bagikan skill deploy-checklist milik be1 ke semua anggota" | Dipindai dulu (secret pemilik, pola kredensial, pemindai Hermes, symlink), lalu disalin ke `skills/shared/`. |

## Mengelola server Discord

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
  dikirimi pesan, diubah izinnya, atau dihapus. "Channel pribadi" mencakup channel anggota aktif maupun
  nonaktif, channel anggota yang sudah dihapus (dicatat di `state/removed_channels.json` oleh
  `remove-member`, jadi tetap terlindungi walau channelnya tidak ikut dihapus), dan channel apa pun di
  kategori tim yang berawalan `channel_prefix`. Thread di channel tim tidak bisa diubah/dihapus, dan
  channel laporan pemakaian diperlakukan sebagai channel tim. Channel bersama/admin dan kategori tim tidak bisa
  dihapus, dipindah, atau diganti namanya (topik dan slowmode boleh), dan overwrite-nya tidak bisa
  dihapus; overwrite milik bot sendiri tidak bisa dihapus/ditolak. Role tim (`@backend`,
  `@hermes-admin`, `@entry`) tidak bisa dihapus dan izinnya tidak bisa diubah.
- Izin tinggi (Administrator, Manage Server/Roles/Channels/Webhooks/Messages/Threads, ban/kick/
  moderate, mention everyone, …) tidak bisa diberikan — lewat role, izin channel, maupun memberi
  role yang memuatnya, termasuk role yang membuka #hermes-admin atau channel pribadi lewat overwrite.
  Role berizin tinggi juga tidak bisa dicabut, dihapus, atau diubah izinnya lewat Hermes, dan role
  tidak bisa dicabut dari admin/pemilik server. Role `@hermes-admin` hanya diberikan manual (dan user
  ID-nya di `admins`). Ubah dengan `server_admin.allow_elevated_permissions: true` bila perlu.
- Tiga izin tambahan ikut diblokir untuk diberikan: `create_instant_invite`, `create_events`,
  `use_external_apps`.
- Admin tim, pemilik server, semua bot, dan pemegang izin tinggi tidak bisa di-timeout/kick. Ban selalu manual.
- Pengguna harus disebut dengan ID, mention, atau username persis (nama tampilan/nickname tidak dicocokkan
  lagi, supaya tidak salah orang). Konfirmasi selalu memuat ID dan teks lengkap yang akan dikirim.
- Setelah klik Approve, keadaan server diperiksa ulang; bila berubah dari yang disetujui, aksi
  dibatalkan ("keadaan berubah, ulangi"). Membatalkan permintaan di klien juga membatalkan konfirmasinya.
- Isi pesan yang dibaca dari Discord dibungkus penanda `<<<DATA-DISCORD …>>>` dengan nonce acak dan
  tiap baris diberi awalan, sehingga satu pesan tidak bisa memalsukan pesan lain; semuanya data,
  bukan instruksi. Argumen tool diperiksa tipenya secara ketat (ID harus string).
- **Jaminan yang jujur.** Tool yang mengubah sesuatu selalu lewat konfirmasi admin (`run_tool`), dijaga
  oleh lint bytecode saat tool didaftarkan dan klien Discord baca-saja saat tool baca berjalan. Ini TIDAK
  mencakup fungsi bantu yang dipanggil tool, klien HTTP buatan sendiri, thread lain, atau penulisan
  berkas lokal: tool baru wajib ditinjau manusia. Bila konfigurasi tim tak terbaca, grup `discord_*`
  disembunyikan (gagal-tertutup).
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

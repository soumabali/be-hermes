# Perubahan perilaku v3.1 (hasil audit)

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
- **Thread per percakapan** aktif secara bawaan (`threads.personal: true`); `#hermes-admin` tetap satu aliran.
  Setel `false` untuk perilaku lama (satu sesi per channel).
- **Pencatatan pemakaian** baru berjalan setelah timer dipasang; tool `usage_report` juga memotret saat dipanggil.
- Profile `team-admin` kini membaca skill bawaan kit dari `/opt/hermes-team/skills` (hanya-baca, milik root).
- Plugin 3.1.0 butuh `plugins.isolation: in_process` (bawaan Hermes). Satu salinan modul dimuat per profile,
  jadi status lintas-profile disimpan di entri `sys.modules` sintetis (`shared.py`).
- `hold_stream` (opsional, default mati; setel dengan `hermes -p <profile> config set plugins.entries.team-guard.settings.hold_stream true`): menahan jawaban AI sampai disamarkan; tanpa itu balasan yang
  mengandung secret bisa terlihat sesaat saat streaming.

> **Batas pembuktian.** Uji memakai Discord tiruan dan stub API plugin; uji integrasi di CI memakai kode
> Hermes asli tetapi tidak menyalakan gateway. Perilaku di gateway yang hidup, Discord, dan Docker baru
> terbukti lewat `selftest.sh` di server dan tes manual Langkah 11 di server uji.

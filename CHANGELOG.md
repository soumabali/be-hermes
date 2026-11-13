# Catatan perubahan

Format mengikuti [Keep a Changelog](https://keepachangelog.com/id-ID/1.1.0/). Nomor versi kit hanya ada di
satu tempat: `KIT_VERSION` di `kit_base.py` (lihat dengan `python3 hermes_team.py --version`).

## [Belum dirilis]

Hasil audit kepraktisan operator. Yang bertanda **PERILAKU BERUBAH** terasa oleh instalasi yang sudah jalan.

### Ditambahkan
- `kit_paths.py`: satu sumber lokasi berkas tim (`team_dir()`, `team_file()`, `members_file()`,
  `state_dir()`, `ledger_path()`), dipakai `hermes_team.py`, `kit_doctor.py`, `usage_ledger.py`, `usage_report.py`.
- `hermes_team.py --version`.
- `hermes_team.py managed --diff` (beda kebijakan dengan berkas managed yang terpasang; exit 1 bila ada) dan
  `managed --install` (menulis `/etc/hermes/config.yaml` secara atomik, 0644; harus root — tanpa root
  mencetak perintah `sudo` persisnya). Tidak perlu lagi menyalin lewat berkas sementara.
- `hermes_team.py remove-member <nama> [--delete-profile] [--delete-channel] [--yes]`: hapus permanen anggota
  yang sudah nonaktif (entri `members.yaml` + rujukan di registry skill bersama; opsional profile lewat
  `hermes profile delete` dan channel Discord). Menampilkan dulu apa yang dihapus dan apa yang TIDAK
  (container/volume sandbox: perintah `docker`-nya dicetak). Mendukung `--dry-run`; di bawah kunci tim.
- `kit_doctor.py`: pemeriksaan konfigurasi (key tak dikenal, ID contoh), berkas managed (ada, sama dengan
  `team.yaml`, tidak lagi mem-pin key yang sudah dipindah ke config per profile), `/etc/hermes/.env` (ada,
  tidak terbaca semua user, memuat tiap `key_env` — hanya nama), linger user, dan timer systemd user.
- `kit_doctor.py --discord`: preflight tingkat tim yang bisa dijalankan sebelum gateway menyala — token bot,
  bot ada di server, role member/admin/entry, urutan role bot, kategori (< 50 channel), channel bersama &
  admin (ada, teks, tidak terlihat `@everyone`, bot bisa melihat & mengirim), izin server bot, channel laporan.
- `tests/selftest.sh`: `HERMES_SRC`, `--allow-skip`, `--detect`; ringkasan di akhir keluaran.
- `CHANGELOG.md` ini.

### Diubah
- **PERILAKU BERUBAH** — `./team.yaml` di direktori kerja tidak lagi dipakai diam-diam. Urutan: `--config` >
  `HERMES_TEAM_DIR` > `~/.hermes-team`. Bila `./team.yaml` ada dan bukan berkas yang dipakai, satu baris
  catatan dicetak ke stderr berisi berkas yang dipakai dan cara memilih yang lain.
- **PERILAKU BERUBAH** — `check` keluar dengan kode ≠ 0 dan `apply` menolak selama konfigurasi masih memuat ID
  contoh dari `team.example.yaml` / `members.example.yaml`; pesannya menyebut key mana saja.
  (`--allow-placeholders` atau env `HERMES_TEAM_ALLOW_PLACEHOLDERS=1` hanya untuk uji otomatis.)
- **PERILAKU BERUBAH** — `members.example.yaml` kini `members: []`; contoh entri berupa komentar. Dulu berkas
  itu memuat dua anggota contoh yang ikut dibuatkan profile oleh `apply`.
- **PERILAKU BERUBAH** — `tests/selftest.sh` keluar dengan kode 3 bila uji integrasi dilewati (kecuali
  `--allow-skip`), dan memasang dependensi uji ke venv sekali pakai (`--system-site-packages`) alih-alih
  meminta `pip install` ke Python sistem / venv Hermes.
- Key yang tidak dikenal di `team.yaml` (teratas dan tiap bagian) dan `members.yaml` dikumpulkan sebagai
  peringatan dengan saran "maksudnya …?" dan dicetak oleh `check`, `apply`, `status`, dan doctor. Key yang
  sejak dulu ditolak (`onboarding.*`, `usage.reports.*`, `sandbox` per anggota) tetap error.
- `status` kini ikut mencetak peringatan konfigurasi.
- `usage_report.py` menjadi pembungkus tipis: penemuan profile dan pemetaan anggota → profile memakai
  helper `usage_ledger.py`; tanpa `--members-file` ia membaca `members.yaml` di folder tim.
- Saran perintah pasang timer di doctor menyertakan `mkdir -p ~/.config/systemd/user` dan memakai lokasi
  kit yang sebenarnya.
- Label versi "kit v3" yang usang dibuang dari teks `--help` dan kepala `team.example.yaml`.

### Diperbaiki
- `usage_ledger.py` dan `usage_report.py` membaca `profile_prefix` dari `members.yaml`, padahal setelan itu
  ada di `team.yaml`: dengan prefix selain `be-`, pemakaian sesi tanpa pengirim (cron, subagent) tidak
  teratribusi ke pemilik profile. Kini dibaca dari `team.yaml`.
- `tests/selftest.sh` melewati uji integrasi pada instalasi nyata: launcher `hermes` berbentuk pembungkus
  `/bin/sh` (`exec <python> -I -c …`) tidak dikenali, dan direktori sumber Hermes tidak ada di `PYTHONPATH`.
- `kit_doctor.py --discord` tidak memeriksa apa pun saat `members: []`.

## [3.1.0] - 2026-11-13

Dirangkum dari riwayat git sejak baseline v3 (`Impor kit Hermes tim v3`).

### Ditambahkan
- Image sandbox tim, CI, dan `tests/selftest.sh`; CI memasang Hermes dengan uv dari lockfile upstream dan
  uji integrasi wajib jalan (`KIT_REQUIRE_HERMES`).
- Laporan pemakaian LLM per anggota (`usage_report.py`) dan buku besar pemakaian (`usage_ledger.py`):
  token & request per orang harian/mingguan/bulanan; laporan terjadwal ke channel dan ekspor metrik.
- Perintah doctor (`kit_doctor.py`): audit members ↔ profile ↔ rute ↔ Discord; doctor memeriksa buku besar.
- Plugin team-guard dan pagar teamctl untuk server Discord (diperketat setelah audit).
- teamctl: opsi `allow_unscanned`, nama onboarding memperhitungkan profile sisa, peringatan daftar
  terpotong; uji share/unshare skill.
- Opsi `threads`: tiap pesan baru di channel pribadi membuka thread/sesi baru.
- Rencana skala dijalankan: batas per orang, penghitung LLM, anggaran token, metrik, alat ukur.
- Memori sandbox per anggota dan ambang kompresi per model.
- Contoh `team.yaml`: image sandbox dan egress bawaan.

### Diubah
- Modul dipecah (`kit_base.py`, `kit_discord.py`); versi kit menjadi 3.1.0; perubahan perilaku v3.1
  didokumentasikan.
- Offboarding memarkir profile (`gateway.parked`) dan `apply` menyelaraskan penanda itu dengan `members.yaml`.
- Lobby: semua toolset upstream ditutup; aux task `skills_hub` dibuang; uji drift terhadap Hermes asli.

### Diperbaiki
- `hermes_team.py`: 20 temuan audit (transaksi onboarding, profile bekas, kunci, retry, skill guard, dll).
- Uji pemindai skill tidak lagi bergantung pada urutan impor (lulus juga dengan Hermes asli); uji LLM tiruan
  menunggu penghitung token (race di CI Python 3.11).

## [3.0.0]

Baseline: impor kit Hermes tim v3 dari handoff.

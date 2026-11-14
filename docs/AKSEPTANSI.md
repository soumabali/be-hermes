# Akseptansi: dari 7/10 ke 10/10

Nilai 10/10 di sini berarti satu hal: **setiap klaim kit sudah terbukti di lingkungan sungguhan**, bukan hanya di
uji dan tiruan. Dokumen ini memisahkan yang sudah terbukti, yang masih menunggu server uji, dan pekerjaan kode
yang tersisa. Centang hanya setelah perintahnya benar-benar dijalankan dan keluarannya cocok.

## A. Sudah terbukti

| Klaim | Bukti |
|---|---|
| Uji unit + integrasi hijau terhadap Hermes sungguhan (versi terpin dan `main`) | CI `ci.yml`: unit Python 3.10/3.11/3.14, integrasi `KIT_REQUIRE_HERMES=1` |
| Titik masuk jalan dari salinan lain / `python -I` | `tests/test_entrypoints.py` |
| `sandbox_idle` memakai Docker dengan benar: hentikan yang diam, biarkan yang sibuk, tidak pernah `rm` | Dijalankan terhadap container berlabel sungguhan (Docker 29.9.2); lihat docstring `tools/sandbox_idle.py` |
| POST/PATCH tidak diulang pada 5xx Discord | `tests/test_kit_units.py` (`test_post_is_not_retried_on_5xx`) |

## B. Menunggu server uji (butuh Discord, Hermes, systemd, Docker sungguhan)

Siapkan satu server Linux dengan systemd, Docker, sebuah server Discord kosong untuk uji, dan satu API key LLM.
Jalankan berurutan; berhenti di pemeriksaan pertama yang gagal dan perbaiki dulu.

| # | Pemeriksaan | Perintah | Lulus bila |
|---|---|---|---|
| B1 | Wizard dari server bersih | `sudo bash setup.sh` | Selesai tanpa galat; ringkasan akhir memuat nol peringatan merah |
| B2 | Wizard bisa diulang | `sudo bash setup.sh` lagi | Semua langkah ditandai selesai, tidak ada channel/role dobel di Discord |
| B3 | Wizard terhenti lalu lanjut | hentikan di tengah (Ctrl-C), jalankan lagi | Lanjut dari langkah yang belum selesai; kode keluar 130 saat dibatalkan |
| B4 | Gateway hidup | `systemctl --user is-active hermes-gateway.service` | `active`, tetap `active` setelah 2 menit |
| B5 | Diagnosa | `python3 /opt/hermes-team/kit_doctor.py` | Tanpa galat; peringatan hanya yang kamu pahami |
| B6 | Onboarding anggota | beri role tim ke akun uji | Channel pribadi terbentuk, anggota hanya bisa melihat channelnya sendiri |
| B7 | Percakapan end-to-end | kirim pesan di channel pribadi | Balasan LLM muncul; satu pesan baru membuka satu thread |
| B8 | Sandbox | minta agen menjalankan `echo $HOME` di terminal | Container `hermes-profile=<profile>` terbentuk dan menjawab |
| B9 | Secret pribadi | `/env-set` nama `MY_UJI` lalu minta agen memakai `$MY_UJI` | Agen berhasil memakai, nilai tidak muncul di transcript/log |
| B10 | Isolasi antar anggota | akun anggota A meminta isi channel/secret anggota B | Ditolak; tidak ada data B yang bocor |
| B11 | Catatan pemakaian | `python3 usage_ledger.py report --period day` setelah B7 | Baris untuk anggota uji dengan token > 0 |
| B12 | Anggaran | set `daily_token_budget` kecil, kirim pesan sampai lewat | Pesan ditolak dengan alasan anggaran; admin bisa menambah jatah |
| B13 | Tool admin + konfirmasi | di #hermes-admin: "buat channel #uji" | Tombol konfirmasi muncul; tanpa Approve tidak ada perubahan |
| B14 | Pagar tool admin | minta baca/ubah channel pribadi anggota | Ditolak sebelum tombol konfirmasi muncul |
| B15 | Hapus anggota | `remove-member <nama> --delete-profile --delete-channel` | Profile hilang, channel hilang, tombstone dibersihkan |
| B16 | `managed --install` | dari `/opt/hermes-team` sebagai root | Ditulis 0644; `managed --diff` bersih; dari folder user ditolak |
| B17 | Idle sandbox menyala lagi | hentikan satu container (`docker stop`), kirim pesan yang memakai terminal | Container hidup lagi dan `/workspace` utuh. **Syarat sebelum `sandbox_idle --apply` dipakai** |
| B18 | Gateway restart | `/restart` di channel admin | Gateway kembali `active`; percakapan lanjut |
| B19 | Beban 20 orang | `tests/loadtest/README.md` terhadap gateway uji | Tidak ada anggota kelaparan; batas tidak terlampaui |
| B20 | Upgrade Hermes | naikkan Hermes, jalankan `bash tests/selftest.sh` | Uji integrasi dijalankan (bukan dilewati) dan hijau |

Catat tanggal, versi kit (`python3 hermes_team.py --version`), versi Hermes, dan keluaran tiap pemeriksaan di
issue atau berkas catatan. Kegagalan yang ditemukan adalah temuan, bukan kegagalan dokumen ini: laporkan dan
perbaiki di kit, lalu ulang dari pemeriksaan yang gagal.

## C. Pekerjaan kode yang tersisa

| Item | Alasan | Selesai bila |
|---|---|---|
| Pecah closure `plan()` panjang di `teamctl/tools_discord.py` (100, 71, 64 baris) dan `tools_team.py`/`tools_usage.py` (71, 83) | Tool baru lebih mudah ditulis dan ditinjau bila dasarnya kecil | Tiap fungsi < 60 baris, `tools/list` identik (uji pin SHA256 tetap hijau) |
| Pecah `usage_ledger._run` (119 baris) dan `collect` (65) | Sama | < 60 baris, uji ledger tetap hijau |
| Langkah manual saat menambah jenis profile (`teamctl/tools_team.py` set-model, `teamctl/discord_guard.py` team_channels, `kind` di plugin, `kit/skills.py`) | Menambah jenis profile semestinya satu entri registry | Uji `TestRegisteredKindWorksEndToEnd` mencakup teamctl dan plugin, tanpa edit manual |
| `tests/test_contract.py` memakai `kit_config.plugin_default_problems()` | Satu sumber untuk drift setelan | Satu pemeriksaan, bukan dua |
| Audit independen ulang atas perbaikan putaran kedua | Penulis tidak boleh jadi satu-satunya pemeriksa | Auditor yang tidak ikut menulis tidak menemukan temuan tinggi |

## D. Cara menilai

- **10/10** hanya bila seluruh bagian B lulus di server sungguhan, bagian C selesai, dan audit ulang bersih.
- Sebelum B1–B5 lulus, kit boleh dicoba di server uji tetapi **jangan dipakai tim**.
- Sebelum B17 lulus, `sandbox_idle --apply` jangan dipakai.

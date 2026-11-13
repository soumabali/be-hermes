# Fitur

## Baru di v3.1

| Fitur | Ringkasnya | Rinci |
|---|---|---|
| **Thread per percakapan** | Tiap pesan baru di channel pribadi membuka thread = sesi baru; konteks satu diskusi tidak tercampur diskusi lain. Memory, skill, secret, sandbox tetap sama. | [Thread dan memory](ANGGOTA.md), `team.yaml` → `threads` |
| **Pencatatan pemakaian** | Token dan request per orang, per hari / minggu / bulan, di buku besar sendiri; diambil lewat CLI, tool `usage_report`, atau skill `team-usage-report`. | [Pemakaian](PEMAKAIAN.md) |
| **`doctor`** | Membandingkan members.yaml ↔ profile ↔ rute ↔ channel Discord ↔ buku besar pemakaian; hanya membaca. | [Operasional](OPERASI.md) |
| **Onboarding transaksional** | Validasi dulu, channel dipakai ulang, batal bersih bila gagal; profile sisa tidak diwarisi orang baru. | [Perubahan v3.1](PERUBAHAN-V3.1.md) |
| **Parkir resmi** | Anggota nonaktif diparkir: adapter dan cron job-nya berhenti, data utuh. | [Onboarding](ONBOARDING.md), [Perubahan v3.1](PERUBAHAN-V3.1.md) |
| **Pagar kelola server diperketat** | Konfirmasi divalidasi ulang setelah Approve, channel pribadi tak bisa diedit/dikirimi, pencabutan role dan penghapusan izin dijaga, tak ada cara mematikan konfirmasi. | [Tool admin](ADMIN.md) |
| **Berbagi skill gagal-tertutup** | Hanya skill yang dinyatakan aman oleh pemindai Hermes yang dibagikan. | [Perubahan v3.1](PERUBAHAN-V3.1.md) |
| **Lobby hanya-teks** | Semua toolset Hermes ditutup untuk profile default; CI gagal bila Hermes menambah toolset baru. | [Perubahan v3.1](PERUBAHAN-V3.1.md) |
| **Pembatas skala berlapis** | Batas sesi per orang, antrean adil, penghitung panggilan LLM, pembatas adaptif saat 429, anggaran token harian, metrik beban. | [Skala](SKALA.md) |
| **Alat ukur** | `tools/probe_llm.py` (kapasitas endpoint), `kit_doctor.py --docker` (RAM sandbox), simulasi 20 pengguna. | [Skala](SKALA.md) |
| **Image sandbox tim** | Dockerfile berisi toolchain backend. | Langkah 8 |
| **Egress bawaan Hermes** | Sandbox hanya memegang token proxy, bukan key asli. | Langkah 8, [Secret pribadi](ANGGOTA.md) |
| **Uji dan CI** | Uji unit + uji integrasi terhadap Hermes yang dipin dan `main` (mingguan); `selftest.sh` di server. | Langkah 9 |

## Dari v3

| Kebutuhan | Cara kit memenuhinya |
|---|---|
| User baru di server langsung dapat role entry | Plugin `team-guard` mendengar event *member join* dan memberi `roles.entry` (setelah lolos membership screening bila server memakainya). |
| Setelah admin memberi @backend: channel, env, profile, sandbox sendiri | Event *role berubah* → plugin memeriksa siapa pemberinya (audit log), lalu menjalankan `hermes_team.py onboard`: channel privat `#hermes-<username>` (hanya dia + bot), profile `be-<username>` dari template (SOUL, memory kosong, `.env` kosong, sandbox Docker sendiri, plugin, model default), rute gateway. |
| Masukkan dia & bot ke channel, langsung sapa dengan mention | Izin channel hanya untuk orang itu + bot. Bot mengirim pesan sambutan yang me-mention dia (bisa diubah di `team.yaml`); role entry dicabut; admin dikabari di #hermes-admin. |
| Tanpa restart | **Live reload**: plugin menerapkan rute & daftar channel baru ke gateway yang sedang jalan dan memverifikasinya. Gagal (mis. Hermes berubah) → restart halus otomatis saat tidak ada sesi aktif. |
| Hermes bisa mengelola server Discord | 14 tool `discord_*` di #hermes-admin: lihat server/anggota, baca pesan channel non-pribadi, buat/ubah/hapus channel & role, atur izin, beri/cabut role, timeout/kick, kirim pengumuman. Semua dengan tombol konfirmasi dan pagar keamanan. |

Dari v2: tool admin (tambah anggota, bagikan skill, model per orang), `/env-set` aman, model terkunci
untuk anggota, penyamaran secret berlapis, dan mekanisme skala 20 orang.

## Isi kit

Semua berkas di bawah ada di repo ini; memasang kit berarti menyalin repo ke `/opt/hermes-team` (Langkah 5).
Satu-satunya bagian yang dipasang terpisah adalah timer pemakaian ([Pemakaian](PEMAKAIAN.md)); `kit_doctor.py` mengingatkan bila belum.


| File | Fungsi |
|---|---|
| `setup.sh`, `kit_setup.py` | Wizard instalasi 14 langkah (lihat [Instalasi cepat](../README.md#instalasi-cepat-satu-perintah)). |
| `kit_paths.py` | Satu tempat penentu lokasi `team.yaml`, state, dan buku besar. |
| `CHANGELOG.md` | Perubahan per versi. |
| `kit_base.py`, `kit_discord.py` | Dasar (versi, `TeamError`, tokenizer `.env`) dan klien Discord REST ringan; diimpor `hermes_team.py` dan tetap terlihat sebagai `hermes_team.DiscordAPI` dst. |
| `hermes_team.py` | CLI provisioning & operasi (check, apply, managed, add-member, onboard, offboard, set-model, share-skill, …). Juga dipakai plugin dan tool admin. |
| `teamctl_mcp.py` | Server MCP 27 tool: admin tim, kelola server Discord, dan `usage_report`; dipasang otomatis hanya di profile `team-admin`. |
| `plugins/team-guard/` (v3.1.0, modul kecil: `onboarding`, `livereload`, `secrets`, `guard`, `redact`, `ratelimit`, …) | Plugin Hermes: `/env-set`, penyamaran secret, guard perintah, rate limit & antrean, **onboarding otomatis, live reload rute**. Dipasang ke semua profile. |
| `team.example.yaml` | Setelan tim (guild, role, admin, channel, model, sandbox, thread, skala, pemakaian, secret, onboarding, kelola server). |
| `members.example.yaml` | Anggota + model per orang (juga ditulis otomatis oleh onboarding). |
| `kit_doctor.py` | `doctor`: membandingkan members.yaml ↔ profile di disk ↔ rute gateway ↔ channel Discord (`--discord`) ↔ buku besar pemakaian; hanya membaca. |
| `usage_ledger.py` | Buku besar pemakaian: token & request per orang per hari/minggu/bulan, tahan terhadap pembersihan sesi ([Pemakaian](PEMAKAIAN.md)). |
| `skills/team-usage-report/` | Skill admin untuk laporan pemakaian (dipanggil manual atau oleh cron). Terbaca otomatis oleh profile `team-admin` setelah `apply`. |
| `systemd/` | Timer user: potret pemakaian, laporan terjadwal, dan pelapor sandbox menganggur. |
| `usage_report.py` | Laporan sekali-jalan langsung dari `state.db` (tanpa buku besar). |
| `tools/` | `probe_llm.py` (ukur kapasitas endpoint LLM), `usage_post.py` (laporan pemakaian terjadwal ke channel), `metrics_export.py` (metrik untuk Prometheus), `sandbox_idle.py` (laporkan/hentikan sandbox menganggur). |
| `tests/loadtest/` | Simulasi 20 pengguna dan server LLM tiruan untuk uji beban. |
| `docs/RENCANA-SKALA.md` | Rencana skala beserta status tiap butir. |
| `docker/sandbox/` | Dockerfile image sandbox tim (PHP, Go, Java, Node, Python, klien DB, `gh`). |
| `.github/workflows/ci.yml` | CI: uji unit + uji integrasi terhadap Hermes yang dipin dan `main` (mingguan). |
| `tests/` | `selftest.sh` menguji seluruh kit memakai Python milik Hermes di server ini (HOME sementara, Discord tiruan). Uji integrasi (resolver rute, loader plugin) dilewati bila Hermes tidak bisa di-import. |

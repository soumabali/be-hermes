# Operasional

| Kebutuhan | Caranya |
|---|---|
| Tambah anggota | Beri role `@backend` (atau minta di #hermes-admin) |
| Nonaktifkan anggota | Cabut `@backend`, atau "nonaktifkan <nama>" |
| Onboarding ditahan / gagal | "status onboarding" → "lanjutkan onboarding @x"; detail di audit.log |
| Ganti model | Chat atau `set-model`; berlaku di sesi baru |
| Ubah batas skala / sandbox / provider | Edit `team.yaml` → `apply` → `managed` → pasang ulang `/etc/hermes/config.yaml` → `/restart` |
| Update kit | Lihat [Upgrade](INSTALL.md#upgrade-dari-v30): perbarui `/opt/hermes-team` → `apply` → `managed --install` → `apply` → `/restart`; perubahan ada di `CHANGELOG.md` |
| Pasang kebijakan managed | `sudo python3 /opt/hermes-team/hermes_team.py managed --install …` dari salinan kit milik root (bukan dari folder user); selain itu ditolak |
| Hapus anggota permanen | Nonaktifkan dulu, lalu `hermes_team.py remove-member <nama> --delete-profile --delete-channel` (`--dry-run` untuk melihat dulu) |
| Cek berkas managed | `hermes_team.py managed --diff` |
| Versi kit | `hermes_team.py --version` |
| Update Hermes | `/update` (admin) atau `hermes update`; lalu `bash tests/selftest.sh` (memeriksa live reload) |
| Periksa konsistensi | `python3 /opt/hermes-team/kit_doctor.py [--discord]` (members ↔ profile ↔ rute ↔ channel) |
| Pemakaian token & request per orang | `python3 /opt/hermes-team/usage_ledger.py report --period week` atau tanya di #hermes-admin ([Pemakaian](PEMAKAIAN.md)) |
| Log | `hermes logs -f`; cari `team-guard` |
| Audit | `~/.hermes-team/state/audit.log`, `onboarding.json` |

## Operasi harian

**Backup.** Empat lokasi memuat semua keadaan; cadangkan bersama, saat gateway berhenti atau lewat snapshot:

| Lokasi | Isi |
|---|---|
| `~hermes/.hermes/` | profile, memory, skill, sesi (`state.db`), `.env` tiap profile, token bot |
| `~hermes/.hermes-team/` | `team.yaml`, `members.yaml`, `state/` (roster, audit, buku besar pemakaian) |
| `/etc/hermes/` | kebijakan managed dan API key LLM |
| volume Docker sandbox | `/workspace` tiap anggota (`docker volume ls`, label `hermes-profile`) |

```bash
sudo -iu hermes hermes gateway stop
sudo tar --numeric-owner -czf hermes-backup-$(date +%F).tgz /home/hermes/.hermes /home/hermes/.hermes-team /etc/hermes
sudo -iu hermes hermes gateway start
```

Arsip itu berisi secret; simpan terenkripsi. Pemulihan: bentangkan ke lokasi yang sama, `apply`, lalu
`kit_doctor.py --discord`. Prosedur ini belum pernah diuji pulih; coba sekali di server percobaan.

**Rotasi token bot.** Reset di Developer Portal → ganti baris `DISCORD_BOT_TOKEN` di `~hermes/.hermes/.env`
→ `hermes gateway restart`. Tidak ada berkas kit lain yang menyimpan token.

**Rotasi API key LLM.** `sudoedit /etc/hermes/.env` → `hermes gateway restart`. Dengan `llm.per_user_keys`,
pakai `hermes_team.py llm-key <nama>`.

**Menghapus anggota permanen.** Cabut role `backend` (atau "nonaktifkan <nama>"), lalu:

```bash
python3 /opt/hermes-team/hermes_team.py remove-member budi --delete-profile --delete-channel --dry-run
python3 /opt/hermes-team/hermes_team.py remove-member budi --delete-profile --delete-channel
```
`remove-member` mencatat channel anggota di `state/removed_channels.json` sebelum mengubah `members.yaml`; channel itu tetap dilindungi dari tool admin sampai benar-benar terhapus.


Perintah itu mencetak apa yang tidak ikut dihapus (container dan volume sandbox) beserta perintah Docker-nya.

**Banyak anggota sekaligus.** `hermes_team.py onboard-batch --file anggota.yaml`, dengan isi:

```yaml
- {user_id: "500000000000000011", username: dodi}
- {user_id: "500000000000000012", username: eka, model: td-normal}
```

**Pindah server.** Pulihkan backup di server baru, pasang Hermes dan kit (`setup.sh --from hermes_install`
akan melewati langkah yang sudah beres), jalankan `apply`, lalu matikan gateway lama **sebelum** menyalakan
yang baru: satu token bot tidak boleh dipakai dua gateway.

**Rollback kit.** `/opt/hermes-team` adalah salinan repo; kembali ke commit atau tag sebelumnya, `apply`,
`managed --install`, `apply`, `/restart`. Profile dan data tidak tersentuh. Perubahan tiap versi ada di
`CHANGELOG.md`.

# Operasional

| Kebutuhan | Caranya |
|---|---|
| Tambah anggota | Beri role `@backend` (atau minta di #hermes-admin) |
| Nonaktifkan anggota | Cabut `@backend`, atau "nonaktifkan <nama>" |
| Onboarding ditahan / gagal | "status onboarding" → "lanjutkan onboarding @x"; detail di audit.log |
| Ganti model | Chat atau `set-model`; berlaku di sesi baru |
| Ubah batas skala / sandbox / provider | Edit `team.yaml` → `apply` → `managed` → pasang ulang `/etc/hermes/config.yaml` → `/restart` |
| Update kit | Lihat [Upgrade dan rollback kit](INSTALL.md#upgrade-dan-rollback-kit): perbarui `/opt/hermes-team` (caranya beda untuk salinan wizard dan clone git) → `apply` → `managed --install` → `apply` → `/restart`; perubahan ada di `CHANGELOG.md` |
| Pasang kebijakan managed | `sudo python3 /opt/hermes-team/hermes_team.py managed --install …` dari salinan kit milik root (bukan dari folder user). Kit user-owned ditolak, tetapi itu hanya pencegah salah pasang: kode kit sudah diimpor sebelum pemeriksaan, jadi pastikan sendiri `/opt/hermes-team` milik root |
| Hapus anggota permanen | Nonaktifkan dulu, lalu `hermes_team.py remove-member <nama> --delete-profile --delete-channel` (`--dry-run` untuk melihat dulu) |
| Cek berkas managed | `hermes_team.py managed --diff` |
| Versi kit | `hermes_team.py --version` |
| Update Hermes | Kit hanya diuji CI terhadap commit di `/opt/hermes-team/HERMES_REF`. `/update` (admin) atau `hermes update` memindahkan Hermes ke versi lain: sesudahnya **wajib** `bash /opt/hermes-team/tests/selftest.sh` (memeriksa rute, plugin, live reload). Kembali ke commit yang diuji: `sudo bash setup.sh --only hermes_install` lalu setujui pin ulang |
| Gateway mati / bot offline | [Gateway mati](#gateway-mati) |
| Periksa konsistensi | `python3 /opt/hermes-team/kit_doctor.py [--discord]` (members ↔ profile ↔ rute ↔ channel) |
| Pemakaian token & request per orang | `python3 /opt/hermes-team/usage_ledger.py report --period week` atau tanya di #hermes-admin ([Pemakaian](PEMAKAIAN.md)) |
| Log | `hermes logs -f`; cari `team-guard` |
| Audit | `~/.hermes-team/state/audit.log`, `onboarding.json` |

## Operasi harian

Perintah di bagian ini memakai dua pembantu. `systemctl --user` dan `hermes gateway …` butuh `XDG_RUNTIME_DIR`
milik user `hermes`; `sudo -iu hermes` tidak selalu menyetelnya, jadi perintah dijalankan dengan lingkungan
yang sama dengan yang dipakai wizard:

```bash
HH="$(getent passwd hermes | cut -d: -f6)"      # home user Hermes; biasanya /home/hermes
hu() { sudo -u hermes -H env XDG_RUNTIME_DIR="/run/user/$(id -u hermes)" \
         PATH="$HH/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }
```

Bila user Hermes-mu bukan `hermes` (wizard: `--user`), ganti namanya di kedua baris itu.

### Backup

Lima lokasi memuat semua keadaan. Cadangkan bersama, saat gateway berhenti atau lewat snapshot disk:

| Lokasi | Isi | Ikut arsip di bawah |
|---|---|---|
| `~hermes/.hermes/` | profile, memory, skill, sesi (`state.db`), `.env` tiap profile, token bot | ya, kecuali `hermes-agent/` |
| `~hermes/.hermes-team/` | `team.yaml`, `members.yaml`, `state/` (roster, audit, buku besar pemakaian), `gateway-watch.env` | ya |
| `/etc/hermes/` | kebijakan managed dan API key LLM | ya |
| `/var/lib/hermes-team-setup/` | state wizard (`state.json`: ID server/role/channel, jawaban, langkah selesai; tanpa rahasia) | ya |
| volume Docker sandbox | `/workspace` tiap anggota | perintah terpisah di bawah |

Yang sengaja **tidak** dicadangkan karena dipasang ulang: kode Hermes (`~hermes/.hermes/hermes-agent/`, dipasang
ulang oleh installer pada commit di `HERMES_REF`), launcher `~hermes/.local/bin/hermes`, kode kit
(`/opt/hermes-team`), unit systemd di `~hermes/.config/systemd/user/`, dan **image** sandbox (dibangun ulang).

```bash
OUT="$PWD/hermes-backup-$(date +%F)"; mkdir -p "$OUT"
hu hermes gateway stop
sudo tar --numeric-owner -czf "$OUT/data.tgz" --exclude=.hermes/hermes-agent \
    -C "$HH" .hermes .hermes-team \
    -C /etc hermes \
    -C /var/lib hermes-team-setup
hu hermes gateway start
```

Instalasi manual (tanpa wizard) tidak punya `/var/lib/hermes-team-setup`: hapus baris terakhir perintah `tar`.

Volume sandbox. Lihat dulu bagaimana `/workspace` dipasang di servermu, lalu cadangkan tiap volume bernama:

```bash
# container sandbox, profile-nya, dan tiap mount (volume bernama atau bind ke folder host)
docker ps -aq --filter label=hermes-profile | xargs -r docker inspect -f \
  '{{.Name}} [{{index .Config.Labels "hermes-profile"}}]{{range .Mounts}} {{.Type}}:{{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}}→{{.Destination}}{{end}}'

# satu arsip per volume bernama (container sebaiknya berhenti: gateway sudah dihentikan di atas)
for v in $(docker ps -aq --filter label=hermes-profile | xargs -r docker inspect -f \
           '{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}' | sort -u); do
  docker run --rm -v "$v":/data:ro -v "$OUT":/backup busybox tar -czf "/backup/volume-$v.tgz" -C /data .
done
```

Mount bertipe `bind` menunjuk folder di host: cadangkan folder itu dengan `tar` biasa. Perintah volume ini
**belum pernah dijalankan** terhadap sandbox Hermes sungguhan (di lingkungan pengembangan tidak ada Docker):
periksa keluaran perintah pertama dan isi arsipnya sebelum mengandalkannya.

`data.tgz` berisi secret (token bot, API key, `.env` tiap anggota); simpan terenkripsi dan jangan di server yang sama.

### Pemulihan dan pindah server

> **Belum pernah diuji di server sungguhan.** Urutan di bawah disusun dari kode wizard dan dicoba hanya sebatas
> perintah `tar`-nya. Latih sekali di server percobaan sebelum membutuhkannya; catat yang tidak cocok.

Syarat: nama user Hermes dan jalur home-nya **sama** dengan server asal (konfigurasi di `~/.hermes` memuat jalur
absolut), dan server Discord serta bot-nya tetap yang sama.

1. **Siapkan server** (lewati di server yang sama bila Docker dan user `hermes` masih ada). Di clone kit:

   ```bash
   sudo bash setup.sh --only preflight
   sudo bash setup.sh --only packages        # Docker, git, curl
   sudo bash setup.sh --only linux_user      # user hermes, grup docker, linger
   ```

   `--from hermes_install` saja **tidak cukup** di server baru: `--from` melompati langkah 1–7, jadi Docker,
   user, dan linger tidak pernah disiapkan. Tiga perintah `--only` di atas yang mengerjakannya.

2. **Bentangkan arsip** dan luruskan pemilik (UID di server baru bisa berbeda):

   ```bash
   HH="$(getent passwd hermes | cut -d: -f6)"
   sudo tar --numeric-owner -xzf data.tgz -C "$HH" .hermes .hermes-team
   sudo chown -R hermes:hermes "$HH/.hermes" "$HH/.hermes-team"
   sudo chmod 700 "$HH/.hermes" "$HH/.hermes-team"
   sudo tar -xzf data.tgz -C /etc hermes
   sudo chown -R root:root /etc/hermes && sudo chown root:hermes /etc/hermes/.env && sudo chmod 0640 /etc/hermes/.env
   sudo tar -xzf data.tgz -C /var/lib hermes-team-setup
   sudo chown -R root:root /var/lib/hermes-team-setup && sudo chmod 700 /var/lib/hermes-team-setup
   ```

3. **Volume sandbox** (bila dicadangkan), satu per arsip:

   ```bash
   v=<nama-volume>; docker volume create "$v"
   docker run --rm -v "$v":/data -v "$PWD":/backup busybox tar -xzf "/backup/volume-$v.tgz" -C /data
   ```

   Apakah Hermes memakai lagi volume bernama sama di server baru **belum dibuktikan**; bila tidak, `/workspace`
   anggota mulai kosong dan isinya harus disalin dari arsip dengan tangan.

4. **Matikan gateway lama** (pindah server): di server lama, `hu hermes gateway stop` lalu
   `hu systemctl --user disable hermes-gateway.service`. Satu token bot tidak boleh dipakai dua gateway, dan
   langkah berikut menyalakan gateway di server baru.

5. **Pasang ulang Hermes, kit, layanan**:

   ```bash
   sudo bash setup.sh --from hermes_install
   ```

   Dengan `state.json` yang sudah dipulihkan, wizard memakai ID server/role/channel dan jawaban lama (langkah
   4–7 tidak diulang, Discord tidak disentuh). Yang terjadi: Hermes dipasang pada commit `HERMES_REF`; kit
   disalin ke `/opt/hermes-team`; `team.yaml` dibandingkan — bila kamu pernah mengeditnya, wizard menampilkan
   bedanya dan bertanya *Timpa dengan hasil wizard?* → jawab **t** supaya berkasmu dipertahankan (`members.yaml`
   tidak pernah ditimpa); `apply` → managed → `apply`; timer dipasang lagi; gateway dipasang dan dinyalakan;
   `kit_doctor --discord` dan selftest. Image sandbox tim tidak ikut arsip: jawab **y** pada *Bangun image
   sandbox* (pakai tag yang sama dengan `sandbox.image` di `team.yaml`), atau bangun sendiri (INSTALL.md Langkah 8).

6. Periksa: `hu python3 /opt/hermes-team/kit_doctor.py --discord`, lalu kirim satu pesan dari akun anggota.

**Tanpa `state.json`** (hilang, atau instalasi manual): wizard tetap bisa dipakai — `sudo bash setup.sh` dari awal
menemukan role, kategori, dan channel yang sudah ada lewat namanya (tidak menggandakan), menanyakan ulang URL
LLM dan pilihan, lalu berhenti di pertanyaan *Timpa team.yaml?* → jawab **t**. Atau kerjakan dengan tangan
[Instalasi manual](INSTALL.md) Langkah 1, 4 (pasang Hermes saja; token sudah ada di `.env` yang dipulihkan),
5 (salin kit saja), 9, dan 10.

### Rotasi token bot

Reset di Developer Portal → ganti baris `DISCORD_BOT_TOKEN` di `~hermes/.hermes/.env` → `hu hermes gateway restart`.
Tidak ada berkas kit lain yang menyimpan token.

### Rotasi API key LLM

`sudoedit /etc/hermes/.env` → `hu hermes gateway restart`. Dengan `llm.per_user_keys`, pakai
`hermes_team.py llm-key <nama>`.

### Menghapus anggota permanen

Cabut role `backend` (atau "nonaktifkan <nama>"), lalu:

```bash
python3 /opt/hermes-team/hermes_team.py remove-member budi --delete-profile --delete-channel --dry-run
python3 /opt/hermes-team/hermes_team.py remove-member budi --delete-profile --delete-channel
```

`remove-member` mencatat channel anggota di `state/removed_channels.json` sebelum mengubah `members.yaml`; channel
itu tetap dilindungi dari tool admin sampai benar-benar terhapus. Perintah itu mencetak apa yang tidak ikut
dihapus (container dan volume sandbox) beserta perintah Docker-nya.

### Banyak anggota sekaligus

`hermes_team.py onboard-batch --file anggota.yaml`, dengan isi:

```yaml
- {user_id: "500000000000000011", username: dodi}
- {user_id: "500000000000000012", username: eka, model: td-normal}
```

### Upgrade dan rollback kit

Langkah lengkapnya, untuk kedua tata letak `/opt/hermes-team` (salinan wizard tanpa `.git`, atau clone git),
ada di [Instalasi manual → Upgrade dan rollback kit](INSTALL.md#upgrade-dan-rollback-kit). Ringkasnya untuk
salinan wizard: perbarui **clone-mu** (bukan `/opt/hermes-team`), `sudo bash setup.sh --only kit_install`, lalu
`apply` → `managed --install` → `apply` → `/restart`. Rollback = `git checkout <tag sebelumnya>` di clone itu,
lalu urutan yang sama. Profile dan data tidak tersentuh. Perubahan tiap versi ada di `CHANGELOG.md`.

## Gateway mati

Satu proses gateway melayani seluruh tim; bila ia mati, **semua** orang terdampak sekaligus.

**Yang dilihat anggota dan admin**

- Bot tampil **offline** (abu-abu) di daftar anggota server.
- Pesan di channel mana pun tidak dijawab sama sekali: tidak ada thread baru, tidak ada tanda mengetik,
  tidak ada pesan galat. Anggap pesan yang dikirim selama gateway mati **hilang**: minta anggota mengirim ulang.
- Perintah slash menjawab "The application did not respond".
- Memberi atau mencabut role `backend` tidak memicu apa pun. Saat gateway hidup lagi, plugin menyamakan
  keadaan (lihat [Onboarding](ONBOARDING.md)); periksa dengan "status onboarding" di `#hermes-admin`.
- Laporan pemakaian terjadwal tetap terkirim (timer terpisah), jadi channel laporan **bukan** tanda gateway sehat.

**Periksa dan nyalakan** (pembantu `hu` didefinisikan di [Operasi harian](#operasi-harian)):

```bash
hu systemctl --user status hermes-gateway.service      # Active: active (running)? sejak kapan? kode keluar terakhir?
hu hermes gateway status                               # baca teksnya: "is running" / "is stopped" (kode keluar selalu 0)
hu hermes logs gateway -n 80                           # baris terakhir sebelum mati
hu journalctl --user -u hermes-gateway.service -n 80 --no-pager   # bila log Hermes kosong
hu hermes gateway restart                              # nyalakan lagi
hu systemctl --user is-active hermes-gateway.service   # ulangi setelah ±15 detik: harus tetap "active"
```

`Failed to connect to bus` berarti manajer systemd milik user `hermes` tidak hidup:
`sudo loginctl enable-linger hermes && sudo systemctl start "user@$(id -u hermes).service"`, lalu ulangi.
Menjalankan ulang wizard juga memeriksa ini: `sudo bash setup.sh` mendapati langkah `gateway` tercatat selesai
tetapi unitnya tidak aktif, lalu memasang dan menyalakannya lagi serta menampilkan log bila gagal.

**Penyebab yang paling sering**

| Di log / status | Penyebab | Perbaikan |
|---|---|---|
| `PrivilegedIntentsRequired` | Intent dimatikan di Developer Portal | Tab Bot → hidupkan Server Members Intent dan Message Content Intent → `gateway restart` |
| `LoginFailure` / `Improper token` | Token bot di-reset atau salah | [Rotasi token bot](#rotasi-token-bot) |
| Unit `inactive (dead)` setelah reboot | Linger mati, layanan user tidak ikut menyala | `sudo loginctl enable-linger hermes`, lalu `gateway start` |
| Mati tak lama setelah `hermes update` / `/update` | Hermes baru tidak cocok dengan kit | `bash /opt/hermes-team/tests/selftest.sh`; kembali ke commit `HERMES_REF` (`sudo bash setup.sh --only hermes_install`) |
| Galat konfigurasi / YAML saat start | `team.yaml` atau `/etc/hermes/config.yaml` rusak | `hermes_team.py check`, `hermes_team.py managed --diff`, `hu hermes doctor` |
| `No space left on device`, OOM di `journalctl -k` | Disk penuh (`state.db`, image, log) atau RAM habis | `df -h`, `docker system df`, `kit_doctor.py --docker`; [Skala](SKALA.md) |
| Restart berulang (`NRestarts` naik) | Salah satu di atas, terus dicoba systemd | Baca log **sebelum** restart berikutnya; hentikan dulu dengan `hu hermes gateway stop` |

Setelah hidup lagi: `hu python3 /opt/hermes-team/kit_doctor.py --discord`, lalu kirim satu pesan uji.

### Pemantau otomatis (opsional)

Kit membawa timer user `hermes-gateway-watch` (tiap 5 menit) yang menjalankan `tools/gateway_watch.py`: bila
unit gateway tidak `active` pada dua pemeriksaan berturut-turut, ia mengirim satu pesan ke webhook, mengulanginya
tiap 60 menit selama masih mati, dan mengirim satu pesan saat pulih. Wizard menawarkannya di langkah `extras`
(bawaan: tidak; kunci jawaban `gateway_watch_timer`). Memasang dengan tangan, sebagai user `hermes`:

```bash
mkdir -p ~/.config/systemd/user && cp /opt/hermes-team/systemd/hermes-gateway-watch.* ~/.config/systemd/user/
umask 077; printf 'HERMES_WATCH_WEBHOOK_URL=%s\n' 'https://discord.com/api/webhooks/<id>/<token>' > ~/.hermes-team/gateway-watch.env
systemctl --user daemon-reload && systemctl --user enable --now hermes-gateway-watch.timer
python3 /opt/hermes-team/tools/gateway_watch.py --test       # pesan uji harus muncul di channel webhook
```

URL webhook dibuat di Discord (Edit Channel → Integrations → Webhooks); pakai channel yang dilihat admin,
mis. `#hermes-admin`. URL itu rahasia: hanya ada di `gateway-watch.env` (0600), tidak di unit systemd.

Batasnya, supaya tidak salah diandalkan:

- Pemantau berjalan **di server yang sama**. Server mati, jaringan putus, atau linger mati → tidak ada pesan.
  Untuk itu perlu pemantau dari luar (mis. layanan uptime yang memeriksa server, atau orang yang melihat bot offline).
- Ia hanya melihat unit systemd. Gateway yang hidup tetapi terputus dari Discord tetap terbaca `active`.
- Belum pernah dijalankan terhadap systemd dan webhook Discord sungguhan; baru diuji dengan tiruan. Jalankan
  `--test`, lalu coba sekali: `hu hermes gateway stop`, tunggu ±10 menit, pastikan pesannya datang.

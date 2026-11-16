# Batasan

- **Thread per percakapan** mengandalkan `discord.free_response_auto_thread` Hermes. Pesan berupa *balasan*
  (reply) tidak membuka thread baru, dan thread diarsipkan otomatis oleh Discord (bisa dibuka lagi).
  Jumlah thread yang berjalan bersamaan per orang dibatasi `scale.user_max_concurrent`.
- **Pencatatan pemakaian**: batas hari bisa bergeser paling lama selang timer. Anggaran token ([Skala](SKALA.md)) memakai
  data ini dan mati secara bawaan (`daily_token_budget: 0`).

- Live reload memakai struktur internal gateway Hermes (dicek dengan resolver rute Hermes sendiri
  setiap kali). Bila update Hermes mengubahnya, kit jatuh ke restart halus (atau `/restart` manual)
  — jalankan `selftest.sh` setelah update Hermes.
- Verifikasi pemberi role butuh izin **View Audit Log**. Tanpa itu semua onboarding ditahan sampai
  admin menyetujui (atau set `verify_actor: false`, artinya siapa pun yang bisa memberi `@backend`
  di Discord dipercaya).
- Bot hanya bisa memberi/mencabut role di bawah role-nya sendiri, dan hanya membuat channel bila
  punya Manage Channels di kategori itu.
- Sandbox Docker anggota baru dibuat saat pertama kali ia memakai terminal/file (beberapa detik).
- `/env-set`, `/env-list`, `/env-unset` adalah guild command: terlihat semua anggota server, tapi
  hanya berfungsi di channel pribadi pemanggil (dan untuk admin di #hermes-shared / #hermes-admin).
- Antrean memakai fungsi internal gateway Hermes (`_running_agent_count`, dll.). Kalau update
  Hermes mengubahnya, antrean nonaktif sendiri dan yang berlaku penolakan bawaan.
- `strict_admin_routes: true` membuat hasil cron profile `team-admin` tidak bisa dikirim ke
  #hermes-admin. `strict_personal_routes: true` berlaku sama untuk channel pribadi.
- Jangan jalankan gateway dengan log level DEBUG untuk `discord.py`: pada level itu discord.py
  mencatat payload mentah dari Discord, termasuk isi form `/env-set`.
- Satu bot = satu nama/avatar dan satu menu `/`. Skill pribadi dipanggil dengan bahasa biasa.
- Jaminan konfirmasi tool admin (`teamctl`) adalah lint bytecode + klien Discord baca-saja; keduanya tidak menjangkau
  fungsi bantu, klien HTTP buatan sendiri, thread lain, atau penulisan berkas lokal. Tool baru perlu ditinjau manusia.
- Wizard `setup.sh`, gateway, Discord, dan Docker belum pernah dijalankan di server sungguhan; yang teruji adalah
  tiruan dan CI terhadap Hermes sungguhan (unit + integrasi).
- Pembatas pembuatan cron (`cronguard`) menghitung semua aksi tool cron kecuali `list`, `status`, `pause`, `resume`,
  `remove` (aksi yang tidak dikenal ikut dihitung). Bila penjaganya sendiri galat, panggilan tool cron ditolak
  (gagal-tertutup) dan tool lain tetap jalan. Batasnya per profile, jadi pengguna channel bersama berbagi jatah.
- Pemeriksaan kepemilikan kit pada `managed --install` hanya mencegah salah pasang; bukan perlindungan terhadap kit
  yang sudah dimodifikasi (kodenya sudah diimpor sebelum pemeriksaan).
- Nilai `/env-set` diperiksa dengan pengurai `.env` milik kit sendiri; kecocokannya dengan pengurai dotenv Hermes
  sungguhan baru dibuktikan oleh uji integrasi di CI, bukan oleh uji unit.

## Batasan setelah audit ketiga

- Orang yang hanya memegang role admin Discord tetapi tidak tercantum di `admins` maupun anggota aktif tidak dilayani
  (pesan "belum terdaftar"). Anggota baru juga ditolak beberapa detik sampai onboarding menulis roster.
- Roster salah bentuk saat gateway baru start (belum ada roster sah): dipakai dengan nilai aman, tetapi bila daftar
  `channels` sendiri rusak, channel tim tidak dikenali dan gerbang/pembatas tidak berlaku sampai `hermes_team.py apply`.
- Galat sebelum lingkup pesan diketahui: pesan tetap diteruskan (token tetap disamarkan). Galat di satu pembatas:
  pembatas itu dilewati (gagal-terbuka); `limiter.<nama>` di capabilities.json tidak kembali sehat sampai gateway di-restart.
- Penjaga cron menolak bentuk jadwal yang sah bagi Hermes tetapi tidak dikenalnya (mis. `every 1.5h`, `tomorrow 9am`).
  Satuan detik dan makro `@…` diurai tanpa konfirmasi bahwa Hermes menerimanya.
- Guard dump environment tetap upaya-terbaik: path `/proc` yang dirakit dari variabel, `vars(os)['environ']`, dan pemecahan
  secret per karakter tidak tertangkap. Token pengguna Discord (bukan bot) dan `pk_live_` tidak disamarkan.
- Anggaran tetap tidak memblokir saat buku besar tidak terbaca; kini hanya terlihat (log, `budget.stale`, `budget.ledger`).
- Toolset baru dari Hermes tetap aktif otomatis di profile pekerja sampai kit diperbarui: uji drift di CI gagal saat hulu
  menambah toolset, tetapi server yang memperbarui Hermes tanpa memperbarui kit tidak terlindungi.
- Penjaga sandbox berjalan saat `apply`/onboarding, bukan terus-menerus: managed yang dihapus setelahnya baru ketahuan di
  `apply` berikutnya atau `kit_doctor`. Nilai `docker_volumes` / `docker_extra_args` tidak dinilai (lihat `managed --diff`).
  `sandbox.allow_unmanaged: true` mematikan penjaga ini.
- User `hermes` anggota grup `docker`, jadi setara root di server itu. Kit milik root dan kebijakan managed bukan batas
  terhadap gateway yang sudah dikuasai penyerang.
- Pemulihan cadangan, pindah server, `hermes_repin`, dan pemantau gateway belum pernah dijalankan di server sungguhan.
- Probe kapabilitas hanya memeriksa keberadaan nama di sumber Hermes, bukan tanda tangan atau perilakunya.

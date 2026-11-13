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

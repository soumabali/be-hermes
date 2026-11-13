# Referensi konfigurasi `team.yaml`

Tabel ini **dibangkitkan** dari `FIELDS` di `kit_config.py` (`python3 hermes_team.py config-doc`); jangan disunting
tangan, `tests/test_release.py` gagal bila berbeda. Kolom *plugin* menandai setelan yang ditegakkan plugin team-guard
(diteruskan lewat `roster.json`). Contoh dengan komentar: [`team.example.yaml`](../team.example.yaml).

Key yang salah ketik tidak diabaikan diam-diam: `check`, `apply`, dan `status` memberi peringatan beserta saran nama.

| key | tipe | default | plugin | deskripsi |
|---|---|---|---|---|
| `team_name` | str | `"Backend"` |  | Nama tim (muncul di SOUL.md dan pesan sambutan). |
| `guild_id` | id | `wajib` |  | ID server Discord. |
| `roles.member` | id | `wajib` |  | Role anggota (@backend); diberikan → onboarding otomatis. |
| `roles.admin` | id | `wajib` |  | Role admin (@hermes-admin). |
| `roles.entry` | id_opt | `""` |  | Role untuk user baru di server (opsional). |
| `admins` | ids | `[]` |  | User ID admin tim (minimal satu): boleh /update, /restart, dan semua perintah. |
| `category_id` | id_opt | `""` |  | Kategori Discord untuk channel pribadi baru (opsional). |
| `channel_prefix` | str | `"hermes-"` |  | Awalan nama channel pribadi (#hermes-andi). |
| `profile_prefix` | str | `"be-"` |  | Awalan nama profile anggota (be-andi). |
| `template_profile` | profile | `<profile_prefix>template` |  | Profile sumber clone untuk profile pribadi. |
| `shared.profile` | profile | `"team-shared"` |  | Nama profile shared. |
| `shared.channel_id` | id | `wajib` |  | ID channel profile shared. |
| `shared.auto_skills` | strs | `[]` |  | Skill yang dimuat tiap sesi baru di channel shared. |
| `admin.profile` | profile | `"team-admin"` |  | Nama profile admin. |
| `admin.channel_id` | id | `wajib` |  | ID channel profile admin. |
| `admin.auto_skills` | strs | `[]` |  | Skill yang dimuat tiap sesi baru di channel admin. |
| `team_skills_dir` | str | `""` |  | Folder skill tim (read-only untuk anggota); kosong = tidak ada. |
| `team_skills_in_personal` | bool | `true` |  | Skill tim juga tersedia di profile pribadi. |
| `strict_personal_routes` | bool | `false` |  | Rute channel pribadi juga mengunci user_id pemiliknya. |
| `strict_admin_routes` | bool | `true` |  | Rute channel admin dikunci per user_id admin. |
| `lobby_restrict` | bool | `true` |  | Profile default (lobby) hanya teks: toolset dimatikan, tanpa memory. |
| `allow_browser` | bool | `false` |  | Izinkan browser/computer-use (berjalan di HOST, bukan sandbox). |
| `threads.personal` | bool | `true` |  | Channel pribadi: tiap pesan baru membuka thread baru. |
| `threads.admin` | bool | `false` |  | #hermes-admin ikut membuat thread (false = satu aliran). |
| `live_reload` | bool | `true` | root.live_reload | Rute & channel baru diterapkan plugin ke gateway yang jalan (tanpa restart). |
| `restart_fallback` | choice (idle/manual) | `"idle"` | root.restart_fallback | Bila live reload gagal: idle = restart halus otomatis saat sepi; manual = admin mengetik /restart. |
| `user_allowed_commands` | custom | `[]` |  | Daftar perintah slash anggota (menggantikan bawaan kit). |
| `extra_routes` | custom | `[]` |  | Rute gateway tambahan (diteruskan apa adanya). |
| `llm.providers` | custom | `{}` |  | Provider LLM OpenAI-compatible: nama → {api, key_env, ...}. |
| `llm.models` | custom | `{}` |  | Katalog model: alias → {provider, model, [compression_threshold_tokens, proactive_prune_tokens]}. |
| `llm.default_model` | custom | `""` |  | Model anggota tanpa `model` di members.yaml. |
| `llm.admin_model` | custom | `""` |  | Model profile admin. |
| `llm.shared_model` | custom | `""` |  | Model profile bersama. |
| `llm.lobby_model` | custom | `""` |  | Model profile default (lobby). |
| `llm.aux_model` | custom | `""` |  | Model murah untuk tugas auxiliary (kompresi, judul, review, cron). |
| `llm.fallback` | custom | `[]` |  | Rantai model cadangan saat model utama error/throttle. |
| `llm.per_user_keys` | custom | `false` |  | true = tiap profile boleh punya API key sendiri (llm-key). |
| `sandbox.image` | str | `""` |  | Image Docker sandbox; kosong = bawaan Hermes. |
| `sandbox.cpu` | number | `1` |  | CPU bawaan per profile (pengecualian per anggota: 0.25..64). |
| `sandbox.memory_mb` | int ≥64 | `1024` |  | RAM bawaan per profile (MB); plafon, bukan cadangan. |
| `sandbox.network` | str | `""` |  | Jaringan Docker untuk sandbox; kosong = bawaan. |
| `sandbox.http_proxy` | str | `""` |  | Proxy HTTP(S) untuk sandbox; kosong = tanpa proxy. |
| `sandbox.no_proxy` | str | `"localhost,127.0.0.1,::1"` |  | Daftar NO_PROXY sandbox. |
| `scale.max_concurrent_sessions` | int | `8` | settings.max_active | Slot sesi aktif bersamaan untuk anggota (seluruh gateway). |
| `scale.admin_reserved_slots` | int | `2` |  | Slot tambahan khusus admin (tidak ikut antre). |
| `scale.user_rate_per_minute` | int | `6` | settings.rate_per_minute | Pesan per menit per orang (0 = tanpa batas). |
| `scale.user_burst` | int | `4` | settings.burst | Pesan beruntun yang langsung lolos. |
| `scale.queue_max_wait_seconds` | int | `180` | settings.queue_max_wait | Lama antre saat slot penuh sebelum pesan ditolak sopan. |
| `scale.agent_max_turns` | int | `60` |  | Langkah tool per giliran. |
| `scale.goals_max_turns` | int | `4` |  | Lanjutan otomatis /goal (× agent_max_turns langkah dari satu pesan). |
| `scale.delegation_max_children` | int | `1` |  | Subagent paralel per giliran (tiap subagent = satu percakapan LLM). |
| `scale.compression_threshold_tokens` | int | `64000` |  | Kompres konteks paling lambat di angka ini (bawaan; per model: llm.models). |
| `scale.proactive_prune_tokens` | int | `48000` |  | Pangkas output tool lama tanpa LLM di angka ini. |
| `scale.idle_compact_after_seconds` | int | `3600` |  | Sesi yang dibuka lagi setelah sekian detik diringkas dulu. |
| `scale.busy_input_mode` | choice (queue/interrupt/steer) | `"queue"` |  | Pesan susulan saat bot sibuk: queue, interrupt, atau steer. |
| `scale.title_llm` | bool | `false` |  | Judul sesi pakai LLM? false = judul instan, tanpa request. |
| `scale.user_max_concurrent` | int ≥1 | `2` | settings.user_max_concurrent | Sesi aktif per orang (minimal 1); pesan berikutnya antre. |
| `scale.max_llm_parallel` | int | `0` | settings.max_llm_parallel | Panggilan LLM paralel seluruh tim (0 = tidak dibatasi); isi dari tools/probe_llm.py. |
| `scale.daily_token_budget` | int | `0` | settings.daily_token_budget | Token per orang per hari (0 = tanpa batas). |
| `scale.team_daily_token_budget` | int | `0` | settings.team_daily_token_budget | Token seluruh tim per hari (0 = tanpa batas). |
| `scale.adaptive_throttle` | bool | `true` | settings.adaptive_throttle | Turunkan batas sesi efektif saat 429 beruntun, pulih perlahan. |
| `scale.allow_member_cron` | bool | `false` |  | false = toolset cronjob mati untuk anggota & profile bersama. |
| `scale.cron_min_interval_minutes` | int | `15` | settings.cron_min_interval_minutes | Jadwal cron lebih rapat dari ini ditolak plugin (0 = tanpa batas). |
| `scale.allow_goals` | bool | `false` |  | false = /goal dan /subgoal keluar dari daftar perintah bawaan anggota. |
| `scale.session_retention_days` | int | `90` |  | Sesi berakhir yang lebih tua dipangkas Hermes (0 = kit tidak mengatur). |
| `usage.timezone` | str | `""` | usage.timezone | Zona waktu batas hari laporan & anggaran (mis. Asia/Makassar); kosong = zona waktu server. |
| `usage.report_channel_id` | id_opt | `""` |  | Channel tujuan laporan pemakaian terjadwal (bukan channel pribadi). |
| `usage.reports.daily` | time | `""` |  | Jam laporan daily "HH:MM"; kosong = mati. |
| `usage.reports.weekly` | time | `""` |  | Jam laporan weekly "HH:MM"; kosong = mati. |
| `usage.reports.monthly` | time | `""` |  | Jam laporan monthly "HH:MM"; kosong = mati. |
| `secrets.max_keys` | int ≥1 | `40` | settings.max_keys | Jumlah secret per profile. |
| `secrets.min_length` | int ≥1 | `6` | settings.min_length | Nilai lebih pendek ditolak (tidak bisa disamarkan dengan aman). |
| `secrets.scrub_chat` | bool | `true` | settings.scrub_chat | Kredensial yang diketik di chat disamarkan sebelum sampai ke AI. |
| `secrets.delete_secret_messages` | bool | `true` | settings.delete_secret_messages | Pesan aslinya dihapus (bot butuh Manage Messages). |
| `secrets.block_env_dump` | bool | `true` | settings.block_env_dump | Blokir env/printenv/set/echo $SECRET di terminal. |
| `secrets.hint` | bool | `true` | settings.hint | Beri tahu AI NAMA secret yang tersedia (bukan nilainya). |
| `secrets.allow_names` | strs | `[]` | settings.allow_names | Pengecualian nama yang biasanya dilarang. |
| `secrets.deny_names` | strs | `[]` | settings.deny_names | Nama tambahan yang dilarang. |
| `onboarding.enabled` | bool | `true` | onboarding.enabled | Role @backend diberikan → channel + profile + sambutan otomatis. |
| `onboarding.give_entry_role` | bool | `true` | onboarding.give_entry_role | User baru di server mendapat roles.entry. |
| `onboarding.remove_entry_role` | bool | `true` | onboarding.remove_entry_role | Role entry dicabut setelah onboarding selesai. |
| `onboarding.verify_actor` | bool | `true` | onboarding.verify_actor | Hanya jika pemberi @backend adalah admin tim (audit log Discord). |
| `onboarding.trusted_actor_ids` | ids | `[]` | onboarding.trusted_actor_ids | Bot lain yang boleh memberi @backend (mis. bot verifikasi). |
| `onboarding.offboard_on_role_remove` | bool | `true` | onboarding.offboard_on_role_remove | @backend dicabut / user keluar → anggota dinonaktifkan. |
| `onboarding.hide_channel_on_offboard` | bool | `true` | onboarding.hide_channel_on_offboard | Izin melihat channel pribadi dicabut saat offboard. |
| `onboarding.entry_role_on_offboard` | bool | `true` | onboarding.entry_role_on_offboard | User yang masih di server kembali mendapat roles.entry. |
| `onboarding.reconcile_minutes` | minutes | `10` | onboarding.reconcile_minutes | Cek berkala @backend yang terlewat saat bot offline (0 = mati). |
| `onboarding.admin_notices` | bool | `true` | onboarding.admin_notices | Kabar onboarding ke channel admin. |
| `onboarding.welcome_message` | text | `(bawaan kit)` | onboarding.welcome_message | Pesan sambutan di channel pribadi anggota baru (maks 1900 karakter; placeholder {mention} {display_name} {name} {team_name} {model} {channel} {shared_channel} {admin_channel} {profile}). |
| `onboarding.welcome_back_message` | text | `(bawaan kit)` | onboarding.welcome_back_message | Pesan saat anggota diaktifkan kembali. |
| `server_admin.enabled` | bool | `true` |  | Tool discord_* di #hermes-admin. |
| `server_admin.allow_moderation` | bool | `true` |  | Timeout & kick (ban selalu manual). |
| `server_admin.allow_elevated_permissions` | bool | `false` |  | Izin tinggi (Administrator, Manage Roles, ...) lewat Hermes. |
| `server_admin.read_messages` | bool | `true` |  | discord_read_messages (channel pribadi selalu ditolak). |

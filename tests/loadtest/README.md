# Uji beban kit tim Hermes

Folder ini berisi dua alat untuk `docs/RENCANA-SKALA.md` #12:

| Berkas | Guna | Butuh Hermes? |
|---|---|---|
| `sim.py` + `../test_scale_sim.py` | Simulasi 20 pengguna terhadap kelas plugin yang sebenarnya, dengan jam tiruan | Tidak |
| `fake_llm.py` | Server LLM tiruan OpenAI-compatible untuk uji manual terhadap gateway sungguhan | Ya (server uji) |

Yang **belum** ada: skrip yang mengirim pesan sebagai 20 user lewat Discord tiruan ke gateway
sungguhan. Sampai itu dibuat, uji terhadap gateway sungguhan dilakukan dengan tangan (lihat bagian 2).

## 1. Simulasi (otomatis, tanpa Hermes)

```bash
python -m pytest tests/test_scale_sim.py -q
SIM_SUMMARY=1 python -m pytest tests/test_scale_sim.py -q -s    # cetak ringkasan angka
```

Simulasi memakai `Dispatcher`, `TurnTracker`, antrean adil, `AdaptiveThrottle`, `Budget`, `LlmTracker`,
dan `Monitor` yang asli. Yang tiruan hanya jam, gateway, provider LLM, kolektor pemakaian, dan para
pengirim. Tidak ada `sleep` sungguhan; 45 menit virtual selesai dalam kurang dari satu detik dan hasilnya
sama persis di tiap eksekusi (`random.Random(seed)`).

Skenario bawaan (`sim.Config`):

- 20 anggota; tiap orang mengirim 1–3 pesan beruntun tiap 1–4 menit, kadang pesan susulan ke sesi yang
  sedang jalan;
- satu **pembanjir** membuka 6 thread sekaligus tiap 4 menit;
- satu orang **boros** memakai 100.000 token per panggilan LLM (anggaran harian 1,5 juta);
- **cron** di `team-shared` membuat 3 panggilan LLM per menit tanpa lewat `pre_gateway_dispatch`;
- provider menjawab **429** di atas 11 request paralel, dan pada menit 15–17 hanya sanggup 2 paralel;
- admin mengirim pesan tiap 200 detik.

Invarian yang diperiksa: tidak ada orang melebihi `user_max_concurrent`; sesi anggota tidak pernah
melebihi batas efektif; orang biasa tidak kelaparan karena pembanjir (tunggu terbatas, tidak ada yang
ditolak di luar masa gangguan); pembatas adaptif turun lalu pulih dengan satu kabar per kejadian;
anggaran menghentikan tepat orang yang boros; pelacak plugin (sesi, panggilan LLM) selalu sama dengan
keadaan sebenarnya; semua antrean kosong di akhir. Kelas `TestSimulationControls` menjalankan skenario
yang sama dengan fitur dimatikan untuk membuktikan invarian itu memang dilanggar tanpa fiturnya.

Mengubah skenario: `sim.run_simulation(env, sim.Config(users=40, max_active=12, seed=3))`.

Batas simulasi: urutan dan isi hook meniru dokumentasi Hermes, bukan Hermes yang berjalan. Simulasi
membuktikan logika plugin, bukan perilaku gateway, RAM, atau endpoint LLM sungguhan.

## 2. LLM tiruan untuk gateway sungguhan (manual)

Jalankan di **server uji**, jangan di server tim. Server tiruan tidak memeriksa API key dan hanya
mendengarkan di `127.0.0.1` secara bawaan.

```bash
python tests/loadtest/fake_llm.py --port 9100 --latency 3 --jitter 2 --max-parallel 8 --completion-tokens 400
```

| Opsi | Arti |
|---|---|
| `--latency`, `--jitter` | lama tiap request: `latency` + acak 0..`jitter` detik |
| `--max-parallel` | request paralel yang dilayani; lebihnya dijawab `429` + `Retry-After` (0 = tanpa batas) |
| `--prompt-tokens` | token masukan di `usage` (0 = diperkirakan dari ukuran request) |
| `--completion-tokens` | token keluaran di `usage` |
| `--models` | ID model yang diiklankan di `/v1/models` (bawaan `td-normal,td-simple`) |

Periksa dulu:

```bash
curl -s localhost:9100/v1/models
curl -s localhost:9100/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"td-simple","messages":[{"role":"user","content":"halo"}]}'
curl -s localhost:9100/stats            # requests, ok, rejected_429, inflight, peak_parallel, tokens
curl -s -X POST localhost:9100/stats/reset -d '{}'
```

### Mengarahkan Hermes uji ke server tiruan

1. Di `team.yaml` instalasi uji, arahkan provider ke server tiruan (nama model harus ada di `--models`):

   ```yaml
   llm:
     providers:
       td:
         api: http://127.0.0.1:9100/v1
         key_env: TD_API_KEY
     models:
       td-normal: {provider: "custom:td", model: td-normal}
       td-simple: {provider: "custom:td", model: td-simple}
   scale:
     max_concurrent_sessions: 8
     user_max_concurrent: 2
     max_llm_parallel: 8        # samakan dengan --max-parallel untuk melihat gerbangnya bekerja
     adaptive_throttle: true
     daily_token_budget: 200000 # kecil, supaya anggaran cepat tercapai saat uji
   ```

2. Isi `TD_API_KEY` dengan nilai apa saja di `.env` host (server tiruan mengabaikannya), lalu
   `python hermes_team.py apply` dan restart gateway uji.
3. Bila sandbox Docker memakai jaringan terbatas / proxy egress, pastikan gateway (bukan sandbox) yang
   menghubungi `127.0.0.1:9100`; panggilan LLM dibuat oleh proses gateway di host.
4. Kirim pesan dari beberapa akun uji (atau beberapa thread dari satu akun) dan amati:

   | Yang diuji | Cara memicu | Yang diharapkan |
   |---|---|---|
   | Batas sesi per orang | satu akun, 4 pesan ke 4 thread, `--latency 20` | 2 berjalan, 2 mendapat kabar "batas per orang" |
   | Antrean adil | isi semua slot, lalu akun lain mengirim 1 pesan | akun lain dilayani sebelum pesan ke-2 pembanjir |
   | Pembatas adaptif | `--max-parallel 2`, beberapa akun aktif | kabar 🐢 di #hermes-admin; ✅ beberapa menit setelah `--max-parallel` dinaikkan lagi |
   | Anggaran token | `--completion-tokens 50000`, `daily_token_budget` kecil | peringatan 80%, lalu penolakan; admin dikabari sekali |
   | Metrik | `cat ~/.hermes-team/state/metrics.json` tiap 30 detik | `llm_inflight` ≈ `inflight` di `/stats` |

   Bandingkan `peak_parallel` di `/stats` dengan `llm_inflight_peak_5m` di `metrics.json`: keduanya harus
   berdekatan. Selisih besar berarti ada panggilan yang tidak terlihat hook `pre_api_request`
   (mis. panggilan bantu: judul sesi, kompresi).

### Keterbatasan server tiruan

- Jawaban selalu teks tetap dan tidak pernah meminta tool call, jadi tiap giliran = satu panggilan LLM.
  Sesi panjang (60 langkah tool) dan subagent tidak tertiru; pakai `--latency` besar untuk meniru sesi
  yang lama memegang slot.
- Hanya `/v1/chat/completions` (termasuk `stream: true`) dan `/v1/models`. Mode API lain
  (Responses, Anthropic Messages) tidak ada.
- Token di `usage` adalah angka buatan; cocok untuk menguji anggaran dan `usage_ledger.py`, bukan biaya.

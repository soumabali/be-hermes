"""SATU tabel jenis penolakan pembatas: `kind` → pesan ke pengguna + kunci metrik.

Modul daun (tidak mengimpor modul plugin lain) supaya `limiters.py` (yang membuat `Verdict`), `dispatch.py`
(yang mengirim pesannya) dan `scale.py` (yang menulis `refused` di metrics.json) memakai tabel yang SAMA lewat
impor biasa — dulu `limiters.REFUSAL_TEXT` dan `scale.REFUSAL_KINDS` adalah dua daftar yang dirawat tangan.
"""
from __future__ import annotations

from typing import Any, Optional

MSG_RATE = ("⏳ <@{uid}> kamu mengirim pesan terlalu cepat. Coba lagi dalam ±{wait} detik "
            "(batas: {rate} pesan/menit).")
MSG_QUEUE_FULL = ("😔 <@{uid}> Hermes masih penuh setelah {wait} detik menunggu, jadi pesanmu tidak diproses. "
                  "Coba kirim lagi sebentar lagi.")
MSG_QUEUE_CANCELLED = ("♻️ <@{uid}> Hermes sedang dimuat ulang, jadi pesanmu yang menunggu di antrean tidak "
                       "diproses. Kirim ulang sebentar lagi.")
MSG_USER_LIMIT_FULL = ("😔 <@{uid}> sesimu yang lain masih berjalan setelah {wait} detik menunggu, jadi pesan ini "
                       "tidak diproses. Tunggu sampai selesai (atau `/stop` di thread-nya), lalu kirim lagi.")
MSG_BUDGET_OVER = ("🚫 <@{uid}> jatah token harianmu sudah habis ({used} dari {limit} token), jadi pesan ini "
                   "tidak diproses. Jatah kembali penuh setelah tengah malam; admin tim bisa menambah kuota.")
MSG_BUDGET_TEAM_OVER = ("🚫 <@{uid}> jatah token harian TIM sudah habis ({used} dari {limit} token), jadi pesan ini "
                        "tidak diproses. Jatah kembali penuh setelah tengah malam.")
MSG_REFUSED_GENERIC = ("🚫 <@{uid}> pesanmu tidak diproses karena dibatasi penjaga tim. Coba lagi sebentar lagi "
                       "atau hubungi admin tim.")
OTHER_METRIC = "other"      # jenis metrik untuk penolakan yang `kind`-nya tidak terdaftar (lihat refusal_for)


class Refusal:
    """Satu jenis penolakan: `say_key` = kunci jeda pemberitahuan, `template` = pesan ke pengguna (selalu
    mendapat `uid`), `metric` = kunci di `metrics.json → refused` (None = tidak dihitung)."""
    __slots__ = ("say_key", "template", "metric")

    def __init__(self, say_key: str, template: str, metric: Optional[str]):
        self.say_key, self.template, self.metric = say_key, template, metric


# SATU tabel untuk semua jenis penolakan: verdict.kind → pesan + metrik. Tidak ada daftar kedua: kunci
# `refused` di metrics.json (`scale.refusal_kinds()` → `refusal_metrics()`) diturunkan dari kolom `metric` tabel ini.
REFUSALS: dict[str, Refusal] = {
    "budget_over": Refusal("budget", MSG_BUDGET_OVER, "budget"),
    "budget_team_over": Refusal("budget", MSG_BUDGET_TEAM_OVER, "budget"),
    "rate": Refusal("rate", MSG_RATE, "rate"),
    "user_limit_full": Refusal("user-limit-full", MSG_USER_LIMIT_FULL, "user_concurrent"),
    "queue_full": Refusal("queue-full", MSG_QUEUE_FULL, "queue_full"),
    "queue_cancelled": Refusal("queue-cancelled", MSG_QUEUE_CANCELLED, None),
}


def register_refusal(kind: str, template: str, metric: Optional[str], say_key: str = "") -> Refusal:
    """Daftarkan jenis penolakan baru (pembatas tambahan). `template` wajib memuat `{uid}`."""
    if "{uid}" not in template:
        raise ValueError(f"templat penolakan '{kind}' harus memuat {{uid}}")
    REFUSALS[kind] = Refusal(say_key or kind.replace("_", "-"), template, metric)
    return REFUSALS[kind]


def refusal_metrics() -> tuple[str, ...]:
    """Kunci metrik penolakan yang terdaftar, berurutan stabil dan tanpa duplikat."""
    ordered = ("rate", "queue_full", "budget", "user_concurrent")          # urutan lama di metrics.json
    found = {r.metric for r in REFUSALS.values() if r.metric}
    return tuple(k for k in ordered if k in found) + tuple(sorted(found - set(ordered)))


def refusal_for(verdict: Any) -> Refusal:
    """Entri tabel untuk `verdict.kind`. Jenis yang TIDAK terdaftar tetap ditolak: pesan umum, dan dihitung
    di metrik `verdict.metric` (bila pembatasnya menyebut satu) atau `other` — tidak pernah KeyError (dulu
    galat itu ditelan dan pesannya malah diteruskan)."""
    kind = getattr(verdict, "kind", None)
    known = REFUSALS.get(kind) if isinstance(kind, str) else None
    if known is not None:
        return known
    metric = getattr(verdict, "metric", None)
    metric = metric if isinstance(metric, str) and metric else OTHER_METRIC
    return Refusal(f"refused-{str(kind)[:40]}", MSG_REFUSED_GENERIC, metric)

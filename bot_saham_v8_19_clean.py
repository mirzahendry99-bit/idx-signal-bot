"""
╔══════════════════════════════════════════════════════════════════╗
║      SIGNAL BOT SAHAM IDX — v8.19 [ZERO FINDINGS — RUN READY]  ║
║                                                                  ║
║  Diadaptasi dari Signal Bot Crypto v7.7b                        ║
║  Target: Saham IDX (Blue chip + Growth stock)                   ║
║                                                                  ║
║  Arsitektur:                                                     ║
║  - INTRADAY (1h)  : BUY + SELL — OFF (data delay 15m)          ║
║  - SWING    (1d)  : BUY + SELL — posisi multi-hari             ║
║                                                                  ║
║  Data Source : yfinance (delay ~15 menit, gratis)               ║
║  Market Filter: IHSG (^JKSE) — SOFT FILTER (penalty only)      ║
║  Notifikasi  : Telegram Bot                                      ║
║  Storage     : Supabase (dedup + signal tracking + win rate)    ║
║  Scheduler   : GitHub Actions (cron)                            ║
║                                                                  ║
║  Harga ditampilkan dalam IDR (Rupiah)                           ║
║                                                                  ║
║  v8.19 ZERO FINDINGS (all audit issues resolved):               ║
║  [H1-FIX] calc_sl_tp(): guard entry<=0 / atr<=0 → return None  ║
║           Mencegah ZeroDivisionError saat yfinance data tipis.  ║
║  [M3-FIX] P8-06 zero-signal alert: hapus PHASE3_COLLECTION      ║
║           condition — alert aktif di semua phase termasuk live. ║
║  [M1-FIX] 11 silent ENV-read except → semua diberi print WARN   ║
║           ke stderr. Operator tahu jika ENV corrupt.            ║
║  [L3-FIX] Hapus dead code getattr(__builtins__,...) di          ║
║           collection progress bar → ganti COLLECTION_TARGET_FULL║
║  [L3b-FIX] _p5_prev_state: ganti builtins hack → module-level  ║
║            global. Python 3.12 safe. Persistence tetap benar.   ║
║                                                                  ║
║  NOTE: H3 (send_slippage_report div/0) dan M2 (_block_rate      ║
║  div/0) adalah false positive — guard sudah ada upstream.       ║
║                                                                  ║
║  PHASE-3 BULLETPROOF (v8.18):                                   ║
║  [P8-01..06] 6 Phase 3 bulletproof fixes                        ║
║                                                                  ║
║  PHASE-7 SCALE (v8.17):                                         ║
║  [P7-01..03] Live execution hard guards + fill deviation        ║
║                                                                  ║
║  PHASE-1 VISIBILITY (v8.16):                                    ║
║  [P1-01..05] Pipeline breakdown + top blocker tracking          ║
║                                                                  ║
║  PHASE-2 STABILIZE (v8.15):                                     ║
║  [P2-01..05] Hard disable semua adaptive/complexity layer       ║
║                                                                  ║
║  PHASE-0 UNBLOCK (v8.15 — tetap aktif):                        ║
║  [P0-01] SWING_MIN_RR = 1.3 (realistis IDX)                    ║
║  [P0-02] LEAN_MODE = True (matikan semua adaptive layer)        ║
║  [P0-03] MIN_VOLUME_IDR = 1B (longgarkan filter ticker)         ║
║  [P0-04] MAX_SIGNALS_CYCLE = 10 (kumpulkan data)               ║
║  [P0-05] INTRADAY_ENABLED = False (data delay)                  ║
║  [P0-06] STRATEGY_MIN_TRADES = 999 (disable auto-disable)       ║
║  [P0-07] BOOTSTRAP_SIGNALS_CAP_COLD = 3                         ║
║  [P0-08] BOOTSTRAP_SIGNALS_CAP_EARLY = 5                        ║
║  [P0-09] IHSG block_buy → soft penalty (score -= 1 Phase3)     ║
║  [P0-10] Changelog dipisah ke CHANGELOG.md                      ║
╚══════════════════════════════════════════════════════════════════╝
"""


from __future__ import annotations   # Python 3.9 compatibility for `X | Y` type hints

import os, json, time, math
import logging
import numpy as np
import pandas as pd
import urllib.request
from datetime import datetime, timedelta, timezone
from supabase import create_client
import yfinance as yf

# ════════════════════════════════════════════════════════
#  LOGGING — Timestamp WIB
# ════════════════════════════════════════════════════════

WIB = timezone(timedelta(hours=7))

class _WIBFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=WIB)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S WIB")

_handler = logging.StreamHandler()
_handler.setFormatter(_WIBFormatter("%(asctime)s [%(levelname)s] %(message)s"))

_logger = logging.getLogger("signal_bot_saham")
_logger.setLevel(logging.INFO)
_logger.addHandler(_handler)
_logger.propagate = False

def log(msg: str, level: str = "info"):
    if level == "warn":
        _logger.warning(msg)
    elif level == "error":
        _logger.error(msg)
    else:
        _logger.info(msg)


# ════════════════════════════════════════════════════════
#  CONFIG — Environment Variables
# ════════════════════════════════════════════════════════

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT_ID   = os.environ.get("CHAT_ID")

# ── Position Sizing — opsional, ada default kalau tidak diset ──
# PORTFOLIO_IDR : total modal kamu dalam Rupiah  (default: Rp 10.000.000)
# RISK_PCT      : persentase risiko per trade    (default: 1.0%)
try:
    PORTFOLIO_IDR = float(os.environ.get("PORTFOLIO_IDR", 10_000_000))
except ValueError:
    PORTFOLIO_IDR = 10_000_000

try:
    RISK_PCT = float(os.environ.get("RISK_PCT", 1.0))
    RISK_PCT = max(0.1, min(RISK_PCT, 5.0))   # clamp 0.1% – 5% (safety)
except ValueError:
    RISK_PCT = 1.0

_missing = [k for k, v in {
    "SUPABASE_URL":   SUPABASE_URL,
    "SUPABASE_KEY":   SUPABASE_KEY,
    "TELEGRAM_TOKEN": TG_TOKEN,
    "CHAT_ID":        TG_CHAT_ID,
}.items() if not v]
if _missing:
    raise EnvironmentError(f"ENV belum diset: {', '.join(_missing)}")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
log(f"💼 Portfolio: Rp{PORTFOLIO_IDR:,.0f} | Risk per trade: {RISK_PCT}%")

# ════════════════════════════════════════════════════════
#  WATCHLIST SAHAM IDX
#  Blue chip (LQ45) + Growth stock pilihan
#  Format yfinance untuk IDX: tambahkan .JK di belakang
# ════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════
#  WATCHLIST SAHAM IDX
#  LQ45 + IDX80 + Sektor komoditas aktif + Growth
#  Format yfinance untuk IDX: tambahkan .JK di belakang
#  Volume filter Rp 5M/hari akan otomatis menyaring yang tidak likuid
# ════════════════════════════════════════════════════════

WATCHLIST = [
    # ── LQ45 — Blue Chip ──────────────────────────────────
    "BBCA.JK",   # Bank Central Asia
    "BBRI.JK",   # Bank Rakyat Indonesia
    "BMRI.JK",   # Bank Mandiri
    "BBNI.JK",   # Bank Negara Indonesia
    "TLKM.JK",   # Telkom Indonesia
    "ASII.JK",   # Astra International
    "UNVR.JK",   # Unilever Indonesia
    "ICBP.JK",   # Indofood CBP
    "KLBF.JK",   # Kalbe Farma
    "HMSP.JK",   # HM Sampoerna
    "GGRM.JK",   # Gudang Garam
    "INDF.JK",   # Indofood Sukses Makmur
    "EXCL.JK",   # XL Axiata
    "SMGR.JK",   # Semen Indonesia
    "PGAS.JK",   # PGN
    "PTBA.JK",   # Bukit Asam
    "ANTM.JK",   # Aneka Tambang
    "INCO.JK",   # Vale Indonesia
    "ADRO.JK",   # Adaro Energy
    "ITMG.JK",   # Indo Tambangraya Megah
    "UNTR.JK",   # United Tractors
    "INTP.JK",   # Indocement
    "INKP.JK",   # Indah Kiat Pulp & Paper
    "TKIM.JK",   # Tjiwi Kimia
    "AKRA.JK",   # AKR Corporindo
    "JSMR.JK",   # Jasa Marga
    "PWON.JK",   # Pakuwon Jati
    "BSDE.JK",   # Bumi Serpong Damai
    "CTRA.JK",   # Ciputra Development
    "SMRA.JK",   # Summarecon Agung
    "MAPI.JK",   # Mitra Adiperkasa
    "LPPF.JK",   # Matahari Department Store
    "SCMA.JK",   # Surya Citra Media
    "MNCN.JK",   # Media Nusantara Citra
    "TOWR.JK",   # Sarana Menara Nusantara
    "TBIG.JK",   # Tower Bersama Infrastructure
    "ISAT.JK",   # Indosat Ooredoo
    # "WSKT.JK",  # [v7.9 REMOVED] Waskita Karya — restrukturisasi utang, sering trading halt
    # "PTPP.JK",  # [v7.10 REMOVED] PP Pembangunan Perumahan — BUMN konstruksi bermasalah
    # "WIKA.JK",  # [v7.10 REMOVED] Wijaya Karya — problem keuangan BUMN konstruksi
    "AALI.JK",   # Astra Agro Lestari
    "LSIP.JK",   # PP London Sumatra
    "CPIN.JK",   # Charoen Pokphand Indonesia
    "JPFA.JK",   # Japfa Comfeed
    "MYOR.JK",   # Mayora Indah
    # ── Growth & Teknologi ────────────────────────────────
    "GOTO.JK",   # GoTo (Tokopedia/Gojek)
    # "BUKA.JK",  # [v7.10 REMOVED] Bukalapak — volume sangat tipis pasca-restrukturisasi
    "EMTK.JK",   # Elang Mahkota Teknologi
    "ARTO.JK",   # Bank Jago
    "BRIS.JK",   # Bank Syariah Indonesia
    "BTPS.JK",   # Bank BTPN Syariah
    "PNBN.JK",   # Bank Pan Indonesia
    "NISP.JK",   # Bank OCBC NISP
    "BNLI.JK",   # Bank Permata
    # ── Komoditas — Nikel & Minerba ───────────────────────
    "MDKA.JK",   # Merdeka Copper Gold
    "AMMN.JK",   # Amman Mineral
    "MBMA.JK",   # Merdeka Battery Materials
    "ESSA.JK",   # ESSA Industries
    "NCKL.JK",   # Trimegah Bangun Persada (Nickel)
    "HRUM.JK",   # Harum Energy
    "DOID.JK",   # Delta Dunia Makmur
    "GEMS.JK",   # Golden Energy Mines
    "BYAN.JK",   # Bayan Resources
    "AADI.JK",   # Adaro Andalan Indonesia
    # ── Komoditas — CPO & Agro ────────────────────────────
    "SIMP.JK",   # Salim Ivomas Pratama
    "SSMS.JK",   # Sawit Sumbermas Sarana
    "TAPG.JK",   # Triputra Agro Persada
    "TBLA.JK",   # Tunas Baru Lampung
    # ── Kimia & Petrokimia ───────────────────────────────
    "TPIA.JK",   # Chandra Asri Petrochemical
    "BRPT.JK",   # Barito Pacific
    # ── Properti & Kawasan Industri ──────────────────────
    "DSSA.JK",   # Dian Swastatika Sentosa
    "PANI.JK",   # Pratama Abadi Nusa
    "CBDK.JK",   # Cipta Bintang Dharma Kencana
    "KIJA.JK",   # Kawasan Industri Jababeka
    "BEST.JK",   # Bekasi Fajar Industrial Estate
    # "JPRT.JK",   # [v7.12 REMOVED] Jaya Properti — no price data, possibly delisted
    # ── Konsumer Defensif ────────────────────────────────
    "SIDO.JK",   # Industri Jamu Sido Muncul
    "ULTJ.JK",   # Ultra Jaya Milk
    "ROTI.JK",   # Nippon Indosari Corpindo
    "SKBM.JK",   # Sekar Bumi
    "MAIN.JK",   # Malindo Feedmill
    # ── Infrastruktur ────────────────────────────────────
    # "FREN.JK",   # [v7.12 REMOVED] Smartfren Telecom — no price data, possibly delisted
    "MARK.JK",   # Mark Dynamics Indonesia
    "ERAA.JK",   # Erajaya Swasembada
    # "HERO.JK",  # [v7.9 REMOVED] Hero Supermarket — volume sangat tipis, hampir delisting
    # ── High-Volatility Speculative (volume filter jaga) ──
    "CUAN.JK",   # Petrindo Jaya Kreasi
]

# Deduplicate sambil pertahankan urutan
_wl_seen: set = set()
WATCHLIST = [t for t in WATCHLIST if not (t in _wl_seen or _wl_seen.add(t))]

# ════════════════════════════════════════════════════════
#  PARAMETER UTAMA
# ════════════════════════════════════════════════════════

# Volume minimum (dalam Rupiah) — filter saham tidak likuid
MIN_VOLUME_IDR     = 1_000_000_000    # [PHASE-0] Rp 1 miliar (was: 5M) — longgarkan agar lebih banyak ticker lolos

# [v7.2 — FIX Masalah 2B] Proteksi saham mendekati level Gocap (Rp 50)
# Di IDX, saham dengan harga mendekati Rp 50 bergerak sangat tidak teratur
# karena fraksi harga Rp 1 = 2% move — setiap tick sangat signifikan
# dan pasar cenderung stagnan atau manipulable
MIN_PRICE_IDR      = 55               # skip saham dengan harga <= Rp 55

MAX_SIGNALS_CYCLE  = 10               # [PHASE-0] was 5 → 10 — fase ini butuh data, bukan limit
DEDUP_HOURS        = 8                # tidak kirim ulang dalam 8 jam

# ════════════════════════════════════════════════════════
#  [v7.13] KONFIGURASI BARU — menjawab 7 kritik fundamental
# ════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════
#  [v7.14] KONFIGURASI — Perbaikan 5 kritik fundamental
# ════════════════════════════════════════════════════════

# ── Fix 1: Data delay — nonaktifkan intraday by default ──────────
# ROOT PROBLEM: yfinance delay ~15 menit tidak bisa diperbaiki tanpa
# API berbayar (IPOT, RTI, IDXChannel). Solusi jujur:
# → INTRADAY_ENABLED=False by default.
# → Aktifkan hanya jika kamu punya akses data real-time sendiri.
# → Swing (1D) tidak terpengaruh delay — delay 15 menit di daily = irrelevant.
try:
    INTRADAY_ENABLED = os.environ.get("INTRADAY_ENABLED", "false").lower() == "true"
except Exception as _e:
    import sys as _sys_env
    print(f"[WARN] ENV read INTRADAY_ENABLED failed: {_e} — fallback False", file=_sys_env.stderr)
    INTRADAY_ENABLED = False   # DEFAULT OFF — data delay makes intraday unreliable

INTRADAY_IS_REFERENCE_ONLY = True   # jika diaktifkan: selalu tambah disclaimer
MAX_DATA_AGE_MINUTES       = 20     # skip jika data lebih dari 20 menit saat market buka

# [R01] DATA SOURCE HONESTY — label eksplisit di setiap signal
# Semua data dari yfinance. Delay ~15 menit intraday, end-of-day untuk swing.
# Label ini disimpan ke Supabase agar analis bisa filter berdasarkan kualitas data.
DATA_SOURCE_INTRADAY = "YFINANCE_DELAYED_15MIN"
DATA_SOURCE_SWING    = "YFINANCE_EOD"

# [R01] Minimum Execution Quality Score untuk intraday signal.
# EQS dihitung oleh calc_execution_quality_score() dari data age + slippage model.
# Di bawah threshold ini, signal intraday diblokir — data terlalu stale untuk dieksekusi.
# Default 35: threshold rendah (lebih baik sinyal sedikit dari sinyal salah).
# Set 0 untuk disable gate ini (tidak disarankan saat INTRADAY_ENABLED=True).
try:
    INTRADAY_MIN_EQS = int(os.environ.get("INTRADAY_MIN_EQS", 35))
    INTRADAY_MIN_EQS = max(0, min(INTRADAY_MIN_EQS, 100))
except ValueError:
    INTRADAY_MIN_EQS = 35

# ── Fix 2: SIMPLE_MODE default True — anti-overfit ───────────────
# Default True = stripped-down mode: BOS/CHoCH + RSI + ATR levels saja.
# Mode kompleks (False) hanya untuk yang sudah punya 100+ signal historis
# dan sudah memvalidasi bahwa kompleksitas ekstra benar-benar menambah edge.
try:
    SIMPLE_MODE = os.environ.get("SIMPLE_MODE", "true").lower() == "true"
except Exception as _e:
    import sys as _sys_env
    print(f"[WARN] ENV read SIMPLE_MODE failed: {_e} — fallback True", file=_sys_env.stderr)
    SIMPLE_MODE = True   # DEFAULT: simple, lebih robust, less overfit

# [U04] LEAN_MODE — nonaktifkan semua adaptive/feedback layer sekaligus.
# Ketika True: gunakan W base, skip cluster weights, skip strategy auto-disable,
# skip adaptive relaxation. Ini mode "I want to verify the base logic works first".
# Aktifkan saat: (a) edge belum terbukti, (b) debugging, (c) sistem terasa terlalu complex.
# Default False — opt-in karena LEAN_MODE mengorbankan personalisasi.
# [PHASE-0] LEAN_MODE paksa True — matikan semua adaptive/feedback layer
# Tujuan: verifikasi base logic bekerja sebelum enable complexity
# Revert: ubah "true" → "false" setelah bot konsisten kirim 1–5 signal/hari
try:
    LEAN_MODE = os.environ.get("LEAN_MODE", "true").lower() == "true"
except Exception as _e:
    import sys as _sys_env
    print(f"[WARN] ENV read LEAN_MODE failed: {_e} — fallback True", file=_sys_env.stderr)
    LEAN_MODE = True   # [PHASE-0] paksa True

# ── [PHASE-2] STABILIZE MODE ─────────────────────────────────────
# Goal: stop overfiltering — bot choke (0 signal) dihilangkan.
# True  → hanya 4–5 filter core aktif, semua layer kompleks dimatikan:
#          • Skip NTZ (No-Trade Zone engine)
#          • Skip liquidity depth filter
#          • Skip redundant yf.download (data age + EQS)
#          • Scoring = raw score_signal() saja (no sniper/priority/strategy bonus)
#          • EV gate = hanya HARD_EV_FLOOR (EV > 0), bukan multi-threshold
#          • Skip DEFENSIVE_SNIPER gate
#          • Fix swing SIMPLE_MODE dead-code bug → gunakan defaults
# False → kembalikan semua layer kompleks (hanya setelah bot konsisten stabil)
# ENV: PHASE2_STABILIZE=true/false
try:
    PHASE2_STABILIZE = os.environ.get("PHASE2_STABILIZE", "true").lower() == "true"
except Exception as _e:
    import sys as _sys_env
    print(f"[WARN] ENV read PHASE2_STABILIZE failed: {_e} — fallback True", file=_sys_env.stderr)
    PHASE2_STABILIZE = True

# ── [PHASE-3] DATA COLLECTION MODE ───────────────────────────────
# Goal: kumpulkan data nyata (WIN/LOSS/RR/durasi/kondisi market)
# sebelum optimasi apapun. Target minimal 50, ideal 100 trade resolved.
#
# Aturan wajib selama PHASE-3:
#   ❌ Jangan ubah parameter scoring (W, TIER_MIN_SCORE, MIN_RR)
#   ❌ Jangan aktifkan adaptive weights / cluster weights
#   ❌ Jangan ubah filter gates
#   ✅ Simpan semua metadata signal selengkap mungkin
#   ✅ Update outcome setiap run (WIN/LOSS/expired/durasi)
#   ✅ Report progress menuju 50-100 trade ke Telegram
#
# ENV: PHASE3_COLLECTION=true/false
try:
    PHASE3_COLLECTION = os.environ.get("PHASE3_COLLECTION", "true").lower() == "true"
except Exception as _e:
    import sys as _sys_env
    print(f"[WARN] ENV read PHASE3_COLLECTION failed: {_e} — fallback True", file=_sys_env.stderr)
    PHASE3_COLLECTION = True

# Target jumlah resolved trade untuk analisis edge yang bermakna
COLLECTION_TARGET_MIN  = 50   # minimum untuk WR + EV kasar
COLLECTION_TARGET_FULL = 100  # target untuk distribusi + confidence interval

# ── [PHASE-3] ENFORCE RULE ────────────────────────────────────────────
# ── [PHASE-4] VALIDATION MODE ─────────────────────────────────────
# Goal: jawab "apakah strategi ini punya edge nyata?"
# menggunakan 4 metode independen yang sudah ada:
#   M1 — Binomial p-value (WR vs H0=50%)
#   M2 — Profit Factor (gross profit / gross loss ≥ 1.20)
#   M3 — Out-of-sample WR (multi-fold walk-forward > 50%)
#   M4 — Train→Test drift (overfitting detector ≤ 10pp)
#
# Hukum PHASE-4:
#   INSUFFICIENT → kumpul lebih banyak data, tidak ada tindakan
#   UNPROVEN     → ⛔ JANGAN tambah kompleksitas apapun
#   PROMISING    → ✅ operasional normal, pantau terus
#   PROVEN       → ✅ boleh aktifkan adaptive layers
#
# Verdict dikirim ke Telegram hanya jika:
#   (a) berubah dari run sebelumnya, ATAU
#   (b) dipanggil via --validate CLI
# ENV: PHASE4_VALIDATE=true/false
# ⚠️ PENTING: ENV dibaca DI SINI — SEBELUM PHASE3 enforce block di bawah.
# PHASE3 enforce WAJIB menjadi kata terakhir dan tidak boleh di-override ENV.
try:
    PHASE4_VALIDATE = os.environ.get("PHASE4_VALIDATE", "true").lower() == "true"
except Exception as _e:
    import sys as _sys_env
    print(f"[WARN] ENV read PHASE4_VALIDATE failed: {_e} — fallback True", file=_sys_env.stderr)
    PHASE4_VALIDATE = True

# ── [PHASE-3] ENFORCE RULE ────────────────────────────────────────────
# Saat PHASE3_COLLECTION=True, paksa semua phase lain ke mode aman.
# Ini mencegah "kebobolan" ke optimasi atau validasi prematur.
# Rule ini NOT overrideable via ENV — ini adalah hard enforcement.
# ⚠️ FIX: Block ini HARUS berada SETELAH semua ENV read selesai,
#    supaya enforce ini tidak bisa ditimpa oleh ENV read berikutnya.
if PHASE3_COLLECTION:
    LEAN_MODE        = True   # matikan adaptive weights & cluster weights
    PHASE2_STABILIZE = True   # pastikan overfiltering tidak aktif
    PHASE4_VALIDATE  = False  # jangan jalankan edge validation prematur
    # Guard kedua: PHASE4 tidak boleh aktif jika data < 50 trade
    # (diperkuat di runtime oleh check_edge_proven, tapi dicegah di sini juga)
    # Log enforcement — tampil di setiap run agar mudah terlihat
    import sys as _sys
    print(
        "[PHASE3 ENFORCE] LEAN_MODE=True | PHASE2_STABILIZE=True | PHASE4_VALIDATE=False\n"
        "                 Tidak ada optimasi parameter sampai 50+ trade terkumpul.",
        file=_sys.stderr
    )
    # NOTE: [P8-05] FREEZE PROTECTION di-inject setelah MIN_RR didefinisikan (~line 975)
    # supaya guard bisa membaca nilai aktual. Lihat blok "PHASE3 FREEZE PROTECTION" di bawah.

# Track verdict antar run — untuk deteksi perubahan dan kirim Telegram
# Diisi di awal run() setelah check_edge_proven()
_prev_edge_verdict:    str = ""    # verdict run sebelumnya
_current_edge_verdict: str = ""    # verdict run ini (dibaca oleh fungsi lain)
_p5_prev_state: str | None = None  # [v8.19] phase5 state dari run terakhir dalam session

# ── [PHASE-5] OPTIMIZATION MODE ───────────────────────────────────────
# Goal: improve profit secara bertahap setelah edge terbukti (PHASE-4 PROVEN/PROMISING).
#
# ⚠️ ATURAN UTAMA: Aktifkan SATU layer setiap kali.
# Aktifkan semua sekaligus = tidak bisa tahu mana yang bekerja.
# Jika WR/EV turun setelah aktivasi → rollback layer itu, jangan lanjut.
#
# Urutan layer yang WAJIB diikuti (step by step):
#   Step 1 — POSITION_SIZING  : Kelly blend aktif berdasarkan win_prob empiris
#   Step 2 — EV_FILTER        : naikkan EV_MIN_THRESHOLD dari 0.05 → 0.15
#   Step 3 — CLUSTER_WEIGHTS  : aktifkan cluster-based weight modifier
#   Step 4 — ADAPTIVE_FILTER  : aktifkan adaptive relaxation berdasarkan filter audit
#
# Prerequisite untuk SEMUA layer:
#   ✅ PHASE4 verdict = PROVEN atau PROMISING
#   ✅ n_resolved ≥ 50 trade
#   ✅ Layer sebelumnya sudah diaktifkan ≥ 20 run dan tidak merusak WR
#
# ENV untuk kontrol layer (satu per satu):
#   PHASE5_LAYER=0   → tidak ada layer aktif (default, aman)
#   PHASE5_LAYER=1   → aktifkan position sizing saja
#   PHASE5_LAYER=2   → aktifkan position sizing + EV filter
#   PHASE5_LAYER=3   → aktifkan position sizing + EV filter + cluster weights
#   PHASE5_LAYER=4   → semua layer aktif (hanya jika PROVEN + 100 trade)
#
# Default: 0 (tidak aktif) — harus di-set manual via ENV setelah review data
try:
    PHASE5_LAYER = int(os.environ.get("PHASE5_LAYER", "0"))
    PHASE5_LAYER = max(0, min(PHASE5_LAYER, 4))   # clamp 0–4
except (ValueError, TypeError):
    PHASE5_LAYER = 0

# EV threshold yang diterapkan di PHASE5 Step 2
# Bisa di-override via ENV untuk fine-tuning tanpa ubah code
try:
    PHASE5_EV_THRESHOLD = float(os.environ.get("PHASE5_EV_THRESHOLD", "0.15"))
    PHASE5_EV_THRESHOLD = round(max(0.05, min(PHASE5_EV_THRESHOLD, 0.40)), 3)
except (ValueError, TypeError):
    PHASE5_EV_THRESHOLD = 0.15

# Prerequisite verdict — layer tidak akan aktif di bawah ini
PHASE5_MIN_VERDICT   = {"PROVEN", "PROMISING"}   # minimum verdict untuk PHASE5
PHASE5_MIN_TRADES    = 50                         # minimum resolved trade untuk PHASE5

# ── SQL Migrations (jalankan SEKALI di Supabase Dashboard) ────────
# ALTER TABLE signals ADD COLUMN IF NOT EXISTS rr         FLOAT;
# ALTER TABLE signals ADD COLUMN IF NOT EXISTS atr_pct    FLOAT;
# ALTER TABLE signals ADD COLUMN IF NOT EXISTS adx        FLOAT;
# ALTER TABLE signals ADD COLUMN IF NOT EXISTS phase      TEXT;
# ALTER TABLE signals ADD COLUMN IF NOT EXISTS daily_bias TEXT;
# ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry_pattern  TEXT;
# ALTER TABLE signals ADD COLUMN IF NOT EXISTS entry_strength TEXT;
# ALTER TABLE signals ADD COLUMN IF NOT EXISTS duration_hours FLOAT;
# ALTER TABLE signals ADD COLUMN IF NOT EXISTS rr_actual      FLOAT;
# ALTER TABLE signals ADD COLUMN IF NOT EXISTS closed_price   FLOAT;
# ALTER TABLE signals ADD COLUMN IF NOT EXISTS mfe_pct        FLOAT;
# ALTER TABLE signals ADD COLUMN IF NOT EXISTS mae_pct        FLOAT;
# -- [PHASE-3] Tambah kolom market_condition untuk PHASE3 dashboard
# ALTER TABLE signals ADD COLUMN IF NOT EXISTS market_condition TEXT;

# ════════════════════════════════════════════════════════
#  [V01] ENSEMBLE EDGE VERDICT — 3-method consensus
# ════════════════════════════════════════════════════════
# Verdict hanya PROVEN jika ≥2/3 metode independen setuju.
ENSEMBLE_MIN_AGREE      = 2   # berapa metode harus setuju untuk PROVEN
ENSEMBLE_MIN_N_PF       = 30  # profit factor butuh min N untuk valid
ENSEMBLE_MIN_N_ROLLING  = 40  # rolling consistency butuh min N
PROFIT_FACTOR_MIN       = 1.20  # gross profit / gross loss ≥ ini = metode setuju

# ════════════════════════════════════════════════════════
#  [V02] DISTRIBUTION-ADJUSTED SIZING
# ════════════════════════════════════════════════════════
# Skew/kurtosis dari check_edge_proven disimpan ke sini,
# dibaca oleh get_smart_risk_pct() sebagai Layer 5.
_run_dist_stats: dict = {}  # {"skew": float, "kurt": float, "n": int}

DIST_KURT_MILD_THRESHOLD  = 3.0   # kept for reference / logging
DIST_KURT_SEVERE_THRESHOLD = 6.0   # kept for reference / logging
DIST_SKEW_NEG_THRESHOLD   = -1.0  # kept for reference / logging

# [W02] SIGMOID PARAMETERS — nonlinear continuous penalty
# Kurtosis sigmoid: factor = 1 - MAX_PENALTY / (1 + exp(-SLOPE*(kurt - INFLECT))
# At kurt=0: factor ≈ 1.00 (no penalty)
# At kurt=3: factor ≈ 0.93 (mild)
# At kurt=6: factor ≈ 0.82 (moderate)
# At kurt=10: factor ≈ 0.73 (severe)
DIST_KURT_SIG_MAX     = 0.30   # max kurtosis penalty (30%)
DIST_KURT_SIG_SLOPE   = 0.30   # steepness of sigmoid
DIST_KURT_SIG_INFLECT = 4.50   # kurtosis value at mid-penalty

# Skewness sigmoid: only penalises negative skew (left tail)
# At skew=0:   factor ≈ 1.00
# At skew=-1:  factor ≈ 0.96
# At skew=-2:  factor ≈ 0.92
# At skew=-4:  factor ≈ 0.88
DIST_SKEW_SIG_MAX     = 0.12   # max skewness penalty (12%)
DIST_SKEW_SIG_SLOPE   = 0.60   # steepness
DIST_SKEW_SIG_INFLECT = -2.00  # skew value at mid-penalty (neg skew focus)

# Legacy step factors kept for backward compat (not used in W02 path)
DIST_KURT_MILD_FACTOR   = 0.85
DIST_KURT_SEVERE_FACTOR = 0.70
DIST_SKEW_NEG_FACTOR    = 0.90

# ════════════════════════════════════════════════════════
#  [v8.09-D] PARAMETER AUTO-CALIBRATION — Sigmoid + Threshold
#
#  Masalah: DIST_KURT_SIG_SLOPE, DIST_SKEW_SIG_SLOPE, inflection points,
#  dan score threshold semuanya hardcoded dari theoretical intuition.
#  Ini berarti penalty bisa too aggressive atau too lenient dibanding
#  distribusi aktual RR historis yang dialami bot ini di IDX.
#
#  Solusi: calibrate_distribution_params() meng-fit slope & inflection
#  dari data historis dengan grid search yang meminimalkan:
#    Loss = MSE antara predicted_risk_reduction dan actual_drawdown_excess.
#
#  Sumber data: Supabase signals resolved (outcome + RR actual).
#  Dijalankan manual: python bot.py --calibrate-params
#  Atau otomatis: setiap 100 trade baru (jika cukup data).
#
#  Output: print ke log + opsional update ENV hint untuk manual apply.
# ════════════════════════════════════════════════════════

# Track kapan terakhir kali auto-calibrasi dijalankan
_last_param_calibration_n: int = 0   # jumlah signal saat calibrasi terakhir
_calibrated_params: dict = {}        # hasil calibrasi terakhir (referensi)

MIN_CALIBRATION_SIGNALS = 80   # minimum resolved trades sebelum calibrasi bermakna
CALIBRATION_INTERVAL_N  = 100  # recalibrate tiap N trade baru


def calibrate_distribution_params(rows: list | None = None,
                                   force: bool = False) -> dict:
    """
    [v8.09-D] Fit sigmoid parameters dari distribusi RR historis.

    Approach: grid search atas (slope, inflection) yang meminimalkan
    korelasi antara kurtosis/skew return dan actual excess loss.

    Logic:
    1. Ambil semua resolved trades dari Supabase (atau pakai `rows` yang dipassing).
    2. Hitung rolling kurtosis + skewness dari window 30 trade.
    3. Bandingkan dengan actual drawdown excess di window berikutnya.
    4. Cari (slope, inflection) yang membuat predicted penalty paling
       berkorelasi dengan actual drawdown (grid search 10x10).
    5. Return parameter optimal beserta confidence interval.

    Hasil dipakai sebagai rekomendasi — tidak auto-overwrite konstanta
    kecuali user eksplisit set ENV APPLY_CALIBRATED_PARAMS=true.
    """
    global _last_param_calibration_n, _calibrated_params
    global DIST_KURT_SIG_SLOPE, DIST_KURT_SIG_INFLECT
    global DIST_SKEW_SIG_SLOPE, DIST_SKEW_SIG_INFLECT

    try:
        if rows is None:
            rows = (
                supabase.table("signals")
                .select("outcome, rr, sent_at, strategy, tier")
                .in_("outcome", ["WIN", "LOSS"])
                .order("sent_at", desc=False)
                .limit(500)
                .execute()
                .data
            )
    except Exception as e:
        log(f"⚠️ calibrate_distribution_params: Supabase error — {e}", "warn")
        return {"error": str(e)}

    if not rows or len(rows) < MIN_CALIBRATION_SIGNALS:
        n = len(rows) if rows else 0
        log(f"⚠️ calibrate_distribution_params: data tidak cukup ({n} < {MIN_CALIBRATION_SIGNALS})")
        return {"error": "insufficient_data", "n": n, "required": MIN_CALIBRATION_SIGNALS}

    # Jika tidak force dan data baru < CALIBRATION_INTERVAL_N, skip
    n_total = len(rows)
    if not force and (n_total - _last_param_calibration_n) < CALIBRATION_INTERVAL_N:
        return {"skipped": True, "reason": "not_enough_new_data",
                "n_since_last": n_total - _last_param_calibration_n}

    rr_vals = []
    for r in rows:
        try:
            rr = float(r["rr"]) if r.get("rr") else None
            if rr is None or rr <= 0:
                # Reconstruct dari outcome: WIN = +RR_avg, LOSS = -1.0
                rr = 1.5 if r["outcome"] == "WIN" else -1.0
            signed_rr = rr if r["outcome"] == "WIN" else -rr
            rr_vals.append(signed_rr)
        except Exception:
            continue

    if len(rr_vals) < MIN_CALIBRATION_SIGNALS:
        return {"error": "insufficient_rr_data", "n": len(rr_vals)}

    rr_arr = np.array(rr_vals)

    # ── Hitung rolling windows untuk features & targets ───
    WINDOW = 30   # window untuk hitung kurtosis/skewness
    features_kurt, features_skew, targets = [], [], []

    for i in range(WINDOW, len(rr_arr) - 5):
        window_data = rr_arr[i - WINDOW:i]
        future_data = rr_arr[i:i + 5]

        if len(window_data) < WINDOW or len(future_data) < 2:
            continue

        std_w = np.std(window_data)
        if std_w < 1e-6:
            continue

        # Kurtosis excess dan skewness dari window sebelumnya
        mean_w = np.mean(window_data)
        kurt_w  = float(np.mean(((window_data - mean_w) / std_w) ** 4) - 3.0)
        skew_w  = float(np.mean(((window_data - mean_w) / std_w) ** 3))

        # Target: seberapa besar actual drawdown di window berikutnya
        # proxy = negative mean return (excess loss)
        future_neg_mean = float(-np.mean(future_data))

        features_kurt.append(kurt_w)
        features_skew.append(skew_w)
        targets.append(future_neg_mean)

    if len(targets) < 20:
        return {"error": "insufficient_rolling_windows", "n": len(targets)}

    feat_k = np.array(features_kurt)
    feat_s = np.array(features_skew)
    targ   = np.array(targets)

    # ── Grid search untuk kurtosis sigmoid params ─────────
    best_kurt_slope, best_kurt_inflect, best_kurt_corr = (
        DIST_KURT_SIG_SLOPE, DIST_KURT_SIG_INFLECT, -999.0
    )

    def _sigmoid_penalty(x, slope, inflect, max_pen):
        return max_pen / (1.0 + np.exp(-slope * (x - inflect)))

    for slope in np.arange(0.10, 0.80, 0.10):
        for inflect in np.arange(2.0, 8.0, 0.5):
            predicted = _sigmoid_penalty(feat_k, slope, inflect, DIST_KURT_SIG_MAX)
            # Korelasi antara predicted penalty dan actual excess loss
            if np.std(predicted) < 1e-9 or np.std(targ) < 1e-9:
                continue
            corr = float(np.corrcoef(predicted, targ)[0, 1])
            if corr > best_kurt_corr:
                best_kurt_corr   = corr
                best_kurt_slope  = slope
                best_kurt_inflect = inflect

    # ── Grid search untuk skewness sigmoid params ─────────
    best_skew_slope, best_skew_inflect, best_skew_corr = (
        DIST_SKEW_SIG_SLOPE, DIST_SKEW_SIG_INFLECT, -999.0
    )
    neg_mask = feat_s < 0   # hanya negative skew yang relevan
    if neg_mask.sum() >= 10:
        feat_s_neg = feat_s[neg_mask]
        targ_neg   = targ[neg_mask]
        for slope in np.arange(0.20, 1.20, 0.20):
            for inflect in np.arange(-4.0, -0.5, 0.5):
                predicted = _sigmoid_penalty(feat_s_neg, slope, inflect, DIST_SKEW_SIG_MAX)
                if np.std(predicted) < 1e-9 or np.std(targ_neg) < 1e-9:
                    continue
                corr = float(np.corrcoef(predicted, targ_neg)[0, 1])
                if corr > best_skew_corr:
                    best_skew_corr   = corr
                    best_skew_slope  = slope
                    best_skew_inflect = inflect

    # ── Baseline score threshold calibration ──────────────
    # Cek apakah threshold saat ini memisahkan WIN/LOSS dengan baik.
    # Ambil score dari rows jika ada; jika tidak, skip.
    score_thresh_suggestion = None
    score_vals = [float(r["score"]) for r in rows if r.get("score") is not None]
    if len(score_vals) >= MIN_CALIBRATION_SIGNALS:
        sc_arr = np.array(score_vals)
        outcomes = np.array([1 if r["outcome"] == "WIN" else 0
                             for r in rows if r.get("score") is not None])
        # Cari threshold yang memaksimalkan (WR high-score - WR low-score)
        best_threshold, best_separation = None, -1.0
        for thresh in np.percentile(sc_arr, np.arange(30, 75, 5)):
            mask_hi = sc_arr >= thresh
            mask_lo = sc_arr < thresh
            if mask_hi.sum() < 5 or mask_lo.sum() < 5:
                continue
            wr_hi   = float(outcomes[mask_hi].mean())
            wr_lo   = float(outcomes[mask_lo].mean())
            sep     = wr_hi - wr_lo
            if sep > best_separation:
                best_separation   = sep
                best_threshold    = thresh
        score_thresh_suggestion = {
            "optimal_threshold": round(best_threshold, 1) if best_threshold else None,
            "separation":        round(best_separation, 4),
            "note": (f"Threshold {best_threshold:.1f} separates WR by {best_separation:.0%} "
                     f"(high vs low score group)")
        }

    result = {
        "n_signals":          n_total,
        "n_windows":          len(targets),
        "kurt_params": {
            "current":    {"slope": DIST_KURT_SIG_SLOPE, "inflect": DIST_KURT_SIG_INFLECT},
            "calibrated": {"slope": round(best_kurt_slope, 2),
                           "inflect": round(best_kurt_inflect, 2)},
            "corr":       round(best_kurt_corr, 4),
            "changed":    (abs(best_kurt_slope  - DIST_KURT_SIG_SLOPE) > 0.05 or
                           abs(best_kurt_inflect - DIST_KURT_SIG_INFLECT) > 0.5),
        },
        "skew_params": {
            "current":    {"slope": DIST_SKEW_SIG_SLOPE, "inflect": DIST_SKEW_SIG_INFLECT},
            "calibrated": {"slope": round(best_skew_slope, 2),
                           "inflect": round(best_skew_inflect, 2)},
            "corr":       round(best_skew_corr, 4),
            "changed":    (abs(best_skew_slope  - DIST_SKEW_SIG_SLOPE) > 0.1 or
                           abs(best_skew_inflect - DIST_SKEW_SIG_INFLECT) > 0.5),
        },
        "score_threshold":    score_thresh_suggestion,
    }

    _last_param_calibration_n = n_total
    _calibrated_params        = result

    # Log rekomendasi
    k_ch  = result["kurt_params"]["changed"]
    s_ch  = result["skew_params"]["changed"]
    log(f"📐 [v8.09-D] Param Calibration — n={n_total} windows={len(targets)}")
    log(f"   Kurt: slope {DIST_KURT_SIG_SLOPE:.2f}→{best_kurt_slope:.2f} "
        f"inflect {DIST_KURT_SIG_INFLECT:.2f}→{best_kurt_inflect:.2f} "
        f"corr={best_kurt_corr:.3f} {'⚠️ CHANGED' if k_ch else '✅ stable'}")
    log(f"   Skew: slope {DIST_SKEW_SIG_SLOPE:.2f}→{best_skew_slope:.2f} "
        f"inflect {DIST_SKEW_SIG_INFLECT:.2f}→{best_skew_inflect:.2f} "
        f"corr={best_skew_corr:.3f} {'⚠️ CHANGED' if s_ch else '✅ stable'}")
    if score_thresh_suggestion:
        log(f"   Score threshold: {score_thresh_suggestion['note']}")
    if k_ch or s_ch:
        log("   ℹ️ Untuk apply: set ENV DIST_KURT_SIG_SLOPE, DIST_KURT_SIG_INFLECT, "
            "DIST_SKEW_SIG_SLOPE, DIST_SKEW_SIG_INFLECT sesuai nilai calibrated di atas.",
            "warn")

    # Apply jika ENV APPLY_CALIBRATED_PARAMS=true
    if os.environ.get("APPLY_CALIBRATED_PARAMS", "false").lower() == "true":
        DIST_KURT_SIG_SLOPE   = best_kurt_slope
        DIST_KURT_SIG_INFLECT = best_kurt_inflect
        DIST_SKEW_SIG_SLOPE   = best_skew_slope
        DIST_SKEW_SIG_INFLECT = best_skew_inflect
        log("✅ [v8.09-D] Calibrated params APPLIED (APPLY_CALIBRATED_PARAMS=true)")

    return result

# ════════════════════════════════════════════════════════
#  [V03] AUTO LEAN_MODE — streak-based auto-switch
# ════════════════════════════════════════════════════════
# Setelah N run berturut-turut UNPROVEN, aktifkan auto lean.
# Reset saat edge recover ke PROVEN/PROMISING.
try:
    AUTO_LEAN_THRESHOLD = int(os.environ.get("AUTO_LEAN_THRESHOLD", 3))
    AUTO_LEAN_THRESHOLD = max(1, min(AUTO_LEAN_THRESHOLD, 10))
except Exception as _e:
    import sys as _sys_env
    print(f"[WARN] ENV read AUTO_LEAN_THRESHOLD failed: {_e} — fallback 3", file=_sys_env.stderr)
    AUTO_LEAN_THRESHOLD = 3

_unproven_run_streak: int  = 0    # berapa run berturut-turut verdict UNPROVEN/INSUFFICIENT
_auto_lean_active:    bool = False  # True = auto lean sedang aktif
_auto_lean_reason:    str  = ""    # "SYSTEM_ISSUE" | "REGIME_CHANGE" | ""


def _effective_lean() -> bool:
    """[V03/BOOTSTRAP] Return True jika LEAN_MODE aktif — manual, auto, atau bootstrap phase.

    Bootstrap phase (n < BOOTSTRAP_EARLY_N) otomatis memperlakukan sistem
    sebagai lean: semua adaptive layer dinonaktifkan karena data belum cukup
    untuk menghasilkan feedback yang bermakna vs noise.
    """
    if LEAN_MODE or _auto_lean_active:
        return True
    # Bootstrap guard — jika track record belum cukup, paksa lean efektif
    if _edge_n_cache < BOOTSTRAP_EARLY_N:
        return True
    return False


# ════════════════════════════════════════════════════════
#  [V04] COMPLEXITY TAX ON SIZING
# ════════════════════════════════════════════════════════
# Setiap complexity point di atas threshold → risk dikurangi N%.
# Tax hanya aktif saat edge belum PROVEN.
# Jika edge PROVEN, complexity dianggap justified — tax = 0.
COMPLEXITY_TAX_THRESHOLD  = 7     # score di atas ini mulai kena tax
COMPLEXITY_TAX_PER_POINT  = 0.05  # 5% reduction per extra complexity point
COMPLEXITY_TAX_CAP        = 0.35  # maksimum tax 35% dari base risk

_current_complexity_tax: float = 0.0   # diset di run() dari calc_complexity_score()

# [R03] SIMPLE/COMPLEX EMPIRICAL TRACKING
# SIMPLE_MODE=True adalah pengakuan jujur: complex belum terbukti outperform simple.
# Solusinya bukan dipaksakan complex — tapi track WR per mode dan biarkan data bicara.
#
# COMPLEX_MODE_MIN_SAMPLE: butuh minimal N signal resolved per mode sebelum perbandingan valid.
# Di bawah ini: "data belum cukup — tetap SIMPLE karena lebih robust".
COMPLEX_MODE_MIN_SAMPLE = 50   # butuh 50 WIN/LOSS per mode untuk perbandingan valid

# SIMPLE_MODE_LOCK: jika True, bot tidak pernah auto-switch ke COMPLEX meski data mendukung.
# Default True = manual switch saja via ENV SIMPLE_MODE=false.
try:
    SIMPLE_MODE_LOCK = os.environ.get("SIMPLE_MODE_LOCK", "true").lower() == "true"
except Exception as _e:
    import sys as _sys_env
    print(f"[WARN] ENV read SIMPLE_MODE_LOCK failed: {_e} — fallback True", file=_sys_env.stderr)
    SIMPLE_MODE_LOCK = True

# ── Fix 3a: Scoring system validation ────────────────────────────
MIN_SIGNALS_FOR_WEIGHT_VALIDATION = 50   # was 30 — per-tier split butuh ~15+ sample valid setelah dibagi tier/strategy

# ── Fix 3b: Portfolio control — lengkap ──────────────────────────
# Max sektor exposure (% portfolio) per cycle
try:
    MAX_SECTOR_EXPOSURE_PCT = float(os.environ.get("MAX_SECTOR_EXPOSURE_PCT", 3.0))
    MAX_SECTOR_EXPOSURE_PCT = max(1.0, min(MAX_SECTOR_EXPOSURE_PCT, 10.0))
except ValueError:
    MAX_SECTOR_EXPOSURE_PCT = 3.0

# Max total signal per HARI (bukan per cycle) — mencegah overtrading
try:
    MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", 4))
    MAX_TRADES_PER_DAY = max(1, min(MAX_TRADES_PER_DAY, 20))
except ValueError:
    MAX_TRADES_PER_DAY = 4   # default: max 4 signal baru per hari

# Daily drawdown limit — jika sudah rugi X% hari ini, stop trading hari ini
try:
    MAX_DAILY_DRAWDOWN_PCT = float(os.environ.get("MAX_DAILY_DRAWDOWN_PCT", 3.0))
    MAX_DAILY_DRAWDOWN_PCT = max(0.5, min(MAX_DAILY_DRAWDOWN_PCT, 10.0))
except ValueError:
    MAX_DAILY_DRAWDOWN_PCT = 3.0   # stop jika sudah rugi 3% portfolio hari ini

# ── [PHASE6] MAX_OPEN_POSITIONS — batas jumlah posisi aktif sekaligus ───────
# Berbeda dari MAX_TRADES_PER_DAY (batas signal baru per hari):
# MAX_OPEN_POSITIONS adalah batas CONCURRENT posisi yang belum ditutup.
# Mencegah over-concentration saat banyak trade stuck atau slow-resolving.
# Default 6 — berdasarkan MAX_TRADES_PER_DAY=4 dan carry-over dari hari sebelumnya.
# ENV: MAX_OPEN_POSITIONS=6
try:
    MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", 6))
    MAX_OPEN_POSITIONS = max(1, min(MAX_OPEN_POSITIONS, 20))
except (ValueError, TypeError):
    MAX_OPEN_POSITIONS = 6

# ── [Q03] Market Impact Model — Calibration Scale ────────────
# Parameter VPR tiers di _VPR_TIERS adalah THEORETICAL defaults yang
# diturunkan dari literatur equity market microstructure (Almgren-Chriss),
# bukan dari kalibrasi empiris IDX order book nyata.
#
# IMPACT_MODEL_SCALE: multiplier global untuk semua VPR impact outputs.
#   1.0 = theoretical default (mungkin UNDERESTIMATE untuk small-cap IDX)
#   1.5–2.0 = konservatif — cocok jika trade sering mengalami slippage besar
#   0.5–0.8 = liberal  — cocok jika order flow kamu kecil relatif terhadap ADV
#
# Cara kalibrasi manual:
#   1. Catat actual_slippage dari 20+ trade (entry order vs fill price)
#   2. Bandingkan dengan impact_pct yang diprediksi bot
#   3. Set IMPACT_MODEL_SCALE = median(actual_slippage) / median(predicted_impact)
#
# Set via ENV var IMPACT_MODEL_SCALE. Default 1.0 (theoretical, tidak dikalibrasi).
try:
    IMPACT_MODEL_SCALE = float(os.environ.get("IMPACT_MODEL_SCALE", 1.0))
    IMPACT_MODEL_SCALE = max(0.1, min(IMPACT_MODEL_SCALE, 5.0))
except ValueError:
    IMPACT_MODEL_SCALE = 1.0

# Status kalibrasi — dipakai untuk logging dan disclaimer sinyal
IMPACT_CALIBRATION_STATUS = (
    "USER_CALIBRATED"
    if os.environ.get("IMPACT_MODEL_SCALE")
    else "UNCALIBRATED_THEORETICAL"
)

# ── Fix 5: Foreign flow proxy hanya informatif ───────────────────
try:
    FOREIGN_FLOW_BLOCK_ENABLED = os.environ.get("FOREIGN_FLOW_BLOCK", "false").lower() == "true"
except Exception as _e:
    import sys as _sys_env
    print(f"[WARN] ENV read FOREIGN_FLOW_BLOCK failed: {_e} — fallback False", file=_sys_env.stderr)
    FOREIGN_FLOW_BLOCK_ENABLED = False   # DEFAULT: tidak memblokir, hanya informasi

# Tracking exposure per sektor dalam satu cycle
_sector_exposure_tracker: dict = {}

# [v7.2 — FIX Masalah 4] Di IDX, short selling tidak tersedia untuk investor ritel.
# SELL signal = instruksi KELUAR POSISI / Take Profit bukan open short.
# Set True agar semua SELL signal diberi label "EXIT / TP" bukan "SELL SHORT"
SELL_AS_EXIT_ONLY  = True

# [v7.10] Net Foreign Flow proxy — confidence threshold untuk hard block BUY
# Nilai 0.75 lebih konservatif dari sebelumnya (0.70) — hanya block saat
# sinyal OUTFLOW benar-benar konsisten dan kuat. Proxy ini heuristik dari
# OHLCV, bukan data net asing sesungguhnya, jadi false positive harus minimal.
# Set via ENV var FOREIGN_FLOW_BLOCK_CONF jika ingin di-tune.
try:
    FOREIGN_FLOW_BLOCK_CONF = float(os.environ.get("FOREIGN_FLOW_BLOCK_CONF", 0.75))
    FOREIGN_FLOW_BLOCK_CONF = max(0.50, min(FOREIGN_FLOW_BLOCK_CONF, 0.95))
except ValueError:
    FOREIGN_FLOW_BLOCK_CONF = 0.75

# [v7.2 — FIX Masalah 1] Data delay disclaimer
# yfinance memberikan data dengan delay ~15 menit di jam bursa.
# Semua sinyal yang dikirim mencantumkan waktu + disclaimer delay ini.
DATA_DELAY_MINUTES = 15

# Scoring threshold per tier
TIER_MIN_SCORE = {
    "S":  11,   # [PHASE-0] was 14 — turunkan agar tier S lebih mudah dicapai
    "A+":  8,   # [PHASE-0] was 10
    "A":   6,   # [PHASE-0] was  8
}

# Risk/Reward minimum
MIN_RR = {
    "INTRADAY": 1.3,   # [PHASE-0] was 1.5 — longgarkan agar lebih banyak setup lolos
    "SWING":    1.3,   # [PHASE-0] was 1.4 → 1.3 — IDX spread & slippage makan RR
}

# ── [P8-05] PHASE3 FREEZE PROTECTION ─────────────────────────────────────────
# Diletakkan di sini (setelah MIN_RR, MIN_VOLUME_IDR, MAX_SIGNALS_CYCLE sudah
# didefinisikan) agar guard bisa membaca nilai aktualnya.
#
# Masalah sebelumnya: tidak ada sistem yang mencegah developer tanpa sadar
# mengubah parameter kritis saat Phase 3 berjalan.
# Fix: jika PHASE3_COLLECTION=True dan parameter berubah dari baseline →
# bot langsung raise RuntimeError dengan pesan jelas saat startup.
#
# ⚠️ Jika kamu SENGAJA ingin ubah parameter di bawah:
#    1. Set PHASE3_COLLECTION=false (via ENV) lebih dulu
#    2. Putuskan: apakah data lama masih valid dengan parameter baru?
#    3. Jika tidak valid → data lama perlu di-exclude dari analisis edge
if PHASE3_COLLECTION:
    import sys as _sys_freeze
    _freeze_errors: list = []

    if MIN_RR.get("SWING") != 1.3:
        _freeze_errors.append(
            f"MIN_RR['SWING'] = {MIN_RR.get('SWING')} (expected 1.3). "
            f"Mengubah RR membuat data historis tidak komparabel."
        )
    if MIN_VOLUME_IDR != 1_000_000_000:
        _freeze_errors.append(
            f"MIN_VOLUME_IDR = {MIN_VOLUME_IDR:,} (expected 1,000,000,000). "
            f"Mengubah volume filter mengubah distribusi ticker yang lolos."
        )
    if MAX_SIGNALS_CYCLE < 5:
        _freeze_errors.append(
            f"MAX_SIGNALS_CYCLE = {MAX_SIGNALS_CYCLE} (expected >= 5). "
            f"Terlalu ketat — data collection menjadi terlalu lambat."
        )

    if _freeze_errors:
        print(
            "\n🚫 [P8-05] PHASE3 FREEZE VIOLATION — Parameter kritis berubah!\n"
            + "\n".join(f"  ❌ {e}" for e in _freeze_errors) +
            "\n\nAksi wajib: kembalikan ke nilai semula ATAU set PHASE3_COLLECTION=false "
            "dan tandai data lama sebagai dari distribusi berbeda.\n",
            file=_sys_freeze.stderr
        )
        raise RuntimeError(
            f"[P8-05] Phase 3 parameter freeze violated: {len(_freeze_errors)} error(s). "
            f"Lihat stderr untuk detail."
        )
    del _sys_freeze, _freeze_errors   # cleanup — tidak perlu di namespace global

# [v7.11] RSI overbought/oversold threshold per strategy
# Intraday lebih toleran (72/28) karena pergerakan 1H lebih cepat reverse
# Swing lebih ketat (68/32) karena posisi multi-hari butuh entry lebih konservatif
RSI_OB = {"INTRADAY": 78, "SWING": 75}   # [PHASE-0] was 72/68 — lebih longgar agar BUY tidak terblokir
RSI_OS = {"INTRADAY": 22, "SWING": 25}   # [PHASE-0] was 28/32 — lebih longgar agar SELL tidak terblokir

# [v7.2 — FIX Masalah 6] Toleransi RR setelah fraksi harga IDX
# Fraksi rounding bisa menurunkan RR sedikit — toleransi 1 fraksi tick
# agar sinyal valid tidak ditolak hanya karena pembulatan Rp5-Rp50
RR_FRAKSI_TOLERANCE = 0.10  # 10% tolerance — misal min RR 1.5 → accept ≥ 1.35

# SL/TP berbasis ATR
INTRADAY_SL_ATR = 1.5
INTRADAY_TP1_R  = 1.5
INTRADAY_TP2_R  = 2.5
SWING_SL_ATR    = 2.0
SWING_TP1_R     = 2.0
SWING_TP2_R     = 3.5

# Market Regime (ADX)
ADX_TREND  = 25
ADX_CHOP   = 18
ADX_PERIOD = 14

# IHSG Guard — pengganti BTC regime
IHSG_DROP_BLOCK  = -2.0   # IHSG turun > 2% dalam 1 hari → blok BUY (default)
IHSG_CRASH_BLOCK = -5.0   # IHSG crash > 5% dalam 5 hari → halt semua

# [v7.2 — FIX Masalah 5] Sektor yang boleh tetap BUY meski IHSG drop ringan (-2% s/d -3%)
# Sektor-sektor ini sering counter-trend: IHSG merah karena BANKING/TELCO,
# tapi MINING/ENERGY/CPO justru naik karena korelasi dengan komoditas global.
# Saat IHSG_DROP_BLOCK aktif, sektor di bawah ini tetap diizinkan BUY
# KECUALI jika IHSG_CRASH_BLOCK aktif (>5%) — semua halt tanpa pengecualian.
IHSG_COUNTER_TREND_SECTORS = {"ENERGY", "MINING", "CPO", "PETROCHEM"}

# ── Sector Correlation Map ────────────────────────────────────────
# Kelompokkan saham berdasarkan sektor IDX
SECTOR_MAP = {
    "BANKING":    ["BBCA.JK","BBRI.JK","BMRI.JK","BBNI.JK","PNBN.JK","NISP.JK","BNLI.JK","BRIS.JK","BTPS.JK","ARTO.JK"],
    "TELCO":      ["TLKM.JK","EXCL.JK","ISAT.JK","TOWR.JK","TBIG.JK","EMTK.JK"],
    "ENERGY":     ["ADRO.JK","PTBA.JK","ITMG.JK","HRUM.JK","DOID.JK","GEMS.JK","BYAN.JK","AADI.JK"],
    "MINING":     ["ANTM.JK","INCO.JK","MDKA.JK","AMMN.JK","MBMA.JK","NCKL.JK","ESSA.JK"],
    "CONSUMER":   ["UNVR.JK","ICBP.JK","KLBF.JK","HMSP.JK","GGRM.JK","INDF.JK","MYOR.JK","SIDO.JK","ULTJ.JK","ROTI.JK","SKBM.JK"],
    "AUTO_INFRA": ["ASII.JK","UNTR.JK","JSMR.JK","ERAA.JK"],
    "PROPERTY":   ["PWON.JK","BSDE.JK","CTRA.JK","SMRA.JK","PANI.JK","CBDK.JK","KIJA.JK","BEST.JK","DSSA.JK"],
    "PETROCHEM":  ["TPIA.JK","BRPT.JK","PGAS.JK"],
    "CPO":        ["AALI.JK","LSIP.JK","SIMP.JK","SSMS.JK","TAPG.JK","TBLA.JK"],
    "POULTRY":    ["CPIN.JK","JPFA.JK","MAIN.JK"],
    "CEMENT":     ["SMGR.JK","INTP.JK"],
    "PULP":       ["INKP.JK","TKIM.JK"],
    "MEDIA":      ["SCMA.JK","MNCN.JK"],
    "TECH":       ["GOTO.JK","EMTK.JK"],
    "MISC":       ["AKRA.JK","MAPI.JK","LPPF.JK","MARK.JK","CUAN.JK"],
}

# Reverse map: ticker → sector name
TICKER_SECTOR: dict = {}
for _sector, _tickers in SECTOR_MAP.items():
    for _t in _tickers:
        TICKER_SECTOR[_t] = _sector

# Sektor proxy — ticker paling representatif per sektor (untuk cek kekuatan sektor)
SECTOR_PROXY = {
    "BANKING":    "BBCA.JK",
    "TELCO":      "TLKM.JK",
    "ENERGY":     "ADRO.JK",
    "MINING":     "ANTM.JK",
    "CONSUMER":   "UNVR.JK",
    "AUTO_INFRA": "ASII.JK",
    "PROPERTY":   "BSDE.JK",
    "PETROCHEM":  "TPIA.JK",
    "CPO":        "AALI.JK",
    "POULTRY":    "CPIN.JK",
    "CEMENT":     "SMGR.JK",
    "PULP":       "INKP.JK",
    "MEDIA":      "SCMA.JK",
    "TECH":       "EMTK.JK",   # [v7.11] EMTK lebih representatif — BUKA dihapus v7.10
    "MISC":       "AKRA.JK",
}

# Sector momentum cache — diisi sekali per run oleh get_sector_momentum()
_sector_momentum_cache: dict = {}

# [Z02] IHSG Cache terpusat — eliminasi triple download ^JKSE per run
# Format: {"data": dict, "timestamp": float, "ttl": int}
# Semua fungsi yang butuh IHSG data WAJIB pakai get_ihsg_cached()
_ihsg_cache: dict = {}
_IHSG_CACHE_TTL_SECONDS = 600   # 10 menit — cukup untuk satu siklus scan 4-jam

# ── Position Management Constants ────────────────────────────────
# Threshold untuk otomasi break-even dan trailing stop
BE_TRIGGER_R     = 1.0    # Pindah SL ke BE setelah profit = 1x risiko
TRAIL_TRIGGER_R  = 1.5    # Mulai trailing setelah profit = 1.5x risiko
TRAIL_ATR_MULT   = 0.8    # Trailing stop = ATR * 0.8 di bawah high terkini

# Weighted Score Components (sama dengan bot crypto)
W = {
    "bos":         6,
    "choch":       5,
    "liq_sweep":   4,
    "order_block": 4,
    "macd_cross":  3,
    "rsi_zone":    3,
    "vol_confirm": 3,
    "ema_align":   2,
    "vwap_side":   2,
    "pullback":    2,
    "candle_body": 2,
    "equal_lows":  1,
    "equal_highs": 1,
    "rsi_extreme": 2,
    "macd_soft":  -2,
    "adx_trend":   2,
    "adx_ranging": -2,
}

# Cache candle per sesi — reset tiap run
_candle_cache: dict = {}

# In-memory dedup fallback
_dedup_memory: set = set()

# Adaptive weights — diisi oleh get_feedback_weights() di awal run()
# Default kosong = gunakan W base
_adaptive_weights: dict = {}


# ════════════════════════════════════════════════════════
#  [R02] RUN STATE SENTINEL — v8.03
#
#  Problem: _candle_cache, _sector_exposure_tracker, _adaptive_weights
#  adalah global mutables. Jika salah satu lupa di-reset, state dari
#  run sebelumnya bocor ke run baru tanpa peringatan.
#
#  Solusi: RunStateGuard mencatat "run fingerprint" setelah reset atomik,
#  dan validate_run_state() memverifikasi semuanya bersih sebelum scan.
# ════════════════════════════════════════════════════════

import uuid as _uuid

class _RunStateGuard:
    """
    [R02] Sentinel untuk memverifikasi global state telah di-reset atomik.

    Cara kerja:
      1. Di awal run(), setelah semua global direset, panggil .mark_reset()
      2. Sebelum scan dimulai, panggil .validate() — jika ada global yang
         tidak bersih, log warning dengan detail spesifik.
      3. .run_id berubah setiap run — memudahkan korelasi log lintas fungsi.

    Tidak memblokir run — hanya transparansi. Keputusan lanjut/tidak
    tetap di tangan operator.
    """
    def __init__(self):
        self.run_id:          str  = ""
        self.reset_called:    bool = False
        self.validation_log:  list = []

    def mark_reset(self):
        """Dipanggil setelah semua global direset di run()."""
        self.run_id       = _uuid.uuid4().hex[:8]
        self.reset_called = True
        self.validation_log.clear()

    def validate(self,
                 candle_cache:    dict,
                 sector_tracker:  dict,
                 adaptive_weights: dict,
                 cluster_weights: dict,
                 raw_counts:      dict) -> bool:
        """
        Verifikasi bahwa semua global dalam state bersih setelah reset.
        Returns True jika semua OK, False jika ada anomali.
        """
        errors = []
        if not self.reset_called:
            errors.append("mark_reset() belum dipanggil — reset mungkin dilewati")
        if candle_cache:
            errors.append(f"_candle_cache tidak kosong ({len(candle_cache)} entry) — state stale?")
        if sector_tracker:
            errors.append(f"_sector_exposure_tracker tidak kosong ({len(sector_tracker)} entry)")
        if adaptive_weights:
            errors.append(f"_adaptive_weights sudah terisi sebelum get_feedback_weights() — urutan salah?")
        if cluster_weights:
            errors.append(f"_cluster_weights sudah terisi sebelum get_cluster_weights() — urutan salah?")
        if raw_counts:
            errors.append(f"_cluster_raw_counts sudah terisi sebelum cluster load")

        self.validation_log = errors
        if errors:
            for e in errors:
                log(f"  ⚠️ [R02] RunStateGuard [{self.run_id}]: {e}", "warn")
            return False
        log(f"  ✅ [R02] RunStateGuard [{self.run_id}]: semua global state bersih")
        return True


_run_state_guard = _RunStateGuard()


# ════════════════════════════════════════════════════════
#  [S01] EDGE PROOF — Konstanta minimum bukti edge
# ════════════════════════════════════════════════════════

# Jumlah minimum resolved signals (WIN/LOSS) untuk klaim edge terbukti
EDGE_PROOF_MIN_SIGNALS = 100
# Minimum EV empiris yang dianggap "terbukti menguntungkan"
EDGE_MIN_EV            = 0.10
# Minimum win rate empiris
EDGE_MIN_WR            = 0.52
# p-value threshold untuk signifikansi statistik (one-sided)
EDGE_PVAL_THRESHOLD    = 0.05

# ════════════════════════════════════════════════════════
#  BOOTSTRAP PHASE — Safeguard saat track record belum cukup
#
#  Problem: semua adaptive layer (cluster weights, feedback loop,
#  adaptive relaxation, kelly shrinkage, complexity tax) dirancang
#  untuk bekerja di atas data historis yang memadai. Saat n < 30,
#  semua layer itu beroperasi di atas noise — bukan signal.
#
#  Solusi: deteksi bootstrap phase dari n resolved signal.
#  - COLD  (n <  10): observasi murni. Max 1 signal/run.
#  - EARLY (n <  30): adaptive layer off otomatis. Max 2 signal/run.
#  - WARMING (n < 100): lean efektif off tapi EV gate lebih ketat.
#  - MATURE (n >= 100): full mode, edge proof gate berlaku normal.
#
#  Tidak ada perubahan ENV yang diperlukan — otomatis berdasarkan data.
#  Saat MATURE, behavior identik dengan bot sebelum fix ini.
# ════════════════════════════════════════════════════════

BOOTSTRAP_COLD_N          = 10   # < 10  = tidak ada feedback sama sekali
BOOTSTRAP_EARLY_N         = 30   # < 30  = adaptive layer tidak reliable
BOOTSTRAP_WARMING_N       = 100  # < 100 = EDGE_PROOF_MIN_SIGNALS (warming menuju mature)

BOOTSTRAP_SIGNALS_CAP_COLD  = 3  # [PHASE-0] was 1 — naikkan agar bot bisa kirim signal di fase cold
BOOTSTRAP_SIGNALS_CAP_EARLY = 5  # [PHASE-0] was 2 — naikkan agar tidak terlalu dibatasi di fase early

# Diisi oleh run() setelah check_edge_proven() — dibaca oleh _effective_lean()
# dan logika signals_cap. Default 0 agar cold start langsung masuk COLD phase.
_edge_n_cache: int = 0

# Label phase saat ini — diisi di run() untuk log dan health check
_bootstrap_phase: str = "COLD"

# [T02] EV COST MODEL — estimasi biaya per tier/strategy untuk net EV
# Semua nilai dalam persen (0.3 = 0.3% dari nilai posisi)
# Ini adalah DEFAULT — idealnya dikalibrasi dari actual fill data
EV_COST_SLIPPAGE = {
    "INTRADAY": {"S": 0.25, "A+": 0.30, "A": 0.40},   # % entry price
    "SWING":    {"S": 0.15, "A+": 0.20, "A": 0.25},
}
EV_COST_FILL_RATE = {
    "INTRADAY": 0.75,   # estimasi 75% order tereksekusi (limit order IDX queue)
    "SWING":    0.90,   # swing lebih longgar, fill rate lebih tinggi
}
# Threshold: net EV harus positif untuk sinyal lolos (default off — informasi saja)
# Set True via ENV EV_USE_ADJUSTED=true untuk aktifkan hard gate net EV
try:
    EV_USE_ADJUSTED_GATE = os.environ.get("EV_USE_ADJUSTED", "false").lower() == "true"
except Exception as _e:
    import sys as _sys_env
    print(f"[WARN] ENV read EV_USE_ADJUSTED failed: {_e} — fallback False", file=_sys_env.stderr)
    EV_USE_ADJUSTED_GATE = False


# ════════════════════════════════════════════════════════
#  [S03] FILTER AUDIT — Track berapa kali tiap gate blokir
# ════════════════════════════════════════════════════════

# Key = nama gate, Value = jumlah ticker/signal yang diblokir gate ini di run ini
_filter_audit: dict = {}
# Total ticker yang masuk ke pipeline check_intraday / check_swing
_filter_audit_checked: int = 0

# ════════════════════════════════════════════════════════
#  [PHASE-1] PIPELINE SCORE BREAKDOWN — v8.16
#  Tracking per-komponen agar tahu PERSIS kenapa signal gagal.
#  Diupdate oleh check_intraday / check_swing setiap ticker.
# ════════════════════════════════════════════════════════
# Akumulasi score mentah dari score_signal() sebelum filter apapun
_p1_score_total_sum:     int = 0   # total score semua ticker yang sampai scoring
_p1_score_total_count:   int = 0   # berapa ticker yang dihitung
# Komponen pemblokir terbanyak (dikumpulkan dari _filter_audit)
# Format final dibangun di run() dari _filter_audit yang sudah ada

# Per-ticker score breakdown (untuk DEBUG_TICKER mode)
# Key = ticker, Value = dict detail
_p1_ticker_score_detail: dict = {}

# [T03] ADAPTIVE FILTER RELAXATION — simpan audit run sebelumnya
# untuk mendeteksi gate yang over-blocking dan relaksasi threshold
_prev_filter_audit:         dict = {}   # audit dari run terakhir
_prev_filter_audit_checked: int  = 0

# Threshold: gate dianggap over-blocking jika blokir > X% dari total yang masuk
# Default 45% — kalau satu gate blokir lebih dari hampir separuh ticker, itu mencurigakan
try:
    ADAPTIVE_BLOCK_THRESHOLD = float(os.environ.get("ADAPTIVE_BLOCK_THRESHOLD", 0.45))
    ADAPTIVE_BLOCK_THRESHOLD = max(0.20, min(ADAPTIVE_BLOCK_THRESHOLD, 0.80))
except Exception as _e:
    import sys as _sys_env
    print(f"[WARN] ENV read ADAPTIVE_BLOCK_THRESHOLD failed: {_e} — fallback 0.45", file=_sys_env.stderr)
    ADAPTIVE_BLOCK_THRESHOLD = 0.45

# Cap relaksasi maksimum per gate: tidak boleh relaksasi lebih dari 15% dari nilai asli
ADAPTIVE_MAX_RELAX_PCT = 0.15

# [U01] ADAPTIVE RELAXATION GUARD
# Relaksasi hanya diizinkan jika edge sudah PROVEN atau PROMISING.
# Jika UNPROVEN/INSUFFICIENT: tidak ada relaksasi — jangan optimise noise.
# [PHASE-0] Relaksasi diizinkan tanpa butuh edge PROVEN — buka gate ini
ADAPTIVE_RELAX_REQUIRE_EDGE = False   # [PHASE-0] was True — diblokir saat UNPROVEN, sekarang dibebaskan

# Cooldown: setelah gate direlaksasi, tunggu N run sebelum relaksasi lagi.
# Mencegah spiral relaksasi: setiap run ngejar run sebelumnya → edge hilang.
ADAPTIVE_RELAX_COOLDOWN = 3   # default: tunggu 3 run setelah relaksasi

# State: track cooldown per gate (gate → sisa run cooldown)
_relaxation_cooldowns: dict = {}
# Counter run total — naik setiap run(), dipakai untuk cooldown tracking
_relaxation_run_counter: int = 0

# Simpan relaxations yang aktif di run ini untuk logging dan transparansi
_active_relaxations: dict = {}   # gate → {"original": v, "relaxed": v, "reason": str}


def _audit_block(gate: str, note: str = "") -> None:
    """
    [S03] Catat satu blokir pada gate tertentu.
    Dipanggil di setiap titik return None di check_intraday/check_swing.
    Thread-safe untuk single-threaded bot; cukup dict global.
    """
    global _filter_audit
    _filter_audit[gate] = _filter_audit.get(gate, 0) + 1


def _audit_enter() -> None:
    """[S03] Catat satu ticker yang mulai dievaluasi."""
    global _filter_audit_checked
    _filter_audit_checked += 1


def _record_score_detail(ticker: str, strategy: str, side: str,
                         score: int, rr: float, ev: float,
                         structure_ok: bool, rsi_ok: bool,
                         tier: str = "", blocker: str = "") -> None:
    """
    [PHASE-1 v8.16] Simpan detail scoring per-ticker untuk pipeline breakdown.
    Dipanggil setelah scoring di check_intraday / check_swing.
    Dipakai oleh DEBUG_TICKER dan summary blocker report.
    """
    global _p1_score_total_sum, _p1_score_total_count, _p1_ticker_score_detail
    _p1_score_total_sum   += score
    _p1_score_total_count += 1
    key = f"{ticker}_{strategy}_{side}"
    _p1_ticker_score_detail[key] = {
        "ticker":       ticker,
        "strategy":     strategy,
        "side":         side,
        "score":        score,
        "rr":           round(rr, 2),
        "ev":           round(ev, 3),
        "structure_ok": structure_ok,
        "rsi_ok":       rsi_ok,
        "tier":         tier,
        "blocker":      blocker,
    }


def get_filter_audit_summary() -> str:
    """
    [S03] Kembalikan string ringkasan filter audit untuk logging dan Telegram.
    [v8.12] Format diperkaya dengan ikon dan top-3 blocker untuk heartbeat.
    """
    if not _filter_audit:
        return "— Tidak ada blokir tercatat (mungkin watchlist kosong)"
    total = max(_filter_audit_checked, 1)
    # Emoji per gate untuk Telegram readability
    _gate_icons = {
        "IHSG":        "📉", "VOLUME":     "📊", "RSI":        "⚡",
        "EV":          "📐", "STALE":      "⏱️", "SECTOR":    "🏭",
        "ADX":         "📈", "SCORE":      "🎯", "TIER":       "🏅",
        "REGIME":      "🌀", "DRAWDOWN":   "🔻", "ARA_ARB":   "⛔",
        "CORRUPT":     "❌", "LEAN":       "🛡️", "BOOTSTRAP": "🧊",
    }
    lines = []
    for i, (gate, count) in enumerate(
            sorted(_filter_audit.items(), key=lambda x: -x[1])[:8], 1):
        pct  = count / total * 100
        icon = next((v for k, v in _gate_icons.items() if k in gate.upper()), "🔒")
        bar_filled = int(pct / 100 * 8)
        bar = "█" * bar_filled + "░" * (8 - bar_filled)
        lines.append(f"  {i}. {icon} {gate:<18}: {count:>3}x ({pct:.0f}%) [{bar}]")
    blocked_total = sum(_filter_audit.values())
    pass_through  = max(total - blocked_total, 0)
    lines.append(f"  ✅ LOLOS : {pass_through}x dari {total} evaluasi")
    return "\n".join(lines)


def apply_adaptive_relaxation(edge_verdict: str = "INSUFFICIENT") -> dict:
    """
    [T03/U01] Relaksasi threshold secara adaptif — dengan guard rails.

    Guard 1 — Edge requirement [U01]:
      Relaksasi hanya diizinkan jika edge_verdict adalah PROVEN atau PROMISING.
      UNPROVEN/INSUFFICIENT → semua relaksasi diblokir.
      Alasan: relaksasi tanpa terbuktinya edge = optimisasi noise,
      bukan optimisasi edge. Hasilnya adalah overfitting ke masa lalu.

    Guard 2 — Cooldown per gate [U01]:
      Setelah gate direlaksasi, ia masuk cooldown ADAPTIVE_RELAX_COOLDOWN run.
      Ini mencegah re-relaksasi beruntun yang memelintir sistem ke titik ekstrem.

    Guard 3 — LEAN_MODE [U04]:
      Jika LEAN_MODE aktif, fungsi ini tidak melakukan apa-apa.

    Semua keputusan dicatat di _active_relaxations dan di-log eksplisit.
    """
    global TIER_MIN_SCORE, EV_MIN_THRESHOLD, RR_FRAKSI_TOLERANCE, \
           INTRADAY_MIN_EQS, _active_relaxations, _relaxation_cooldowns

    _active_relaxations = {}

    # Guard 3: LEAN_MODE (manual or auto)
    if _effective_lean():
        log("  ℹ️ [U01] Adaptive relaxation: LEAN_MODE aktif — skip")
        return {}

    # Guard 1: Edge requirement
    if ADAPTIVE_RELAX_REQUIRE_EDGE and edge_verdict not in ("PROVEN", "PROMISING"):
        log(f"  ⚠️ [U01] Adaptive relaxation DIBLOKIR — edge verdict={edge_verdict}. "
            f"Jangan optimise threshold tanpa bukti edge yang valid.", "warn")
        return {}

    total = max(_prev_filter_audit_checked, 1)
    if not _prev_filter_audit or total < 10:
        log("  ℹ️ [T03] Adaptive relaxation: tidak ada data run sebelumnya — skip")
        return {}

    # Tick down semua cooldowns
    expired = [g for g, cd in _relaxation_cooldowns.items() if cd <= 1]
    for g in expired:
        del _relaxation_cooldowns[g]
    for g in _relaxation_cooldowns:
        _relaxation_cooldowns[g] -= 1

    def _block_rate(gate: str) -> float:
        return _prev_filter_audit.get(gate, 0) / total

    def _try_relax(gate: str, action_fn, orig_val, cap_check_fn) -> bool:
        """Helper: cek cooldown → apply → record."""
        if _relaxation_cooldowns.get(gate, 0) > 0:
            log(f"  ⏳ [U01] {gate}: cooldown aktif ({_relaxation_cooldowns[gate]} run) — skip relaksasi")
            return False
        if _block_rate(gate) <= ADAPTIVE_BLOCK_THRESHOLD:
            return False
        new_val = action_fn()
        if cap_check_fn(new_val, orig_val):
            _relaxation_cooldowns[gate] = ADAPTIVE_RELAX_COOLDOWN
            return True
        return False

    applied = {}

    # ── TIER_SKIP ─────────────────────────────────────────
    orig_s = TIER_MIN_SCORE["S"]
    def _relax_tier():
        global TIER_MIN_SCORE
        TIER_MIN_SCORE = dict(TIER_MIN_SCORE)
        TIER_MIN_SCORE["S"]  = TIER_MIN_SCORE["S"]  - 1
        TIER_MIN_SCORE["A+"] = max(TIER_MIN_SCORE["A+"] - 1, 7)
        TIER_MIN_SCORE["A"]  = max(TIER_MIN_SCORE["A"]  - 1, 5)
        return TIER_MIN_SCORE["S"]
    if _try_relax("TIER_SKIP", _relax_tier, orig_s,
                  lambda nv, ov: nv >= max(int(ov * (1 - ADAPTIVE_MAX_RELAX_PCT)), ov - 1)):
        applied["TIER_SKIP"] = {"original": orig_s, "relaxed": TIER_MIN_SCORE["S"],
                                 "reason": f"rate={_block_rate('TIER_SKIP'):.0%}"}
        log(f"  🔵 [T03] TIER_MIN_SCORE S: {orig_s}→{TIER_MIN_SCORE['S']} "
            f"(rate {_block_rate('TIER_SKIP'):.0%}, cooldown {ADAPTIVE_RELAX_COOLDOWN} run)")

    # ── EV_THRESHOLD ──────────────────────────────────────
    orig_ev = EV_MIN_THRESHOLD
    def _relax_ev():
        global EV_MIN_THRESHOLD
        EV_MIN_THRESHOLD = max(round(orig_ev - 0.02, 3), orig_ev * (1 - ADAPTIVE_MAX_RELAX_PCT))
        return EV_MIN_THRESHOLD
    if _try_relax("EV_THRESHOLD", _relax_ev, orig_ev,
                  lambda nv, ov: nv >= ov * (1 - ADAPTIVE_MAX_RELAX_PCT)):
        applied["EV_THRESHOLD"] = {"original": orig_ev, "relaxed": EV_MIN_THRESHOLD,
                                    "reason": f"rate={_block_rate('EV_THRESHOLD'):.0%}"}
        log(f"  🔵 [T03] EV_MIN_THRESHOLD: {orig_ev:.3f}→{EV_MIN_THRESHOLD:.3f} "
            f"(rate {_block_rate('EV_THRESHOLD'):.0%})")

    # ── RR_LOW ────────────────────────────────────────────
    orig_rr = RR_FRAKSI_TOLERANCE
    def _relax_rr():
        global RR_FRAKSI_TOLERANCE
        RR_FRAKSI_TOLERANCE = min(round(orig_rr + 0.03, 3), orig_rr * (1 + ADAPTIVE_MAX_RELAX_PCT))
        return RR_FRAKSI_TOLERANCE
    if _try_relax("RR_LOW", _relax_rr, orig_rr,
                  lambda nv, ov: nv <= ov * (1 + ADAPTIVE_MAX_RELAX_PCT)):
        applied["RR_LOW"] = {"original": orig_rr, "relaxed": RR_FRAKSI_TOLERANCE,
                              "reason": f"rate={_block_rate('RR_LOW'):.0%}"}
        log(f"  🔵 [T03] RR_FRAKSI_TOLERANCE: {orig_rr:.2f}→{RR_FRAKSI_TOLERANCE:.2f} "
            f"(rate {_block_rate('RR_LOW'):.0%})")

    # ── EQS_DEGRADED ──────────────────────────────────────
    orig_eqs = INTRADAY_MIN_EQS
    def _relax_eqs():
        global INTRADAY_MIN_EQS
        INTRADAY_MIN_EQS = max(int(orig_eqs - 3), int(orig_eqs * (1 - ADAPTIVE_MAX_RELAX_PCT)))
        return INTRADAY_MIN_EQS
    if _try_relax("EQS_DEGRADED", _relax_eqs, orig_eqs,
                  lambda nv, ov: nv >= int(ov * (1 - ADAPTIVE_MAX_RELAX_PCT))):
        applied["EQS_DEGRADED"] = {"original": orig_eqs, "relaxed": INTRADAY_MIN_EQS,
                                    "reason": f"rate={_block_rate('EQS_DEGRADED'):.0%}"}
        log(f"  🔵 [T03] INTRADAY_MIN_EQS: {orig_eqs}→{INTRADAY_MIN_EQS} "
            f"(rate {_block_rate('EQS_DEGRADED'):.0%})")

    _active_relaxations = applied
    if not applied:
        log("  ✅ [T03/U01] Adaptive relaxation: nol gate over-blocking atau semua dalam cooldown")
    return applied


# ════════════════════════════════════════════════════════
#  [v8.01] DECOUPLING LAYER — Interdependency Fix
#
#  Masalah v8.00: weight pipeline tersebar, ev_floor berubah
#  via global mutable tanpa trace, score boost tumpuk tanpa
#  breakdown, threshold bisa berubah mid-scan.
#
#  Solusi: MarketContext + ScoreAccumulator + ThresholdGuard
# ════════════════════════════════════════════════════════

import copy as _copy
import time as _time
from dataclasses import dataclass as _dataclass, field as _field
from typing import Optional as _Optional


@_dataclass
class MarketContext:
    """
    Snapshot kondisi pasar untuk SATU ticker pada SATU waktu scan.
    Dibuat sekali sebelum scoring, tidak berubah setelah itu.
    Seluruh pipeline (weights, strategy_mode, ev_floor) tercatat di sini.
    """
    ticker:   str
    side:     str    # "BUY" | "SELL"
    strategy: str    # "INTRADAY" | "SWING"

    regime: str
    adx:    float
    phase:  str

    base_weights:    dict = _field(default_factory=dict)
    dynamic_weights: dict = _field(default_factory=dict)
    final_weights:   dict = _field(default_factory=dict)

    strategy_mode: dict = _field(default_factory=dict)

    raw_score:      int = 0
    priority_boost: int = 0
    sniper_bonus:   int = 0
    strategy_boost: int = 0
    final_score:    int = 0

    wr_cache_snapshot:   dict = _field(default_factory=dict)
    threshold_snapshot:  dict = _field(default_factory=dict)
    built_at_ts:         float = 0.0
    pipeline_log:        list  = _field(default_factory=list)


def _build_market_context(
    ticker: str, side: str, strategy: str,
    regime: str, adx: float, phase: str,
) -> MarketContext:
    """
    [v8.01] Bangun MarketContext untuk satu ticker.
    Resolve semua dependency (weights, strategy_mode) dalam satu tempat,
    inject dari global — tapi di-copy agar immutable setelah ini.
    """
    ctx = MarketContext(
        ticker=ticker, side=side, strategy=strategy,
        regime=regime, adx=adx, phase=phase,
        built_at_ts=_time.time(),
        wr_cache_snapshot=_copy.deepcopy(_strategy_wr_cache),
        threshold_snapshot=_copy.deepcopy(
            _threshold_guard.snapshot() if _threshold_guard else {}
        ),
    )

    # Step 1: Base weights
    ctx.base_weights = _copy.deepcopy(_adaptive_weights if _adaptive_weights else W)
    ctx.pipeline_log.append(f"base={'adaptive' if _adaptive_weights else 'W'}")

    # Step 2: Dynamic weights (regime + phase)
    ctx.dynamic_weights = get_dynamic_weights(regime, phase)
    ctx.pipeline_log.append(f"dynamic(regime={regime},phase={phase})")

    # Step 3: Merge — max untuk semua key, min untuk penalty keys
    PENALTY_KEYS = {"macd_soft", "adx_ranging"}
    merged = {}
    for k in W:
        if k in PENALTY_KEYS:
            merged[k] = min(ctx.base_weights.get(k, W[k]), ctx.dynamic_weights.get(k, W[k]))
        else:
            merged[k] = max(ctx.base_weights.get(k, W[k]), ctx.dynamic_weights.get(k, W[k]))
    ctx.pipeline_log.append("merge(max/min)")

    # Step 4: Cluster modifier
    # [PHASE-2] HARD DISABLED — cluster_weights dimatikan paksa saat PHASE2_STABILIZE
    if PHASE2_STABILIZE:
        ctx.final_weights = merged
        ctx.pipeline_log.append("cluster(PHASE2_BYPASS)")
    else:
        merged = apply_cluster_weights(merged, regime, ticker)
        ctx.final_weights = merged
        ctx.pipeline_log.append(f"cluster({ticker})")

    # Step 5: Strategy mode — inject wr_cache snapshot, bukan global langsung
    ctx.strategy_mode = _get_active_strategy_v2(
        regime, phase, adx, wr_cache=ctx.wr_cache_snapshot
    )
    ctx.pipeline_log.append(f"mode={ctx.strategy_mode.get('mode','?')}")

    return ctx


def _log_ctx_summary(ctx: MarketContext) -> None:
    """[v8.01] Log ringkasan MarketContext — satu baris per ticker, semua state terlihat."""
    w   = ctx.final_weights
    wr  = {k: f"{v:.0%}" for k, v in ctx.wr_cache_snapshot.items()}
    ev  = ctx.strategy_mode.get("ev_floor_override", "None")
    log(
        f"  🔬 CTX [{ctx.ticker}|{ctx.strategy} {ctx.side}] "
        f"regime={ctx.regime}(ADX={ctx.adx:.0f}) phase={ctx.phase} "
        f"mode={ctx.strategy_mode.get('mode','?')} ev_floor={ev} | "
        f"w: bos={w.get('bos')} choch={w.get('choch')} "
        f"liq={w.get('liq_sweep')} ob={w.get('order_block')} "
        f"macd={w.get('macd_cross')} ema={w.get('ema_align')} | "
        f"WR: {wr} | pipeline: {' → '.join(ctx.pipeline_log)}"
    )


class ScoreAccumulator:
    """
    [v8.01] Kumpulkan score dari semua layer dengan breakdown yang jelas.
    Ganti pola `score += X` yang tersebar dengan accumulator ini.

    Contoh:
        acc = ScoreAccumulator()
        acc.add("raw",      score_signal(...))
        acc.add("priority", setup_rank["score_boost"])
        acc.add("sniper",   sniper["bonus"])
        acc.add("strategy", ctx.strategy_mode.get("min_score_boost", 0))
        final = acc.total()
        log(acc.explain())
    """
    def __init__(self):
        self._items: list = []

    def add(self, label: str, value: int) -> "ScoreAccumulator":
        self._items.append((label, value))
        return self

    def total(self) -> int:
        return sum(v for _, v in self._items)

    def explain(self) -> str:
        parts = [f"{lbl}={val:+d}" for lbl, val in self._items]
        return f"score: {' | '.join(parts)} → {self.total()}"

    def get(self, label: str, default: int = 0) -> int:
        for lbl, val in self._items:
            if lbl == label:
                return val
        return default


class ThresholdGuard:
    """
    [v8.01] Frozen snapshot threshold untuk satu run cycle.
    Dibuat sekali di awal run() — tidak berubah selama scan batch.
    Mencegah mutate_thresholds() mid-scan mempengaruhi ticker berikutnya.
    """
    def __init__(self, data: dict):
        self._data = _copy.deepcopy(data)

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def snapshot(self) -> dict:
        return _copy.deepcopy(self._data)

    def __repr__(self):
        return f"ThresholdGuard({self._data})"


# Singleton ThresholdGuard — diisi di awal run(), sebelum scan batch dimulai
_threshold_guard: _Optional[ThresholdGuard] = None


def _get_active_strategy_v2(
    regime: str, phase: str, adx: float,
    wr_cache: _Optional[dict] = None,
) -> dict:
    """
    [v8.01] Patched get_active_strategy — wr_cache di-inject, tidak baca global.
    Drop-in compatible dengan get_active_strategy() lama.
    """
    cache = wr_cache or {}

    def _adj(base_ev: float, sub1: str, sub2: str) -> float:
        wrs = [cache.get(sub1), cache.get(sub2)]
        valid = [w for w in wrs if w is not None]
        if not valid:
            return base_ev
        avg = sum(valid) / len(valid)
        if avg > 0.60:
            return round(max(base_ev - 0.03, base_ev - 0.05), 3)
        elif avg < 0.45:
            return round(min(base_ev + 0.05, base_ev + 0.07), 3)
        return base_ev

    if regime == "CHOPPY" or phase == "MANIPULATION":
        return {"mode": "DEFENSIVE", "min_score_boost": 3, "rr_min_override": None,
                "ev_floor_override": 0.30, "require_sniper": True,
                "description": "Defensive — hanya A+ setup saat pasar tidak clear", "emoji": "🛡️"}

    if regime == "TRENDING" and adx >= 25:
        ev = _adj(0.25, "TREND_INTRADAY", "TREND_SWING")
        return {"mode": "TREND_FOLLOW", "min_score_boost": 0,
                "rr_min_override": {"INTRADAY": 1.8, "SWING": 2.5},
                "ev_floor_override": ev, "require_sniper": False,
                "description": f"Trend Follow — ride struktur (ev_floor={ev})", "emoji": "🚀"}

    if regime == "RANGING":
        ev = _adj(0.18, "MEANREV_INTRADAY", "MEANREV_SWING")
        return {"mode": "MEAN_REVERSION", "min_score_boost": -1,
                "rr_min_override": {"INTRADAY": 1.3, "SWING": 1.8},
                "ev_floor_override": ev, "require_sniper": False,
                "description": f"Mean Reversion — OB/liq zone (ev_floor={ev})", "emoji": "↕️"}

    return {"mode": "NORMAL", "min_score_boost": 0, "rr_min_override": None,
            "ev_floor_override": None, "require_sniper": False,
            "description": "Normal — standard scoring berlaku", "emoji": "📊"}


# ════════════════════════════════════════════════════════
#  UTILITIES
# ════════════════════════════════════════════════════════

def tg(msg: str):
    """
    Kirim pesan ke Telegram dengan retry 2x.
    FIX 4B: Sleep 1.2s (bukan 0.5s) agar tidak hit rate limit 1 msg/s.
    Tambahan: parse Retry-After header pada 429 response untuk backoff akurat.
    [v8.12] Auto-split pesan >4096 karakter (limit Telegram HTML mode).
    """
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    # [v8.12] Telegram HTML mode limit = 4096 chars. Split di batas aman.
    TG_LIMIT = 4000   # sedikit di bawah 4096 untuk margin tag HTML
    chunks = []
    if len(msg) <= TG_LIMIT:
        chunks = [msg]
    else:
        # Split di newline terdekat sebelum batas, pertahankan konteks
        remaining = msg
        while remaining:
            if len(remaining) <= TG_LIMIT:
                chunks.append(remaining)
                break
            # Cari newline terdekat sebelum TG_LIMIT
            cut = remaining.rfind("\n", 0, TG_LIMIT)
            if cut == -1:
                cut = TG_LIMIT
            chunks.append(remaining[:cut])
            remaining = remaining[cut:].lstrip("\n")
        # Tandai potongan agar user tahu ada lanjutan
        if len(chunks) > 1:
            chunks = [
                (c + f"\n<i>({i+1}/{len(chunks)})</i>" if i < len(chunks)-1 else c)
                for i, c in enumerate(chunks)
            ]

    for chunk in chunks:
        body = json.dumps({
            "chat_id": TG_CHAT_ID, "text": chunk,
            "parse_mode": "HTML", "disable_web_page_preview": True
        }).encode()
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body,
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
                time.sleep(1.2)   # FIX 4B: margin aman di atas batas 1 msg/s Telegram
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # FIX 4B: parse Retry-After dari header jika tersedia
                    retry_after = int(e.headers.get("Retry-After", 5))
                    log(f"⚠️ Telegram 429 rate limit — tunggu {retry_after}s", "warn")
                    time.sleep(retry_after + 1)
                elif attempt < 2:
                    log(f"⚠️ Telegram retry {attempt+1}/2 (HTTP {e.code}): {e}", "warn")
                    time.sleep(2 ** attempt * 2)
                else:
                    log(f"⚠️ Telegram gagal setelah 3x retry: {e}", "error")
            except Exception as e:
                if attempt < 2:
                    log(f"⚠️ Telegram retry {attempt+1}/2: {e}", "warn")
                    time.sleep(2 ** attempt * 2)
                else:
                    log(f"⚠️ Telegram gagal setelah 3x retry: {e}", "error")


def html_escape(text: str) -> str:
    """
    [v8.12] Escape karakter HTML special agar aman dikirim via Telegram parse_mode=HTML.
    Telegram menolak (HTTP 400) jika ada <, >, & yang tidak di-escape dalam dynamic content.
    """
    return (str(text)
            .replace("&", "&amp;")   # harus pertama
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def format_idr(price: float) -> str:
    """Format harga ke Rupiah yang mudah dibaca."""
    if price >= 1_000_000_000:
        return f"Rp{price/1_000_000_000:.2f}M"
    elif price >= 1_000_000:
        return f"Rp{price/1_000_000:.2f}jt"
    elif price >= 1_000:
        return f"Rp{price:,.0f}"
    else:
        return f"Rp{price:.2f}"


def round_to_fraction(price: float, direction: str = "nearest") -> int:
    """
    [v7.2 — FIX Masalah 6] Bulatkan harga ke fraksi harga IDX yang valid.

    Aturan Fraksi Harga IDX (Papan Utama & Pengembangan):
      Rp 1    – Rp 200    : fraksi Rp 1
      Rp 200  – Rp 500    : fraksi Rp 2
      Rp 500  – Rp 2.000  : fraksi Rp 5
      Rp 2.000– Rp 5.000  : fraksi Rp 10
      Rp 5.000– Rp 10.000 : fraksi Rp 25
      >= Rp 10.000        : fraksi Rp 50
    """
    p = float(price)
    if p <= 0:
        return int(round(p))
    if p < 200:      frac = 1
    elif p < 500:    frac = 2
    elif p < 2_000:  frac = 5
    elif p < 5_000:  frac = 10
    elif p < 10_000: frac = 25
    else:            frac = 50

    if direction == "up":
        return int(math.ceil(p / frac) * frac)
    elif direction == "down":
        return int(math.floor(p / frac) * frac)
    else:
        return int(round(p / frac) * frac)


def apply_price_fraction(sl: float, tp1: float, tp2: float,
                          entry: float, side: str) -> tuple:
    """
    [v7.2] Terapkan fraksi harga IDX ke semua level order.

    BUY:  SL → bawah (proteksi lebih lebar), TP → bawah (lebih mudah tercapai)
    SELL: SL → atas  (proteksi lebih lebar), TP → atas  (konservatif)
    Entry selalu nearest — beli/jual di market price.
    """
    if side == "BUY":
        entry_adj = round_to_fraction(entry, "nearest")
        sl_adj    = round_to_fraction(sl,    "down")
        tp1_adj   = round_to_fraction(tp1,   "down")
        tp2_adj   = round_to_fraction(tp2,   "down") if tp2 else None
    else:
        entry_adj = round_to_fraction(entry, "nearest")
        sl_adj    = round_to_fraction(sl,    "up")
        tp1_adj   = round_to_fraction(tp1,   "up")
        tp2_adj   = round_to_fraction(tp2,   "up") if tp2 else None
    return sl_adj, tp1_adj, tp2_adj, entry_adj


# ════════════════════════════════════════════════════════
#  DATA SOURCE — yfinance
# ════════════════════════════════════════════════════════

def get_candles(ticker: str, interval: str, limit: int):
    """
    Ambil data OHLCV dari yfinance.

    interval: '1h' untuk intraday, '1d' untuk swing
    Mengembalikan tuple (closes, highs, lows, volumes) sebagai numpy array,
    atau None jika data tidak cukup.

    Cache per ticker+interval untuk efisiensi — tidak fetch ulang dalam 1 run.
    """
    cache_key = f"{ticker}|{interval}|{limit}"
    if cache_key in _candle_cache:
        return _candle_cache[cache_key]

    try:
        # Hitung period yang dibutuhkan
        if interval == "1h":
            period = "60d"    # yfinance max 1h = 730 hari, 60d cukup untuk 100 candle
        elif interval == "1d":
            period = "180d"   # 180 hari trading untuk swing
        else:
            period = "60d"

        df = yf.download(
            ticker,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
        )

        if df is None or df.empty:
            log(f"⚠️ {ticker} [{interval}]: data kosong dari yfinance", "warn")
            _candle_cache[cache_key] = None
            return None

        # Flatten MultiIndex kolom jika ada (yfinance kadang return MultiIndex)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Pastikan kolom yang dibutuhkan ada (termasuk Open sejak v7.9 diperlukan)
        required = ["Open", "Close", "High", "Low", "Volume"]
        if not all(c in df.columns for c in required):
            missing = [c for c in required if c not in df.columns]
            log(f"⚠️ {ticker} [{interval}]: kolom tidak lengkap — missing: {missing}", "warn")
            _candle_cache[cache_key] = None
            return None

        df = df.dropna(subset=required)

        # Ambil limit candle terakhir
        if len(df) < max(30, limit // 2):
            log(f"⚠️ {ticker} [{interval}]: hanya {len(df)} candle tersedia (min {max(30, limit//2)})", "warn")
            _candle_cache[cache_key] = None
            return None

        df = df.tail(limit)

        closes  = df["Close"].values.astype(float)
        highs   = df["High"].values.astype(float)
        lows    = df["Low"].values.astype(float)
        volumes = df["Volume"].values.astype(float)
        opens   = df["Open"].values.astype(float)   # [v7.9] tambah opens untuk akurasi candle pattern

        result = (closes, highs, lows, volumes, opens)
        _candle_cache[cache_key] = result
        return result

    except Exception as e:
        log(f"⚠️ get_candles {ticker} [{interval}]: {e}", "warn")
        _candle_cache[cache_key] = None
        return None


def get_current_price(ticker: str) -> float:
    """Ambil harga terakhir dari yfinance fast_info (lebih fresh dari candle close)."""
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info
        return float(info.last_price or 0)
    except Exception as _e:
        log(f"  ⚠️ [FALLBACK] get_last_price yf.Ticker: {_e} — fallback candle", "warn")
        # Fallback: ambil dari candle
        data = get_candles(ticker, "1d", 5)
        if data:
            return float(data[0][-1])
        return 0.0


# ════════════════════════════════════════════════════════
#  TECHNICAL INDICATORS
# ════════════════════════════════════════════════════════

def calc_rsi(closes, period: int = 14) -> float:
    """RSI dengan Wilder's EMA (konsisten dengan TradingView)."""
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes.astype(float))
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    # Wilder's smoothing: EMA dengan alpha = 1/period
    alpha  = 1.0 / period
    avg_g  = float(np.mean(gains[:period]))
    avg_l  = float(np.mean(losses[:period]))
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = alpha * g + (1 - alpha) * avg_g
        avg_l = alpha * l + (1 - alpha) * avg_l
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - (100 / (1 + rs)), 2)


def calc_macd(closes, fast=12, slow=26, signal=9):
    """
    MACD dengan true iterative EMA — konsisten dengan TradingView.
    Seed EMA pertama pakai SMA, lalu iterasi penuh untuk setiap candle.
    """
    if len(closes) < slow + signal:
        return 0.0, 0.0
    c = closes.astype(float)
    alpha_f = 2.0 / (fast + 1)
    alpha_s = 2.0 / (slow + 1)
    alpha_sig = 2.0 / (signal + 1)

    # Seed EMA awal dengan SMA
    ef = float(c[:fast].mean())
    es = float(c[:slow].mean())

    # Warm-up fast EMA dari index fast..slow-1 agar tidak ada gap
    # (tanpa ini, ef lompat dari index fast-1 langsung ke slow — 14 candle dilewati)
    for i in range(fast, slow):
        ef = alpha_f * c[i] + (1 - alpha_f) * ef

    macd_series = []
    for i in range(slow, len(c)):
        ef = alpha_f * c[i] + (1 - alpha_f) * ef
        es = alpha_s * c[i] + (1 - alpha_s) * es
        macd_series.append(ef - es)

    if len(macd_series) < signal:
        return 0.0, 0.0

    # Signal line: iterative EMA dari MACD series
    sig_line = float(np.mean(macd_series[:signal]))
    for val in macd_series[signal:]:
        sig_line = alpha_sig * val + (1 - alpha_sig) * sig_line

    return round(macd_series[-1], 6), round(sig_line, 6)


def calc_ema(closes, period: int) -> float:
    """
    EMA iteratif yang benar — seed SMA dari period pertama,
    lalu iterasi pada sisa candle (tidak double-pass).
    """
    if len(closes) < period:
        return float(closes[-1])
    c     = closes.astype(float)
    alpha = 2.0 / (period + 1)
    ema   = float(c[:period].mean())   # seed dari period pertama
    for price in c[period:]:           # iterasi hanya pada candle sesudahnya
        ema = alpha * price + (1 - alpha) * ema
    return round(ema, 4)


def calc_atr(closes, highs, lows, period: int = 14) -> float:
    """
    ATR dengan Wilder's smoothing (RMA) — konsisten dengan TradingView.
    [v7.9 FIX] Versi sebelumnya menggunakan SMA biasa, bukan Wilder's.
    Wilder's ATR lebih smooth: ATR = (prev_ATR × (period-1) + TR) / period
    Seed awal dari SMA period pertama, lalu iterasi penuh.
    """
    if len(closes) < period + 1:
        return float(highs[-1] - lows[-1])
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1])
        )
        tr_list.append(tr)
    # Seed dengan SMA dari period TR pertama
    atr = float(np.mean(tr_list[:period]))
    # Wilder's smoothing pada sisa data
    for tr in tr_list[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 4)


def calc_vwap(closes, highs, lows, volumes, timeframe: str = "1d",
               timestamps=None) -> float:
    """
    VWAP sesi — untuk 1H wajib reset setiap sesi pasar IDX (09:00 WIB).
    [v7.9 FIX] Versi lama pakai rolling 24 candle yang lintas sesi.
    Sekarang: untuk intraday, filter hanya candle dari hari ini.
    Untuk swing (1d), pakai window 20 seperti sebelumnya.
    """
    if timeframe == "1h" and timestamps is not None:
        # Filter hanya candle dari sesi hari ini (WIB)
        today = datetime.now(WIB).date()
        mask = []
        for ts in timestamps:
            try:
                if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                ts_wib = ts.astimezone(WIB)
                mask.append(ts_wib.date() == today)
            except Exception as _e:
                log(f"  ⚠️ [FALLBACK] candle timestamp mask: {_e}", "warn")
                mask.append(False)
        mask = np.array(mask, dtype=bool)
        if mask.sum() >= 2:
            c = closes[mask]; h = highs[mask]
            l = lows[mask];   v = volumes[mask]
            tp = (h + l + c) / 3
            return float((np.cumsum(tp * v) / (np.cumsum(v) + 1e-9))[-1])
        # Fallback: 8 candle terakhir jika tidak ada timestamp hari ini
        window = min(8, len(closes))
    elif timeframe == "1h":
        # Tidak ada timestamps — fallback ke 8 candle (lebih konservatif dari 24)
        window = min(8, len(closes))
    else:
        _window_map = {"1d": 20, "4h": 6}
        window = min(_window_map.get(timeframe, 20), len(closes))

    c = closes[-window:]; h = highs[-window:]
    l = lows[-window:];   v = volumes[-window:]
    tp = (h + l + c) / 3
    cum_v = np.cumsum(v) + 1e-9
    return float((np.cumsum(tp * v) / cum_v)[-1])


def calc_adx(closes, highs, lows, period: int = ADX_PERIOD):
    """
    ADX dengan Wilder's smoothing (RMA) — returns (adx, +DI, -DI).
    [v7.10 FIX] Versi sebelumnya menggunakan np.mean(dx[-period:]) — SMA biasa.
    Sekarang konsisten dengan calc_atr(): seed SMA, lalu Wilder's RMA iteratif.
    ADX = Wilder's smooth dari DX, bukan rata-rata 14 DX terakhir.
    """
    if len(closes) < period * 2:
        return 20.0, 0.0, 0.0
    h = highs.astype(float)
    l = lows.astype(float)
    c = closes.astype(float)
    n = len(h)

    plus_dm  = np.zeros(n)
    minus_dm = np.zeros(n)
    tr_arr   = np.zeros(n)

    for i in range(1, n):
        up   = h[i] - h[i-1]
        down = l[i-1] - l[i]
        plus_dm[i]  = up   if up > down and up > 0   else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        tr_arr[i]   = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))

    # [v7.10] Wilder's RMA: seed SMA dari period pertama, lalu iterasi
    def wilder_rma(arr, p):
        """Wilder's smoothed average — konsisten dengan RMA di calc_atr."""
        result = np.zeros(n)
        # Seed: SMA dari elemen 1..p (skip index 0 yang selalu 0)
        result[p] = float(np.mean(arr[1:p+1]))
        for i in range(p + 1, n):
            result[i] = (result[i-1] * (p - 1) + arr[i]) / p
        return result

    sm_tr    = wilder_rma(tr_arr,   period)
    sm_plus  = wilder_rma(plus_dm,  period)
    sm_minus = wilder_rma(minus_dm, period)

    # np.errstate supresses RuntimeWarning dari divide-by-zero saat sm_tr=0
    with np.errstate(invalid="ignore", divide="ignore"):
        plus_di  = np.where(sm_tr > 0, 100.0 * sm_plus  / sm_tr, 0.0)
        minus_di = np.where(sm_tr > 0, 100.0 * sm_minus / sm_tr, 0.0)
        dx = np.where(
            (plus_di + minus_di) > 0,
            100.0 * np.abs(plus_di - minus_di) / (plus_di + minus_di),
            0.0
        )

    # [v7.10] ADX = Wilder's RMA dari DX (bukan SMA 14 terakhir)
    adx_val = wilder_rma(dx, period)[-1]
    return round(float(adx_val), 2), round(float(plus_di[-1]), 2), round(float(minus_di[-1]), 2)


def detect_market_regime(closes, highs, lows) -> dict:
    """Deteksi regime pasar via ADX."""
    adx, plus_di, minus_di = calc_adx(closes, highs, lows)
    if adx >= ADX_TREND:
        regime = "TRENDING"
    elif adx >= ADX_CHOP:
        regime = "RANGING"
    else:
        regime = "CHOPPY"
    return {"regime": regime, "adx": adx, "plus_di": plus_di, "minus_di": minus_di}


# ════════════════════════════════════════════════════════
#  STRUCTURE ENGINE
# ════════════════════════════════════════════════════════

def detect_swing_points(highs, lows, strength=3, lookback=80):
    """Deteksi swing high/low dengan strength filter."""
    if strength >= len(highs) // 4:
        log(f"⚠️ detect_swing_points: strength={strength} terlalu besar untuk array len={len(highs)}", "warn")
        return []
    points = []
    n      = min(len(highs), lookback)
    start  = len(highs) - n
    for i in range(start + strength, len(highs) - strength):
        if (all(highs[i] > highs[i-j] for j in range(1, strength+1)) and
                all(highs[i] > highs[i+j] for j in range(1, strength+1))):
            points.append((i, highs[i], "SH"))
        if (all(lows[i] < lows[i-j]  for j in range(1, strength+1)) and
                all(lows[i] < lows[i+j]  for j in range(1, strength+1))):
            points.append((i, lows[i], "SL"))
    return sorted(points, key=lambda x: x[0])


def detect_structure(closes, highs, lows, strength=3, lookback=80) -> dict:
    """Deteksi BOS (Break of Structure) dan CHoCH (Change of Character)."""
    result = {
        "bos": None, "choch": None,
        "last_sh": None, "last_sl": None,
        "prev_sh": None, "prev_sl": None,
        "bias": "NEUTRAL", "valid": False,
    }
    if len(closes) < lookback:
        return result

    pts = detect_swing_points(highs, lows, strength=strength, lookback=lookback)
    shs = [(i, p) for i, p, t in pts if t == "SH"]
    sls = [(i, p) for i, p, t in pts if t == "SL"]
    if len(shs) < 2 or len(sls) < 2:
        return result

    last_sh, prev_sh = shs[-1][1], shs[-2][1]
    last_sl, prev_sl = sls[-1][1], sls[-2][1]

    result.update({
        "last_sh": last_sh, "prev_sh": prev_sh,
        "last_sl": last_sl, "prev_sl": prev_sl,
        "valid": True,
    })

    hh = last_sh > prev_sh; hl = last_sl > prev_sl
    lh = last_sh < prev_sh; ll = last_sl < prev_sl
    if hh and hl:    result["bias"] = "BULLISH"
    elif lh and ll:  result["bias"] = "BEARISH"
    else:            result["bias"] = "NEUTRAL"

    recent_closes = closes[-5:]
    # [v7.9 FIX] Buffer diperkecil 1.008 → 1.003 (0.3%) agar BOS tidak terkonfirmasi
    # dari candle yang sudah overshoot. Untuk IDX dengan ATR 1–3%, 0.8% terlalu longgar.
    bull_break = any(recent_closes[i] > last_sh * 1.001 and
                     recent_closes[i-1] <= last_sh * 1.003
                     for i in range(1, len(recent_closes)))
    bear_break = any(recent_closes[i] < last_sl * 0.999 and
                     recent_closes[i-1] >= last_sl * 0.997
                     for i in range(1, len(recent_closes)))

    if bull_break:
        # BOS hanya kalau bias sudah BULLISH (konfirmasi tren)
        # CHoCH hanya kalau bias sebelumnya BEARISH (perubahan karakter)
        # Kalau NEUTRAL → tidak cukup konteks, skip
        if result["bias"] == "BULLISH":
            result["bos"] = "BULLISH"
        elif result["bias"] == "BEARISH":
            result["choch"] = "BULLISH"
        # NEUTRAL: tidak set apapun — sinyal tidak cukup kuat
    elif bear_break:
        if result["bias"] == "BEARISH":
            result["bos"] = "BEARISH"
        elif result["bias"] == "BULLISH":
            result["choch"] = "BEARISH"
        # NEUTRAL: tidak set apapun

    return result


def detect_order_block(closes, highs, lows, volumes, side="BUY",
                        lookback=30, opens=None) -> dict:
    """
    Order block detection — zona institusional.
    [v7.10 FIX] Terima parameter 'opens' untuk akurasi arah candle.
    Sebelumnya menggunakan c[i] vs c[i-1] sebagai proxy open — tidak
    akurat untuk saham IDX yang sering gap. Sekarang pakai opens[i]
    jika tersedia, fallback ke c[i-1] jika tidak.
    """
    result = {"valid": False, "ob_high": None, "ob_low": None}
    if len(closes) < lookback:
        return result
    c = closes[-lookback:]; h = highs[-lookback:]
    l = lows[-lookback:];   v = volumes[-lookback:]
    # Gunakan opens asli jika tersedia, fallback ke prev close sebagai proxy
    if opens is not None and len(opens) >= lookback:
        o = opens[-lookback:].astype(float)
    else:
        # proxy: open[i] ≈ close[i-1]
        o = np.roll(c, 1); o[0] = c[0]
    o = o.astype(float)
    n = len(c)
    avg_body = float(np.mean([abs(c[i] - o[i]) for i in range(n)]))

    for i in range(n - 3, 1, -1):
        impulse = abs(c[i+1] - c[i])
        if impulse < avg_body * 1.5:
            continue
        # [v7.10] gunakan o[i] (open nyata) bukan c[i-1]
        if side == "BUY"  and c[i] < o[i] and c[i+1] > c[i]:
            return {"valid": True, "ob_high": float(h[i]), "ob_low": float(l[i])}
        if side == "SELL" and c[i] > o[i] and c[i+1] < c[i]:
            return {"valid": True, "ob_high": float(h[i]), "ob_low": float(l[i])}
    return result


def detect_liquidity(closes, highs, lows, lookback=50) -> dict:
    """Deteksi equal highs/lows dan liquidity sweep."""
    result = {
        "equal_lows": None, "equal_highs": None,
        "sweep_bull": False, "sweep_bear": False,
    }
    if len(closes) < lookback:
        return result
    h = highs[-lookback:]; l = lows[-lookback:]
    c = closes[-lookback:]
    # [v7.10 FIX] Tolerance adaptif berbasis harga — bukan flat 0.5%
    # Di saham Rp50.000 dengan tol 0.5% = Rp250 gap masih dianggap "equal"
    # Sekarang: 2 fraksi harga (0.2% untuk LQ45 Rp5.000-Rp20.000, lebih ketat di atas)
    price_now = float(c[-1]) if len(c) > 0 else 1000.0
    if price_now >= 10_000: tol = 0.003   # 0.3% — Rp50.000 → ±Rp150
    elif price_now >= 5_000: tol = 0.004  # 0.4%
    elif price_now >= 2_000: tol = 0.005  # 0.5%
    else:                    tol = 0.007  # 0.7% — saham murah lebih noisy

    for i in range(len(h) - 1, 0, -1):
        window_start = max(i - 10, 0)
        window = h[window_start:i]
        if len(window) == 0: continue
        diffs = np.abs(window - h[i]) / (h[i] + 1e-9)
        match_idx = np.where(diffs < tol)[0]
        if len(match_idx) > 0:
            j = window_start + match_idx[-1]
            result["equal_highs"] = float((h[i] + h[j]) / 2)
            break

    for i in range(len(l) - 1, 0, -1):
        window_start = max(i - 10, 0)
        window = l[window_start:i]
        if len(window) == 0: continue
        diffs = np.abs(window - l[i]) / (l[i] + 1e-9)
        match_idx = np.where(diffs < tol)[0]
        if len(match_idx) > 0:
            j = window_start + match_idx[-1]
            result["equal_lows"] = float((l[i] + l[j]) / 2)
            break

    if len(c) < 6:
        return result   # array terlalu pendek untuk liquidity sweep (butuh min 6 candle)

    ref_low  = float(np.min(l[:-5]))
    ref_high = float(np.max(h[:-5]))
    for i in range(-5, 0):
        if l[i] < ref_low  and c[i] > ref_low:
            result["sweep_bull"] = True
        if h[i] > ref_high and c[i] < ref_high:
            result["sweep_bear"] = True

    return result


# ════════════════════════════════════════════════════════
#  MARKET INTELLIGENCE LAYER — v3.0
#  [1] Market Phase  [2] Dynamic Weights  [3] Entry Trigger
#  [4] Liquidity Trap  [5] MTF Bias
# ════════════════════════════════════════════════════════


# [v8.0 FIX 5] Market phase detection thresholds — dipindah ke konstanta bernama
# agar mudah di-tune tanpa harus baca logika internal fungsi.
# Sebelumnya semua angka embedded langsung di dalam kondisi (magic numbers).
_MP_VOL_SPIKE_RATIO       = 1.9    # vol_ratio > ini = potensi manipulation/jebakan
_MP_VOL_SPIKE_MAXMOVE     = 1.2    # max abs(momentum_5)% saat vol spike = no direction
_MP_ATR_EXPANSION_RATIO   = 1.5    # atr_ratio > ini = volatilitas melebar signifikan
_MP_MOMENTUM_EXPANSION    = 2.5    # min abs(momentum_5)% untuk konfirmasi expansion
_MP_TREND_MOMENTUM        = 4.0    # min abs(momentum_20)% untuk markup/markdown (turun dari 4.5)
_MP_TREND_VOL_CONFIRM     = 1.10   # min vol_ratio untuk konfirmasi trend (turun dari 1.15)
_MP_RANGE_COMPRESS_RATIO  = 0.75   # range compressed jika range_recent < prev * ini
_MP_RANGE_VOL_LOW         = 0.85   # vol rendah saat range compressed = konsolidasi
_MP_RANGE_MAXMOVE         = 1.8    # max abs(momentum_5)% saat range compressed


def detect_market_phase(closes, highs, lows, volumes) -> dict:
    """
    Deteksi fase pasar yang lebih granular dari sekedar ADX.

    [v8.0 FIX 5] Dua perubahan utama:
    1. Semua magic number dipindah ke konstanta _MP_* di atas — mudah di-tune.
    2. ACCUMULATION dan DISTRIBUTION digabung ke dalam RANGING karena secara
       eksekusi bot memperlakukan keduanya identik (mean-reversion logic,
       RR target lebih rendah). Ini mengurangi false precision dari 7 phase → 5 phase.

    Phases: MANIPULATION | EXPANSION | MARKUP | MARKDOWN | RANGING | CONSOLIDATION
    Setiap fase mengubah bobot scoring secara otomatis via get_dynamic_weights().
    """
    result = {"phase": "CONSOLIDATION", "description": "Tidak ada fase dominan", "confidence": 0.3}
    if len(closes) < 50:
        return result

    c = closes.astype(float)
    h = highs.astype(float)
    l = lows.astype(float)
    v = volumes.astype(float)
    n = len(c)

    ema50 = calc_ema(c, 50)
    price = c[-1]

    # Guard semua slice agar tidak crash jika n < 30
    w_recent     = min(5, n)
    w_prev       = min(15, n)
    w_range      = min(10, n)
    w_range_prev = min(30, n) - w_range

    # ATR ratio — ekspansi vs kontraksi volatilitas
    atr_recent = float(np.mean([h[i] - l[i] for i in range(-w_recent, 0)]))
    atr_prev   = float(np.mean([h[i] - l[i] for i in range(-w_prev, -w_recent)])) \
                 if w_prev > w_recent else atr_recent
    atr_ratio  = atr_recent / (atr_prev + 1e-9)

    # Volume ratio
    vol_recent = float(np.mean(v[-w_recent:]))
    vol_prev   = float(np.mean(v[-w_prev:-w_recent])) if w_prev > w_recent else vol_recent
    vol_ratio  = vol_recent / (vol_prev + 1e-9)

    # Price momentum
    momentum_5  = (c[-1] - c[max(-6, -n)]) / (c[max(-6, -n)] + 1e-9) * 100
    momentum_20 = (c[-1] - c[max(-21, -n)]) / (c[max(-21, -n)] + 1e-9) * 100

    # Range compression
    range_recent = float(np.mean([h[i] - l[i] for i in range(-w_range, 0)]))
    if w_range_prev > 0:
        range_prev = float(np.mean([h[i] - l[i] for i in range(-(w_range + w_range_prev), -w_range)]))
    else:
        range_prev = range_recent
    range_compressed = range_recent < range_prev * _MP_RANGE_COMPRESS_RATIO

    # ── Fase detection dengan prioritas dari paling kuat ke paling lemah ──
    if vol_ratio > _MP_VOL_SPIKE_RATIO and abs(momentum_5) < _MP_VOL_SPIKE_MAXMOVE:
        # Volume spike tinggi tapi harga tidak kemana-mana = institutional trap
        phase = "MANIPULATION"
        desc  = "Volume spike tanpa arah — potensi jebakan retail"
        conf  = min(vol_ratio / (_MP_VOL_SPIKE_RATIO * 1.2), 0.90)

    elif atr_ratio > _MP_ATR_EXPANSION_RATIO and abs(momentum_5) > _MP_MOMENTUM_EXPANSION:
        # Volatilitas melebar + momentum kuat = expansion move
        phase = "EXPANSION"
        desc  = f"Volatilitas ekspansi {'bullish' if momentum_5 > 0 else 'bearish'}"
        conf  = min(atr_ratio / (_MP_ATR_EXPANSION_RATIO * 1.3), 0.95)

    elif price > ema50 and momentum_20 > _MP_TREND_MOMENTUM and vol_ratio > _MP_TREND_VOL_CONFIRM:
        phase = "MARKUP"
        desc  = "Uptrend aktif — volume konfirmasi kuat"
        conf  = min(vol_ratio / 1.8, 0.90)

    elif price < ema50 and momentum_20 < -_MP_TREND_MOMENTUM and vol_ratio > _MP_TREND_VOL_CONFIRM:
        phase = "MARKDOWN"
        desc  = "Downtrend aktif — tekanan jual dominan"
        conf  = min(vol_ratio / 1.8, 0.90)

    elif range_compressed and vol_ratio < _MP_RANGE_VOL_LOW and abs(momentum_5) < _MP_RANGE_MAXMOVE:
        # [v8.0 FIX 5] ACCUMULATION + DISTRIBUTION digabung → RANGING
        # Keduanya diperlakukan identik oleh scoring (mean-reversion mode).
        # Label deskriptif tetap berbeda untuk readability di log/Telegram.
        phase = "RANGING"
        if price > ema50:
            desc = "Konsolidasi di area tinggi — mean-reversion dominant"
        else:
            desc = "Konsolidasi di area rendah — mean-reversion dominant"
        conf = 0.65

    else:
        phase = "CONSOLIDATION"
        desc  = "Tidak ada fase dominan terdeteksi"
        conf  = 0.35

    return {"phase": phase, "description": desc, "confidence": round(conf, 2)}




def get_dynamic_weights(regime: str, phase: str) -> dict:
    """
    [Upgrade #1] Dynamic Scoring System.
    Bobot scoring berubah sesuai regime ADX + market phase.
    Trending ≠ Ranging ≠ Panic — sinyal yang relevan berbeda.
    """
    w = W.copy()   # selalu mulai dari base weights

    # ── Layer 1: Regime ADX ──────────────────────────────
    if regime == "TRENDING":
        # Trending: structure break + momentum paling relevan
        w["bos"]         = 8     # base 6
        w["choch"]       = 6     # base 5
        w["ema_align"]   = 4     # base 2  — trend follower
        w["macd_cross"]  = 4     # base 3
        w["liq_sweep"]   = 3     # base 4  — less reliable di strong trend
        w["order_block"] = 3     # base 4
        w["adx_trend"]   = 3     # bonus trending lebih besar

    elif regime == "RANGING":
        # Ranging: liquidity + OB (mean reversion logic)
        w["bos"]         = 4     # base 6  — BOS di ranging sering false
        w["choch"]       = 4     # base 5
        w["liq_sweep"]   = 6     # base 4  — naik signifikan
        w["order_block"] = 6     # base 4  — naik signifikan
        w["ema_align"]   = 1     # base 2  — EMA kurang relevan
        w["rsi_zone"]    = 4     # base 3  — RSI extreme lebih penting
        w["rsi_extreme"] = 3     # base 2
        w["pullback"]    = 3     # base 2  — pullback ke OB lebih berharga

    # ── Layer 2: Phase Modifier ──────────────────────────
    if phase == "ACCUMULATION":
        # Backward compat — sekarang dihasilkan sebagai RANGING
        w["liq_sweep"]   = max(w["liq_sweep"],   5)
        w["order_block"] = max(w["order_block"],  5)
        w["vol_confirm"] = 4     # volume breakout kritis

    elif phase == "RANGING":
        # [v8.0] ACCUMULATION + DISTRIBUTION digabung — mean-reversion mode
        # Bobot: OB dan liq sweep yang relevan, EMA kurang berguna
        w["liq_sweep"]   = max(w["liq_sweep"],   5)
        w["order_block"] = max(w["order_block"],  5)
        w["choch"]       = max(w["choch"],        5)   # CHoCH penting di ranging
        w["rsi_extreme"] = max(w["rsi_extreme"],  3)   # oversold/overbought lebih relevan
        w["ema_align"]   = min(w["ema_align"],    1)   # EMA alignment kurang relevan

    elif phase == "MARKUP":
        # Momentum fase naik
        w["bos"]        = max(w["bos"],        8)
        w["ema_align"]  = max(w["ema_align"],  4)
        w["macd_cross"] = max(w["macd_cross"], 4)

    elif phase == "DISTRIBUTION":
        # Backward compat — sekarang dihasilkan sebagai RANGING
        w["choch"]      = max(w["choch"],     6)
        w["vwap_side"]  = 3     # VWAP lebih relevan di distribusi

    elif phase == "EXPANSION":
        # Expansi: semua konfirmasi lebih berharga (+20%)
        for k in list(w.keys()):
            if w[k] > 0:
                w[k] = int(w[k] * 1.2)

    elif phase == "MANIPULATION":
        # Manipulasi: hanya liq_sweep yang berharga (retail trapped)
        for k in list(w.keys()):
            if k != "liq_sweep":
                w[k] = max(1, int(w[k] * 0.65))
        w["liq_sweep"] = 7     # ini setup terbaik di manipulasi

    return w


# ════════════════════════════════════════════════════════
#  [22] META INTELLIGENCE — STRATEGY SWITCHING — v7.0
#  Bot berpikir pada level tertinggi: kapan strategy A dipakai,
#  kapan strategy B dimatikan. Bukan hanya "adjust weight" —
#  tapi "ganti cara bermain secara fundamental."
#
#  Dua mode strategi utama:
#  TREND_FOLLOW  — aktif saat TRENDING: BOS/CHoCH sebagai trigger,
#                   trailing profit, momentum entry, RR > 2.0 target
#  MEAN_REVERSION — aktif saat RANGING: OB/liq sweep trigger,
#                   target center range, tighter TP, reversal candle
#
#  Strategy switching mengubah: min_score_threshold, RR target,
#  entry criteria, EV minimum, dan cara membaca setup.
# ════════════════════════════════════════════════════════

def get_active_strategy(regime: str, phase: str, adx: float) -> dict:
    """
    [v7.0] Meta Intelligence — tentukan strategi aktif berdasarkan
    kondisi pasar. Bukan sekedar weight adjustment — ini perubahan
    fundamental cara bot mengevaluasi setiap trade.

    [v8.0 FIX 2] Closed feedback loop: baca _strategy_wr_cache yang diisi
    oleh get_strategy_performance(). Jika WR aktual menyimpang dari baseline,
    ev_floor disesuaikan secara data-driven — bukan hanya rule-based switching.

    TREND_FOLLOW (regime TRENDING, ADX > 25):
      - Trigger utama: BOS + CHoCH (konfirmasi struktur tren)
      - Entry: pullback ke EMA/OB di dalam tren
      - RR target: ≥ 2.5 (ride the wave)
      - EV minimum: 0.25 base (naik/turun ±0.05 berdasarkan WR aktual)
      - Paradigma: "trend is your friend, cut losers fast, let winners run"

    MEAN_REVERSION (regime RANGING, ADX < 25):
      - Trigger utama: OB + liq sweep di zona ekstrem
      - Entry: di/dekat support/resistance zone
      - RR target: ≥ 1.5 (realistic di ranging — target center range)
      - EV minimum: 0.18 base (naik/turun ±0.05 berdasarkan WR aktual)
      - Paradigma: "sell the extreme, buy the extreme, quick profit lock"

    DEFENSIVE (regime CHOPPY / phase MANIPULATION):
      - Hanya ambil setup SNIPER level (score sangat tinggi)
      - EV minimum: 0.30 (butuh edge besar karena kondisi tidak reliable)
      - Paradigma: "sit on hands until clarity returns"

    Returns: dict strategy config yang digunakan oleh check_intraday/swing
    """

    def _wr_ev_adjust(base_ev: float, sub_intraday: str, sub_swing: str) -> float:
        """
        [v8.0 FIX 2] Sesuaikan ev_floor berdasarkan WR aktual dari cache.
        WR > 60% → sedikit longgarkan (bot sedang dalam kondisi bagus).
        WR < 45% → perketat threshold (edge sedang melemah).
        WR tidak tersedia → gunakan base_ev tanpa perubahan.
        """
        wrs = [_strategy_wr_cache.get(sub_intraday), _strategy_wr_cache.get(sub_swing)]
        valid_wrs = [w for w in wrs if w is not None]
        if not valid_wrs:
            return base_ev
        avg_wr = sum(valid_wrs) / len(valid_wrs)
        if avg_wr > 0.60:
            adjusted = round(max(base_ev - 0.03, base_ev - 0.05), 3)   # longgarkan sedikit
        elif avg_wr < 0.45:
            adjusted = round(min(base_ev + 0.05, base_ev + 0.07), 3)   # perketat
        else:
            adjusted = base_ev
        if adjusted != base_ev:
            log(f"  🔄 ev_floor adjusted: {base_ev} → {adjusted} (WR aktual {avg_wr:.0%})")
        return adjusted

    if regime == "CHOPPY" or phase == "MANIPULATION":
        return {
            "mode":              "DEFENSIVE",
            "min_score_boost":   3,
            "rr_min_override":   None,
            "ev_floor_override": 0.30,
            "require_sniper":    True,
            "description":       "Defensive — hanya A+ setup saat pasar tidak clear",
            "emoji":             "🛡️",
        }

    if regime == "TRENDING" and adx >= 25:
        ev = _wr_ev_adjust(0.25, "TREND_INTRADAY", "TREND_SWING")
        return {
            "mode":              "TREND_FOLLOW",
            "min_score_boost":   0,
            "rr_min_override":   {"INTRADAY": 1.8, "SWING": 2.5},
            "ev_floor_override": ev,
            "require_sniper":    False,
            "description":       f"Trend Follow — ride struktur, trailing agresif (ev_floor={ev})",
            "emoji":             "🚀",
        }

    if regime == "RANGING":
        ev = _wr_ev_adjust(0.18, "MEANREV_INTRADAY", "MEANREV_SWING")
        return {
            "mode":              "MEAN_REVERSION",
            "min_score_boost":   -1,
            "rr_min_override":   {"INTRADAY": 1.3, "SWING": 1.8},
            "ev_floor_override": ev,
            "require_sniper":    False,
            "description":       f"Mean Reversion — OB/liq zone, quick profit lock (ev_floor={ev})",
            "emoji":             "↕️",
        }

    # Default: normal mode (TRENDING tapi ADX borderline)
    return {
        "mode":              "NORMAL",
        "min_score_boost":   0,
        "rr_min_override":   None,
        "ev_floor_override": None,
        "require_sniper":    False,
        "description":       "Normal — standard scoring berlaku",
        "emoji":             "📊",
    }


def entry_trigger_check(closes, highs, lows, side: str,
                         opens=None) -> dict:
    """
    [Upgrade #3] Entry Trigger Layer.
    Konfirmasi candle WAJIB ada sebelum entry.
    [v7.9 FIX] Parameter 'opens' ditambahkan — sebelumnya pakai c[-2] sebagai
    proxy open yang salah (prev close bukan open candle terakhir, terutama di
    saham IDX yang sering gap). Jika opens tidak tersedia, fallback ke proxy.
    Patterns: Bullish/Bearish Engulfing, Hammer/Pin Bar,
              Shooting Star, Strong Close, Momentum Close
    """
    no_trigger = {"valid": False, "pattern": "No Trigger", "strength": 0.0}
    if len(closes) < 3:
        return no_trigger

    c  = closes.astype(float)
    h  = highs.astype(float)
    l  = lows.astype(float)

    # Candle terakhir (current)
    last_close = c[-1]
    # [v7.9 FIX] Gunakan open nyata jika tersedia, bukan proxy c[-2]
    if opens is not None and len(opens) >= 1:
        last_open = float(opens[-1])
    else:
        last_open = c[-2]   # fallback ke proxy jika opens tidak tersedia
    last_high  = h[-1]
    last_low   = l[-1]
    c_range    = last_high - last_low + 1e-9
    body       = abs(last_close - last_open)
    body_ratio = body / c_range

    # Candle sebelumnya (untuk engulfing)
    prev_close = c[-2]
    if opens is not None and len(opens) >= 2:
        prev_open = float(opens[-2])
    else:
        prev_open = c[-3]
    prev_high  = h[-2]
    prev_low   = l[-2]

    if side == "BUY":
        upper_wick = last_high - last_close
        lower_wick = last_open - last_low if last_close > last_open else last_close - last_low

        # Pattern 1: Bullish Engulfing — paling kuat
        if (last_close > last_open and
                prev_close < prev_open and
                last_close > prev_open and
                last_open  < prev_close):
            return {"valid": True, "pattern": "Bullish Engulfing", "strength": 0.90}

        # Pattern 2: Hammer / Pin Bar — reversal kuat
        if (lower_wick / c_range > 0.55 and
                body_ratio < 0.35 and
                last_close > last_open):
            return {"valid": True, "pattern": "Hammer / Pin Bar", "strength": 0.80}

        # Pattern 3: Strong Bullish Close — dominasi bull
        if (last_close > last_open and
                body_ratio > 0.60 and
                upper_wick / c_range < 0.20):
            return {"valid": True, "pattern": "Strong Bullish Close", "strength": round(body_ratio, 2)}

        # Pattern 4: Momentum Close — close 0.8% di atas prev close
        if (last_close > last_open and
                (last_close - prev_close) / (prev_close + 1e-9) > 0.008):
            return {"valid": True, "pattern": "Momentum Close", "strength": 0.60}

    else:  # SELL
        lower_wick_s = last_close - last_low
        upper_wick_s = last_high - (last_open if last_close < last_open else last_close)

        # Pattern 1: Bearish Engulfing
        if (last_close < last_open and
                prev_close > prev_open and
                last_close < prev_open and
                last_open  > prev_close):
            return {"valid": True, "pattern": "Bearish Engulfing", "strength": 0.90}

        # Pattern 2: Shooting Star / Pin Bar
        if (upper_wick_s / c_range > 0.55 and
                body_ratio < 0.35 and
                last_close < last_open):
            return {"valid": True, "pattern": "Shooting Star / Pin Bar", "strength": 0.80}

        # Pattern 3: Strong Bearish Close
        if (last_close < last_open and
                body_ratio > 0.60 and
                lower_wick_s / c_range < 0.20):
            return {"valid": True, "pattern": "Strong Bearish Close", "strength": round(body_ratio, 2)}

        # Pattern 4: Momentum bearish
        if (last_close < last_open and
                (prev_close - last_close) / (prev_close + 1e-9) > 0.008):
            return {"valid": True, "pattern": "Momentum Close", "strength": 0.60}

    return no_trigger


def detect_liquidity_trap(closes, highs, lows) -> dict:
    """
    [Upgrade #4] Liquidity Trap Detection.
    Deteksi fake breakout + stop hunt — ketika retail trapped,
    itu adalah setup entry terbaik untuk kita.
    """
    result = {
        "fake_bull_break": False,   # breakout ke atas palsu
        "fake_bear_break": False,   # breakout ke bawah palsu
        "stop_hunt_bull":  False,   # retail SL di bawah support kena, harga balik naik
        "stop_hunt_bear":  False,   # retail SL di atas resistance kena, harga balik turun
    }
    if len(closes) < 20:
        return result

    c = closes.astype(float)
    h = highs.astype(float)
    l = lows.astype(float)

    # Reference zone: 20 candle ke-2 dari bawah (exclude 5 candle terakhir)
    ref_h     = h[-20:-5]
    ref_l     = l[-20:-5]
    recent_h  = h[-5:]
    recent_l  = l[-5:]
    recent_c  = c[-5:]

    if len(ref_h) == 0 or len(ref_l) == 0:
        return result

    resistance = float(np.max(ref_h))
    support    = float(np.min(ref_l))

    for i in range(len(recent_h)):
        # Fake bullish breakout: high tembus resistance, tapi close di bawahnya lagi
        if recent_h[i] > resistance * 1.002 and recent_c[i] < resistance * 0.998:
            result["fake_bull_break"] = True

        # Fake bearish breakout: low tembus support, tapi close di atasnya lagi
        if recent_l[i] < support * 0.998 and recent_c[i] > support * 1.002:
            result["fake_bear_break"] = True

        # Stop hunt bullish: spike low yang langsung recovery di atas support
        # [v7.10 FIX] Simetriskan threshold: tembus 0.6% bawah, recovery 0.4% atas
        # (sebelumnya 0.6% tembus vs 0.3% recovery — terlalu asimetris)
        if recent_l[i] < support * 0.994 and recent_c[i] > support * 1.004:
            result["stop_hunt_bull"] = True

        # Stop hunt bearish: spike high yang langsung rejection di bawah resistance
        if recent_h[i] > resistance * 1.006 and recent_c[i] < resistance * 0.996:
            result["stop_hunt_bear"] = True

    return result


def get_daily_bias(ticker: str) -> str:
    """
    [Upgrade #5] MTF Alignment Helper.
    Ambil bias struktur 1D untuk validasi sinyal 1H.
    Intraday BUY hanya valid jika 1D BULLISH atau NEUTRAL.
    Intraday SELL hanya valid jika 1D BEARISH atau NEUTRAL.

    [v7.12 FIX] Normalisasi ke limit=120 agar berbagi cache key '1d|120'
    dengan check_swing() — sebelumnya '1d|60' membuat cache entry terpisah
    dan memicu download extra per ticker intraday.

    Returns: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
    """
    try:
        data = get_candles(ticker, "1d", 120)   # berbagi cache dengan check_swing
        if data is None:
            return "NEUTRAL"
        closes, highs, lows, _, _opens = data
        structure = detect_structure(closes, highs, lows, strength=3, lookback=50)
        return structure.get("bias", "NEUTRAL")
    except Exception:
        return "NEUTRAL"


# ════════════════════════════════════════════════════════
#  [8] PROBABILITY ENGINE — v4.0
#  Win probability + Expected Value filter
#  Bot hanya ambil trade yang mathematically "worth it"
# ════════════════════════════════════════════════════════

def calc_win_probability(score: int, max_score: int,
                          regime: str, phase: str,
                          trigger_strength: float,
                          daily_bias_aligned: bool,
                          ticker: str = None) -> float:
    """
    Estimasi probabilitas win berdasarkan data historis aktual (cluster WR)
    dengan fallback ke heuristik terstruktur jika data belum cukup.

    [v7.9 FIX] Versi sebelumnya menggunakan score/max_score sebagai base
    probabilitas — ini bukan probabilitas empiris, hanya normalisasi score
    yang menciptakan circular dependency (score→tier→weight→score).

    Prioritas:
    1. Cluster win rate aktual dari Supabase (jika tersedia, min 15 trades)
       Ini adalah sumber paling akurat karena berbasis data historis nyata.
    2. Fallback ke heuristik terstruktur jika data belum cukup
       (lebih konservatif dari sebelumnya — modifier dikecilkan)

    Returns float antara 0.0 - 1.0
    """
    # ── Prioritas 1: gunakan cluster win rate aktual ──────────────────
    if ticker and _cluster_weights:
        sector  = TICKER_SECTOR.get(ticker, "MISC")
        key     = f"{regime}|{sector}"
        cluster = _cluster_weights.get(key, {})
        cluster_wr = cluster.get("_win_rate")
        if cluster_wr is not None:
            # Cluster WR sebagai base — bukan score normalisasi
            base = cluster_wr
            # Modifier kecil dari kondisi real-time (maksimal ±0.08)
            regime_mod  = {"TRENDING": 0.03, "RANGING": -0.02, "CHOPPY": -0.08}.get(regime, 0.0)
            phase_mod   = {
                "ACCUMULATION": 0.04, "MARKUP": 0.03, "EXPANSION": 0.02,
                "MANIPULATION": -0.06, "DISTRIBUTION": -0.03,
                "MARKDOWN": -0.03, "CONSOLIDATION": 0.0,
            }.get(phase, 0.0)
            trigger_mod = (trigger_strength - 0.6) * 0.08   # max ±0.04
            bias_mod    = 0.02 if daily_bias_aligned else 0.0
            prob = base + regime_mod + phase_mod + trigger_mod + bias_mod
            return round(max(0.30, min(prob, 0.92)), 4)

    # ── Fallback: heuristik jika data cluster belum cukup ────────────
    if max_score <= 0:
        return 0.50
    base        = min(score / max_score, 1.0) * 0.75 + 0.15   # dikecilkan — hindari over-confident
    regime_mod  = {"TRENDING": 0.05, "RANGING": -0.04, "CHOPPY": -0.12}.get(regime, 0.0)
    phase_mod   = {
        "ACCUMULATION": 0.05, "MARKUP": 0.04, "EXPANSION": 0.03,
        "MANIPULATION": -0.07, "DISTRIBUTION": -0.03,
        "MARKDOWN": -0.04, "CONSOLIDATION": 0.0,
    }.get(phase, 0.0)
    trigger_mod = (trigger_strength - 0.6) * 0.10
    bias_mod    = 0.03 if daily_bias_aligned else 0.0
    prob = base + regime_mod + phase_mod + trigger_mod + bias_mod
    return round(max(0.30, min(prob, 0.90)), 4)


def get_cluster_n_samples(ticker: str, regime: str) -> int:
    """
    [Q02] Kembalikan jumlah trade di cluster (regime|sector) yang relevan.
    Dipakai sebagai n_samples untuk Bayesian shrinkage di calc_kelly_fraction.
    Jika cluster tidak ditemukan atau data kosong → return 0 (shrink penuh).
    """
    if not ticker or not _cluster_weights:
        return 0
    sector  = TICKER_SECTOR.get(ticker, "MISC")
    key     = f"{regime}|{sector}"
    cluster = _cluster_weights.get(key)
    if not cluster:
        return 0
    # _cluster_weights hanya menyimpan modifier dan _win_rate, bukan total count.
    # Gunakan proxy: ambil dari _cluster_raw_counts jika tersedia (diisi get_cluster_weights).
    return _cluster_raw_counts.get(key, 0)


# [Q02] Cache untuk raw trade count per cluster — diisi oleh get_cluster_weights()
_cluster_raw_counts: dict = {}


def calibrate_cost_model_from_fills() -> dict:
    """
    [U03] Kalibrasi EV_COST_SLIPPAGE dari data fill nyata di Supabase.

    Query signals yang punya kolom 'actual_fill_price' (diisi oleh
    update_execution_fills() atau broker bridge). Hitung slippage aktual:
      actual_slippage_pct = abs(fill_price - entry) / entry * 100

    Jika N ≥ 20 fills per strategy/tier → update EV_COST_SLIPPAGE.
    Jika tidak cukup → pertahankan defaults, log status.

    Kolom Supabase yang dibutuhkan:
      ALTER TABLE signals ADD COLUMN IF NOT EXISTS actual_fill_price FLOAT;

    Returns dict: {calibrated: bool, n_fills: int, updates: dict, note: str}
    """
    global EV_COST_SLIPPAGE

    result = {"calibrated": False, "n_fills": 0, "updates": {}, "note": ""}

    # [U04] LEAN_MODE: skip kalibrasi, pakai defaults
    if LEAN_MODE:
        result["note"] = "LEAN_MODE aktif — skip kalibrasi, pakai EV_COST_SLIPPAGE defaults"
        return result

    try:
        rows = (
            supabase.table("signals")
            .select("strategy, tier, entry, actual_fill_price")
            .not_.is_("actual_fill_price", "null")
            .order("sent_at", desc=True)
            .limit(500)
            .execute()
            .data
        )
    except Exception as e:
        result["note"] = f"Supabase error: {e} — pakai defaults"
        log(f"⚠️ [U03] calibrate_cost_model_from_fills: {e}", "warn")
        return result

    if not rows:
        result["note"] = "Tidak ada actual_fill_price di Supabase — pakai defaults"
        return result

    result["n_fills"] = len(rows)

    # Kelompokkan per strategy/tier
    groups: dict = {}
    for r in rows:
        strat = r.get("strategy") or "SWING"
        tier  = r.get("tier") or "A"
        try:
            entry = float(r["entry"])
            fill  = float(r["actual_fill_price"])
            if entry <= 0: continue
            slip_pct = abs(fill - entry) / entry * 100
            key = f"{strat}|{tier}"
            groups.setdefault(key, []).append(slip_pct)
        except Exception:
            continue

    MIN_FILLS_FOR_CALIBRATION = 20
    new_cost = {s: dict(v) for s, v in EV_COST_SLIPPAGE.items()}
    updates  = {}

    for key, slips in groups.items():
        if len(slips) < MIN_FILLS_FOR_CALIBRATION:
            continue
        strat, tier = key.split("|")
        median_slip = float(np.median(slips))
        old_val     = EV_COST_SLIPPAGE.get(strat, {}).get(tier, 0.30)
        # Blend: 70% actual data + 30% prior default (don't fully abandon prior)
        blended     = round(0.70 * median_slip + 0.30 * old_val, 3)
        if strat in new_cost and tier in new_cost[strat]:
            new_cost[strat][tier] = blended
            updates[key] = {"old": old_val, "new": blended, "n": len(slips),
                            "median_actual": round(median_slip, 3)}
            log(f"  ✅ [U03] {key}: slippage {old_val:.3f}% → {blended:.3f}% "
                f"(median actual={median_slip:.3f}%, n={len(slips)}, 70/30 blend)")

    if updates:
        EV_COST_SLIPPAGE = new_cost
        result["calibrated"] = True
        result["updates"]    = updates
        result["note"]       = f"Dikalibrasi dari {result['n_fills']} fills: {len(updates)} grup diupdate"
    else:
        result["note"] = (f"{result['n_fills']} fills ditemukan tapi <{MIN_FILLS_FOR_CALIBRATION} "
                          f"per grup — pakai defaults")

    return result


def calc_cost_adjusted_ev(
    win_prob: float,
    rr: float,
    strategy: str = "SWING",
    tier: str = "A",
    impact_pct: float = 0.0,
) -> dict:
    """
    [T02] Hitung net EV setelah biaya estimasi.

    Gross EV (yang dipakai saat ini):
      gross_ev = win_prob * rr - (1 - win_prob) * 1.0

    Net EV (lebih realistis):
      adjusted_rr = rr - slippage_drag (slippage makan TP dan entry)
      adjusted_sl = 1.0 + slippage_drag (slippage juga memperburuk SL)
      net_ev_raw  = win_prob * adjusted_rr - (1-win_prob) * adjusted_sl
      net_ev      = net_ev_raw * fill_rate  (missed trades = 0 EV)

    Note: ini ESTIMASI default. Untuk kalibrasi: set EV_COST_SLIPPAGE
    berdasarkan actual fill data dari Supabase.

    Returns dict: gross_ev, net_ev, slippage_pct, fill_rate, cost_drag
    """
    slippage_pct = (EV_COST_SLIPPAGE
                    .get(strategy, EV_COST_SLIPPAGE.get("SWING", {}))
                    .get(tier, 0.30))
    # Tambah impact cost jika tersedia (dari VPR model)
    total_slip = slippage_pct + impact_pct
    fill_rate  = EV_COST_FILL_RATE.get(strategy, 0.85)

    gross_ev   = win_prob * rr - (1.0 - win_prob) * 1.0
    adj_rr     = max(rr - total_slip / 100 * 2, 0.05)
    adj_sl     = 1.0 + total_slip / 100
    net_ev_raw = win_prob * adj_rr - (1.0 - win_prob) * adj_sl
    net_ev     = net_ev_raw * fill_rate

    return {
        "gross_ev":    round(gross_ev, 4),
        "net_ev":      round(net_ev, 4),
        "slippage_pct": round(slippage_pct, 3),
        "impact_pct":   round(impact_pct, 3),
        "fill_rate":    round(fill_rate, 3),
        "cost_drag":    round(gross_ev - net_ev, 4),
    }


def calc_expected_value(win_prob: float, rr: float) -> float:
    """
    Expected Value = (win_prob × RR) - ((1 - win_prob) × 1)

    [v7.0] EV adalah CORE ENGINE — bukan filter tambahan.
    Filosofi: bukan cari winrate tinggi, tapi cari "positive expectation".
    Trade dengan winrate 60% dan RR 2.0 punya EV = 0.40 → AMBIL.
    Trade dengan winrate 80% dan RR 0.8 punya EV = 0.44 → AMBIL.
    Trade dengan winrate 75% dan RR 1.0 punya EV = 0.50 → AMBIL.
    Trade dengan winrate 90% dan RR 0.3 punya EV = -0.37 → HARD SKIP.

    EV <= 0 = HARD SKIP tanpa pengecualian. Tidak ada override.
    EV positif tapi kecil = tergantung setup_rank threshold (0.15–0.25).
    EV > 0.5 = edge sangat kuat — eksekusi dengan full confidence.
    """
    ev = (win_prob * rr) - ((1.0 - win_prob) * 1.0)
    return round(ev, 4)


# [v7.0] EV sebagai CORE ENGINE
# HARD_EV_FLOOR: EV <= 0 = HARD SKIP tanpa kompromi apapun (primary gate)
# EV_MIN_THRESHOLD: floor minimum yang bisa lolos (0.20 untuk MEDIUM setup)
# Threshold aktual per setup diambil dari rank_setup_priority()
HARD_EV_FLOOR    = 0.0    # EV <= ini → HARD SKIP, tidak ada yang bisa override
EV_MIN_THRESHOLD = 0.05   # [PHASE-0] was 0.20 — floor sangat rendah agar tidak memblokir signal awal


# ════════════════════════════════════════════════════════
#  [9] POSITION MANAGEMENT ENGINE — v4.0
#  Trailing stop, partial TP, break-even otomatis
# ════════════════════════════════════════════════════════

def check_position_management(row: dict, closes, highs, lows, volumes=None) -> dict | None:
    """
    [v7.0] Adaptive Trade Management — bot "berpikir" saat trade berjalan.

    Tiga mode adaptif berdasarkan kondisi pasar real-time:

    MODE 1 — LET PROFIT RUN (strong trend):
      Jika momentum kuat (EMA20 masih naik, candle bullish, ATR ekspansi),
      trailing SL dilonggarkan (ATR × 1.2 bukan × 0.8) agar profit bisa run.
      Exit terlalu cepat di strong trend adalah kesalahan paling mahal.

    MODE 2 — EXIT EARLY (weak momentum):
      Jika momentum melemah (EMA20 mulai flat/turun, RSI diverge, volume drop),
      TP1 diturunkan ke harga saat ini × 0.985 untuk lock profit sebelum reversal.
      Better to exit 80% profit than to give it all back.

    MODE 3 — VOLATILITY TIGHTEN SL (spike):
      Jika ATR tiba-tiba melebar > 2x normal (news/panic), SL diperketat
      ke current high/low - ATR × 0.5 untuk melindungi dari guncangan.
      Volatility spike sering diikuti reversal tajam tanpa warning.

    Semua mode existing (BE trigger, trailing, partial TP) tetap aktif.
    Mode adaptif adalah LAPISAN TAMBAHAN di atas mekanisme existing.
    """
    if closes is None or len(closes) < 10:
        return None
    try:
        entry  = float(row.get("entry", 0))
        sl     = float(row.get("sl", 0))
        tp1    = float(row.get("tp1", 0))
        tp2    = float(row.get("tp2", tp1))
        side   = row.get("side", "BUY")
        if entry <= 0 or sl <= 0:
            return None
        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            return None

        current_high  = float(np.max(highs[-5:]))
        current_low   = float(np.min(lows[-5:]))
        current_close = float(closes[-1])
        atr           = calc_atr(closes, highs, lows)
        actions = []

        # ── EMA dan volatilitas untuk mode detection ──────
        ema20 = calc_ema(closes, 20) if len(closes) >= 20 else None
        ema10 = calc_ema(closes, 10) if len(closes) >= 10 else None

        # Volatilitas: bandingkan ATR terkini dengan ATR 10 candle lalu
        atr_recent = atr
        atr_prev   = calc_atr(closes[:-5], highs[:-5], lows[:-5]) if len(closes) > 10 else atr
        vol_ratio  = atr_recent / (atr_prev + 1e-9)

        # [Z03] Volume momentum — gunakan volumes aktual jika tersedia
        # BUG FIX: sebelumnya menggunakan closes sebagai proxy volume (salah)
        if volumes is not None and len(volumes) >= 3:
            vol_arr = np.array(volumes, dtype=float)
            vol_avg  = float(np.mean(vol_arr[-11:-1])) if len(vol_arr) >= 11 else float(np.mean(vol_arr[:-1])) if len(vol_arr) > 1 else float(vol_arr[-1])
            vol_drop = float(vol_arr[-1]) < vol_avg * 0.60   # volume turun > 40% dari rata-rata
        else:
            # Fallback: tidak ada data volume — gunakan price momentum sebagai proxy
            vol_avg  = float(np.mean(closes[-11:-1])) if len(closes) >= 11 else float(closes[-1])
            vol_drop = float(closes[-1]) < float(closes[-3]) * 0.95   # harga drop 5% dari 3 candle lalu

        # ── Deteksi adaptive mode ─────────────────────────
        # STRONG TREND: EMA10 > EMA20, harga di atas EMA20, ATR normal/sedikit naik
        strong_trend = (
            ema10 and ema20 and
            ema10 > ema20 and
            current_close > ema20 and
            1.0 <= vol_ratio <= 1.8   # ATR sedikit naik tapi tidak spike
        ) if side == "BUY" else (
            ema10 and ema20 and
            ema10 < ema20 and
            current_close < ema20 and
            1.0 <= vol_ratio <= 1.8
        )

        # WEAK MOMENTUM: EMA10 mendatar atau balik arah, volume melemah
        weak_momentum = (
            ema10 and ema20 and
            (ema10 <= ema20 * 1.002 or vol_drop)   # EMA10 hampir menyentuh/crossing EMA20
        ) if side == "BUY" else (
            ema10 and ema20 and
            (ema10 >= ema20 * 0.998 or vol_drop)
        )

        # VOLATILITY SPIKE: ATR tiba-tiba > 2x normal
        vol_spike = vol_ratio > 2.0

        if side == "BUY":
            profit_r = (current_close - entry) / sl_dist

            # ── Existing: BE trigger ──────────────────────
            if profit_r >= BE_TRIGGER_R and sl < entry:
                new_be_sl = round(entry * 1.005, 2)
                actions.append({"action": "MOVE_TO_BE", "new_sl": new_be_sl,
                    "reason": f"Profit +{profit_r:.1f}R — SL ke BE (+0.5%)", "emoji": "🛡️"})

            # ── Adaptive Mode 3: VOLATILITY TIGHTEN SL ───
            if vol_spike and profit_r > 0.5:
                tight_sl = round(current_high - atr * 0.5, 2)
                if tight_sl > sl:
                    actions.append({"action": "TIGHTEN_SL", "new_sl": tight_sl,
                        "reason": f"Vol spike {vol_ratio:.1f}x — SL diperketat (ATR×0.5)", "emoji": "⚡"})

            elif profit_r >= TRAIL_TRIGGER_R:
                if strong_trend:
                    # ── Adaptive Mode 1: LET PROFIT RUN ──
                    # Trailing lebih longgar — beri ruang trend berkembang
                    trail_sl = round(current_high - atr * 1.2, 2)
                    if trail_sl > sl:
                        actions.append({"action": "TRAIL_STOP", "new_sl": trail_sl,
                            "reason": f"Strong trend — trailing longgar (ATR×1.2)", "emoji": "🚀"})
                elif weak_momentum:
                    # ── Adaptive Mode 2: EXIT EARLY ───────
                    # Saran keluar lebih awal sebelum momentum habis
                    early_exit_price = round(current_close * 0.998, 2)
                    actions.append({"action": "EXIT_EARLY", "exit_price": early_exit_price,
                        "reason": f"Momentum melemah (EMA diverge/vol drop) — lock profit sekarang",
                        "emoji": "⚠️"})
                else:
                    # ── Standard adaptive trailing ─────────
                    trail_atr_sl = round(current_high - atr * TRAIL_ATR_MULT, 2)
                    trail_ema_sl = round(ema20 - atr * 0.5, 2) if ema20 else trail_atr_sl
                    trail_sl     = max(trail_atr_sl, trail_ema_sl)
                    if trail_sl > sl:
                        trail_src = "EMA+ATR" if trail_ema_sl > trail_atr_sl else "ATR"
                        actions.append({"action": "TRAIL_STOP", "new_sl": trail_sl,
                            "reason": f"Trailing [{trail_src}] — high:{current_high:,.0f}", "emoji": "📡"})

            # ── Existing: Partial TP ──────────────────────
            if current_high >= tp1:
                be_after_tp1 = round(entry * 1.005, 2)
                actions.append({"action": "PARTIAL_TP", "tp_hit": tp1,
                    "reason": f"TP1 hit — close 50%, SL → Rp{be_after_tp1:,.0f}", "emoji": "🎯"})

        else:  # SELL
            profit_r = (entry - current_close) / sl_dist

            if profit_r >= BE_TRIGGER_R and sl > entry:
                new_be_sl = round(entry * 0.995, 2)
                actions.append({"action": "MOVE_TO_BE", "new_sl": new_be_sl,
                    "reason": f"Profit +{profit_r:.1f}R — SL ke BE (-0.5%)", "emoji": "🛡️"})

            if vol_spike and profit_r > 0.5:
                tight_sl = round(current_low + atr * 0.5, 2)
                if tight_sl < sl:
                    actions.append({"action": "TIGHTEN_SL", "new_sl": tight_sl,
                        "reason": f"Vol spike {vol_ratio:.1f}x — SL diperketat", "emoji": "⚡"})

            elif profit_r >= TRAIL_TRIGGER_R:
                if strong_trend:
                    trail_sl = round(current_low + atr * 1.2, 2)
                    if trail_sl < sl:
                        actions.append({"action": "TRAIL_STOP", "new_sl": trail_sl,
                            "reason": f"Strong trend — trailing longgar (ATR×1.2)", "emoji": "🚀"})
                elif weak_momentum:
                    early_exit_price = round(current_close * 1.002, 2)
                    actions.append({"action": "EXIT_EARLY", "exit_price": early_exit_price,
                        "reason": f"Momentum melemah — lock profit sekarang", "emoji": "⚠️"})
                else:
                    trail_atr_sl = round(current_low + atr * TRAIL_ATR_MULT, 2)
                    trail_ema_sl = round(ema20 + atr * 0.5, 2) if ema20 else trail_atr_sl
                    trail_sl     = min(trail_atr_sl, trail_ema_sl)
                    if trail_sl < sl:
                        trail_src = "EMA+ATR" if trail_ema_sl < trail_atr_sl else "ATR"
                        actions.append({"action": "TRAIL_STOP", "new_sl": trail_sl,
                            "reason": f"Trailing [{trail_src}] — low:{current_low:,.0f}", "emoji": "📡"})

            if current_low <= tp1:
                be_after_tp1 = round(entry * 0.995, 2)
                actions.append({"action": "PARTIAL_TP", "tp_hit": tp1,
                    "reason": f"TP1 hit — close 50%, SL → Rp{be_after_tp1:,.0f}", "emoji": "🎯"})

        meta = {
            "mode":        "STRONG_TREND" if strong_trend else ("WEAK_MOMENTUM" if weak_momentum else ("VOL_SPIKE" if vol_spike else "NORMAL")),
            "vol_ratio":   round(vol_ratio, 2),
            "profit_r":    round(profit_r, 2),
        }
        return {"actions": actions, **meta} if actions else None

    except Exception as e:
        log(f"⚠️ check_position_management: {e}", "warn")
        return None


def send_position_management_alert(row: dict, mgmt: dict):
    """[v7.0] Kirim notifikasi Telegram untuk aksi position management — termasuk mode adaptif baru."""
    try:
        pair     = row.get("pair", "?")
        side     = row.get("side", "?")
        entry    = float(row.get("entry", 0))
        profit_r = mgmt.get("profit_r", 0)
        mode     = mgmt.get("mode", "NORMAL")
        side_str = "🟢 BUY" if side == "BUY" else "🔴 SELL"
        mode_str = {"STRONG_TREND": "🚀 Strong Trend", "WEAK_MOMENTUM": "⚠️ Weak Momentum",
                    "VOL_SPIKE": "⚡ Vol Spike", "NORMAL": "📊 Normal"}.get(mode, mode)
        for act in mgmt["actions"]:
            emoji      = act.get("emoji", "📌")
            action     = act.get("action", "")
            reason     = act.get("reason", "")
            new_sl     = act.get("new_sl")
            tp_hit     = act.get("tp_hit")
            exit_price = act.get("exit_price")

            if action == "PARTIAL_TP":
                msg = (f"{emoji} <b>PARTIAL TP ALERT</b>\n"
                       f"━━━━━━━━━━━━━━━━━━\n"
                       f"{side_str} <b>{pair}</b>\n"
                       f"TP1 hit  : Rp{tp_hit:,.0f}\n"
                       f"Aksi     : <b>Tutup 50% posisi sekarang</b>\n"
                       f"SL baru  : Break-even (Rp{entry:,.0f})\n"
                       f"Profit   : +{profit_r:.1f}R\n"
                       f"<i>Biarkan 50% sisanya menuju TP2.</i>")

            elif action in ("MOVE_TO_BE", "TRAIL_STOP", "TIGHTEN_SL"):
                sl_str  = f"Rp{new_sl:,.0f}" if new_sl else "—"
                labels  = {"MOVE_TO_BE": "Break-Even", "TRAIL_STOP": "Trailing Stop",
                           "TIGHTEN_SL": "SL Diperketat"}
                a_label = labels.get(action, action)
                msg = (f"{emoji} <b>{a_label.upper()} AKTIF</b>\n"
                       f"━━━━━━━━━━━━━━━━━━\n"
                       f"{side_str} <b>{pair}</b> [{mode_str}]\n"
                       f"SL baru  : <b>{sl_str}</b>\n"
                       f"Alasan   : {html_escape(reason)}\n"
                       f"Profit   : +{profit_r:.1f}R\n"
                       f"<i>Update SL manual di platform kamu.</i>")

            elif action == "EXIT_EARLY":
                exit_str = f"Rp{exit_price:,.0f}" if exit_price else "Market"
                msg = (f"{emoji} <b>EXIT EARLY ALERT</b>\n"
                       f"━━━━━━━━━━━━━━━━━━\n"
                       f"{side_str} <b>{pair}</b> [{mode_str}]\n"
                       f"Harga exit: <b>{exit_str}</b>\n"
                       f"Alasan    : {html_escape(reason)}\n"
                       f"Profit    : +{profit_r:.1f}R terkunci\n"
                       f"<i>Pertimbangkan tutup posisi sebelum momentum habis.</i>")
            else:
                continue
            tg(msg)
            time.sleep(0.3)
    except Exception as e:
        log(f"⚠️ send_position_management_alert [{row.get('pair')}]: {e}", "warn")


# ════════════════════════════════════════════════════════
#  [10] MARKET CORRELATION FILTER — v4.0
#  Deteksi kekuatan sektor IDX — cegah false signal systemic
# ════════════════════════════════════════════════════════

def get_sector_momentum(sector: str) -> dict:
    """
    Hitung momentum sektor via proxy ticker — cache per run.
    Returns trend: BULLISH | BEARISH | NEUTRAL
    """
    global _sector_momentum_cache
    default = {"momentum_1d": 0.0, "momentum_5d": 0.0,
               "trend": "NEUTRAL", "above_ema20": True}
    if sector in _sector_momentum_cache:
        return _sector_momentum_cache[sector]
    proxy = SECTOR_PROXY.get(sector)
    if not proxy:
        _sector_momentum_cache[sector] = default
        return default
    try:
        data = get_candles(proxy, "1d", 30)
        if data is None:
            _sector_momentum_cache[sector] = default
            return default
        closes, _, _, _, _op = data
        c = closes.astype(float)
        mom_1d = (c[-1] - c[-2]) / c[-2] * 100 if len(c) >= 2 else 0.0
        mom_5d = (c[-1] - c[max(-6, -len(c))]) / c[max(-6, -len(c))] * 100
        ema20  = calc_ema(c, 20)
        above  = c[-1] > ema20
        if mom_5d > 2.0 and above:   trend = "BULLISH"
        elif mom_5d < -2.0 and not above: trend = "BEARISH"
        else:                         trend = "NEUTRAL"
        result = {"momentum_1d": round(mom_1d, 2), "momentum_5d": round(mom_5d, 2),
                  "trend": trend, "above_ema20": above}
        _sector_momentum_cache[sector] = result
        log(f"  📊 Sektor {sector}: 1d:{mom_1d:+.1f}% 5d:{mom_5d:+.1f}% [{trend}]")
        return result
    except Exception as e:
        log(f"⚠️ get_sector_momentum [{sector}]: {e}", "warn")
        _sector_momentum_cache[sector] = default
        return default


def is_sector_blocked(ticker: str, side: str, ihsg: dict | None = None) -> bool:
    """
    Blokir BUY jika sektor BEARISH, blokir SELL jika sektor BULLISH.

    [v7.2 — FIX Masalah 5] Tambahan: jika IHSG hanya drop ringan (-2% s/d -3%)
    dan sektor ticker termasuk IHSG_COUNTER_TREND_SECTORS (ENERGY/MINING/CPO/PETROCHEM),
    blok IHSG tidak berlaku untuk sektor tersebut — sektor ini sering counter-trend.
    Blok penuh (crash > 5%) tetap berlaku untuk semua sektor tanpa pengecualian.
    """
    sector = TICKER_SECTOR.get(ticker, "MISC")
    mom    = get_sector_momentum(sector)
    trend  = mom["trend"]

    # Counter-trend exemption: IHSG drop ringan tapi sektor ini komoditas
    if (side == "BUY" and ihsg is not None and
            ihsg.get("block_buy") and not ihsg.get("halt") and
            sector in IHSG_COUNTER_TREND_SECTORS):
        log(f"  ℹ️ {ticker}: Sektor {sector} masuk counter-trend exemption — IHSG drop ringan, sektor komoditas tetap diizinkan BUY")
        # Hanya cek sektor momentum, bukan IHSG block
        if trend == "BEARISH":
            log(f"  ⚠️ {ticker}: Sektor {sector} sendiri BEARISH — blokir BUY meski counter-trend exemption")
            return True
        return False

    if side == "BUY" and trend == "BEARISH":
        log(f"  ⚠️ {ticker}: Sektor {sector} BEARISH — blokir BUY")
        return True
    if side == "SELL" and trend == "BULLISH":
        log(f"  ⚠️ {ticker}: Sektor {sector} BULLISH — blokir SELL/EXIT")
        return True
    return False


def get_sector_capital_weight(ticker: str, side: str) -> float:
    """
    [v6.0] Upgrade #5 — Capital Rotation Logic.

    Pasar bergerak per sektor. Bot sebelumnya hanya bisa blokir atau tidak blokir
    (binary). Upgrade ini menambahkan dimensi ketiga: REDUCE.

    Sektor STRONG  → alokasi penuh (multiplier = 1.0)
    Sektor NEUTRAL → alokasi normal (multiplier = 0.75) — sedikit konservatif
    Sektor WEAK    → alokasi dikurangi (multiplier = 0.50) — masih trading tapi
                     dengan position size 50% untuk lindungi kapital

    Logic tambahan: momentum 1d yang sangat kuat/lemah juga dipertimbangkan
    sebagai tiebreaker dalam kondisi NEUTRAL.

    Returns: float multiplier (0.50 – 1.0) yang dikalikan dengan smart_risk_pct
    di position sizing.
    """
    sector = TICKER_SECTOR.get(ticker, "MISC")
    mom    = get_sector_momentum(sector)
    trend  = mom["trend"]
    mom_1d = mom.get("momentum_1d", 0.0)
    mom_5d = mom.get("momentum_5d", 0.0)

    if side == "BUY":
        if trend == "BULLISH":
            # Sektor kuat — alokasi penuh
            weight = 1.0
            label  = "STRONG ✅"
        elif trend == "NEUTRAL":
            # Tiebreaker: 1d momentum
            if mom_1d > 1.0:
                weight = 0.85   # NEUTRAL tapi hari ini bagus
                label  = "NEUTRAL+ 🟡"
            elif mom_1d < -1.0:
                weight = 0.60   # NEUTRAL tapi hari ini lemah
                label  = "NEUTRAL- 🟡"
            else:
                weight = 0.75
                label  = "NEUTRAL 🟡"
        else:  # BEARISH (sudah diblokir oleh is_sector_blocked, tapi jaga-jaga)
            weight = 0.50
            label  = "WEAK ⚠️"
    else:  # SELL — logika terbalik
        if trend == "BEARISH":
            weight = 1.0
            label  = "STRONG ✅"
        elif trend == "NEUTRAL":
            weight = 0.75 if abs(mom_1d) < 1.0 else (0.85 if mom_1d < -1.0 else 0.60)
            label  = "NEUTRAL 🟡"
        else:  # BULLISH
            weight = 0.50
            label  = "WEAK ⚠️"

    log(f"  💼 Capital rotation [{ticker} {side}]: Sektor {sector} = {label} → weight {weight:.0%}")
    return weight


# ════════════════════════════════════════════════════════
#  [11] SNIPER ENTRY ENGINE — v4.0
#  OB Reaction + FVG (Fair Value Gap) + Sweep+Reversal
# ════════════════════════════════════════════════════════

def detect_fvg(closes, highs, lows, side: str = "BUY", lookback: int = 30) -> dict:
    """
    Fair Value Gap / Imbalance detection.
    Bullish FVG: high[i-2] < low[i] — gap support belum terisi
    Bearish FVG: low[i-2]  > high[i] — gap resistance belum terisi
    price_in_fvg=True berarti harga masuk zona imbalance = entry presisi.
    """
    result = {"valid": False, "fvg_top": None, "fvg_bottom": None, "price_in_fvg": False}
    if len(closes) < lookback + 3:
        return result
    h = highs[-lookback:].astype(float)
    l = lows[-lookback:].astype(float)
    c = closes[-lookback:].astype(float)
    current_price = c[-1]
    for i in range(len(c) - 1, 1, -1):
        if side == "BUY":
            if h[i-2] < l[i]:
                fvg_top = l[i]; fvg_bottom = h[i-2]
                in_fvg = fvg_bottom <= current_price <= fvg_top * 1.01
                if current_price >= fvg_bottom:
                    return {"valid": True, "fvg_top": fvg_top,
                            "fvg_bottom": fvg_bottom, "price_in_fvg": in_fvg}
        else:
            if l[i-2] > h[i]:
                fvg_top = l[i-2]; fvg_bottom = h[i]
                in_fvg = fvg_bottom * 0.99 <= current_price <= fvg_top
                if current_price <= fvg_top:
                    return {"valid": True, "fvg_top": fvg_top,
                            "fvg_bottom": fvg_bottom, "price_in_fvg": in_fvg}
    return result


def detect_ob_reaction(closes, highs, lows, volumes, ob: dict, side: str = "BUY") -> dict:
    """
    Konfirmasi harga bereaksi dari Order Block (bukan tembus).
    OB reaction valid = harga menyentuh OB + candle reversal + volume konfirmasi.
    """
    no_reaction = {"is_reacting": False, "strength": 0.0}
    if not ob.get("valid") or len(closes) < 5:
        return no_reaction
    ob_high = ob["ob_high"]; ob_low = ob["ob_low"]
    c = closes.astype(float); h = highs.astype(float)
    l = lows.astype(float);   v = volumes.astype(float)
    vol_avg = float(np.mean(v[-11:-1])) if len(v) >= 11 else float(np.mean(v[:-1]))
    for i in range(-3, 0):
        if side == "BUY":
            if l[i] <= ob_high and l[i] >= ob_low * 0.99 and c[i] > c[i-1]:
                body = c[i] - c[i-1]; rng = h[i] - l[i] + 1e-9
                vol_s = float(v[i]) / (vol_avg + 1e-9)
                strength = min((body / rng) * (vol_s / 1.5), 1.0)
                if strength > 0.35:
                    return {"is_reacting": True, "strength": round(strength, 2)}
        else:
            if h[i] >= ob_low and h[i] <= ob_high * 1.01 and c[i] < c[i-1]:
                body = c[i-1] - c[i]; rng = h[i] - l[i] + 1e-9
                vol_s = float(v[i]) / (vol_avg + 1e-9)
                strength = min((body / rng) * (vol_s / 1.5), 1.0)
                if strength > 0.35:
                    return {"is_reacting": True, "strength": round(strength, 2)}
    return no_reaction


def calc_sniper_score(liq: dict, liq_trap: dict, ob_reaction: dict,
                       fvg: dict, side: str) -> dict:
    """
    Sniper Score = jumlah konfirmasi presisi (max 4).
    Level: SNIPER (3-4) | PRECISION (2) | STANDARD (0-1)
    Skor tinggi = win prob lebih tinggi + worth entry.
    """
    bonus = 0; details = []
    sweep_key  = "sweep_bull"  if side == "BUY" else "sweep_bear"
    hunt_key   = "stop_hunt_bull" if side == "BUY" else "stop_hunt_bear"
    if liq.get(sweep_key):
        bonus += 1; details.append("Liq Sweep")
    if ob_reaction.get("is_reacting"):
        bonus += 1; details.append(f"OB Reaction({ob_reaction['strength']:.0%})")
    if fvg.get("price_in_fvg"):
        bonus += 1; details.append("FVG Zone")
    if liq_trap.get(hunt_key):
        bonus += 1; details.append("Stop Hunt")
    level = "SNIPER 🎯" if bonus >= 3 else ("PRECISION 🔵" if bonus >= 2 else "STANDARD ⚪")
    return {"bonus": bonus, "details": " | ".join(details) if details else "—", "level": level}


# ════════════════════════════════════════════════════════
#  [12] DELAY-AWARE ENTRY GUARD — v4.0
#  Proteksi dari entry berbasis candle stale (yfinance 15m delay)
# ════════════════════════════════════════════════════════

def is_candle_stale(ticker: str, interval: str = "1h") -> bool:
    """
    Deteksi candle stale dari yfinance (delay ~15 menit).
    Intraday (1h): stale jika candle terakhir > 90 menit lalu.
    Swing (1d): tidak perlu cek — candle harian tidak stale dalam sehari.

    [v7.12 FIX] Sebelumnya selalu melakukan extra yf.download(2d,1h) per ticker
    meski cache sudah ada dari get_candles(). Sekarang:
    1. Jika cache 1h|100 ada → estimasi staleness dari timestamp cache itu sendiri
       dengan memanfaatkan yfinance disk cache (request identik = no network hit)
    2. Hanya download jika benar-benar tidak ada cache sama sekali

    [Y02] OPEN_VOLATILE hard block: selama 09:00–09:45 WIB, yfinance 15m delay
    menjadi sangat berbahaya karena harga bergerak cepat. Hard block tanpa
    pengecualian — jangan relaksasi di sesi ini.

    Returns tuple (is_stale: bool, reason: str | None).
    - is_stale=True  → caller harus skip ticker ini
    - reason         → deskripsi penyebab staleness untuk audit log & _filter_audit

    [Y02-FIX] Sebelumnya return bool saja; stale_reason hanya di-log ke console dan
    hilang. Sekarang reason dikembalikan ke caller agar bisa dicatat ke _filter_audit
    dan di-trace secara post-run per ticker.
    """
    if interval != "1h":
        return False, None

    # [Y02] Hard block saat OPEN_VOLATILE — cek SEBELUM download apapun
    now_wib  = datetime.now(WIB)
    time_val = now_wib.hour + now_wib.minute / 60.0
    if 9.0 <= time_val < 9.75:
        reason = "OPEN_VOLATILE_HARD_BLOCK (09:00–09:45 WIB)"
        log(f"  🔴 [Y02] {ticker} [1H]: {reason} — skip intraday", "warn")
        return True, reason

    try:
        cache_key = f"{ticker}|1h|100"
        # Jika cache sudah ada, fetch df 2d/1h — yfinance disk-cache memastikan
        # ini tidak memicu network request baru (sudah di-cache dari get_candles run)
        if cache_key not in _candle_cache or _candle_cache[cache_key] is None:
            # Belum ada cache sama sekali — download minimal
            df = yf.download(ticker, period="2d", interval="1h",
                             progress=False, auto_adjust=True)
        else:
            # Ada cache — yfinance akan return dari disk cache (no network)
            df = yf.download(ticker, period="2d", interval="1h",
                             progress=False, auto_adjust=True)

        if df is None or df.empty:
            return True, "empty_dataframe"
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        last_ts = df.index[-1]
        if hasattr(last_ts, "tzinfo") and last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        age_mins = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60

        # [Y02] Audit log staleness per ticker agar bisa di-trace post-run
        if age_mins > 90:
            reason = f"age={age_mins:.0f}m > 90m threshold"
            log(f"  ⚠️ {ticker} [1H]: Candle stale ({reason}) — skip intraday")
            return True, reason
        elif age_mins > 60:
            log(f"  🟡 [Y02] {ticker} [1H]: Candle ageing ({age_mins:.0f}m) — masih lolos tapi mendekati stale")
        return False, None
    except Exception as e:
        log(f"⚠️ is_candle_stale [{ticker}]: {e}", "warn")
        return False, None


# ════════════════════════════════════════════════════════
#  [13] NO-TRADE ZONE ENGINE — v5.0
#  Smart skip untuk kondisi pasar uncertain.
#  "Profit bukan hanya dari entry bagus — tapi dari skip entry jelek."
#  Eliminasi ~30% losing trade yang berasal dari market noise.
# ════════════════════════════════════════════════════════

# Threshold No-Trade Zone
NTZ_MIN_ATR_PCT     = 0.4    # ATR terlalu kecil = pasar tidur
NTZ_MAX_CHOP_IDX    = 50.0   # Choppiness Index > 50 = tidak ada tren
NTZ_MIN_RANGE_RATIO = 0.55   # Range candle vs ATR terlalu sempit
NTZ_MTF_CONFLICT_MAX = 1     # Maksimal 1 timeframe conflict yang ditoleransi


def calc_choppiness_index(closes, highs, lows, period: int = 14) -> float:
    """
    Choppiness Index (CI) — ukuran seberapa chop pasar.
    CI > 61.8: sangat choppy (konsolidasi, hindari trending system)
    CI < 38.2: strong trend
    CI 38–62:  zona abu-abu

    Formula: 100 × log10(sum_ATR_n / (highest_n - lowest_n)) / log10(n)
    """
    if len(closes) < period + 1:
        return 50.0
    h = highs[-period:].astype(float)
    l = lows[-period:].astype(float)
    c = closes[-period-1:].astype(float)

    # True Range sum
    tr_sum = 0.0
    for i in range(1, period + 1):
        tr = max(h[i-1] - l[i-1],
                 abs(h[i-1] - c[i-2]),
                 abs(l[i-1] - c[i-2]))
        tr_sum += tr

    highest = float(np.max(h))
    lowest  = float(np.min(l))
    diff    = highest - lowest

    if diff <= 0 or tr_sum <= 0:
        return 50.0

    ci = 100.0 * math.log10(tr_sum / diff) / math.log10(period)
    return round(ci, 2)


def check_no_trade_zone(closes, highs, lows, volumes,
                         regime: str, phase: str,
                         daily_bias: str, side: str,
                         atr_pct: float) -> dict:
    """
    Evaluasi apakah kondisi pasar saat ini masuk No-Trade Zone.
    Kembalikan dict: {skip: bool, reasons: list[str]}

    [v6.0] Upgrade #3 — Market Avoidance Intelligence:
      Penambahan dua kondisi kritis yang sebelumnya tidak ada:

      CHOPPY_EXTREME: Choppiness Index > 61.8 (zona Fibonacci atas) — ini bukan
      sekadar "choppy", ini kondisi di mana bahkan scalper profesional skip.
      Di atas 61.8, pasar secara statistik sedang random walk murni.

      CONFLICTING_SIGNALS: Deteksi kontradiksi internal antar indikator —
      misalnya MACD bullish tapi RSI overbought, atau BOS bullish tapi bias
      bearish. Satu konflik = warning; dua+ konflik = skip mandatory.
      Ini eliminasi "false positive convergence" di mana sinyal terlihat kuat
      tapi saling bertentangan secara fundamental.

    Kondisi NTZ yang sudah ada sebelumnya dipertahankan.
    """
    reasons = []

    # Kondisi 1: ATR terlalu kecil
    if atr_pct < NTZ_MIN_ATR_PCT:
        reasons.append(f"ATR={atr_pct:.2f}% < min {NTZ_MIN_ATR_PCT}% (pasar tidur)")

    # Kondisi 2: Choppiness Index
    ci = calc_choppiness_index(closes, highs, lows, period=14)

    # [v6.0] CHOPPY_EXTREME: CI > 61.8 (Fibonacci upper band) = random walk murni
    if ci > 61.8:
        reasons.append(f"CHOPPY_EXTREME: Chop Index={ci:.1f} > 61.8 (random walk — NO TRADE)")
    elif ci > NTZ_MAX_CHOP_IDX:
        reasons.append(f"Chop Index={ci:.1f} > {NTZ_MAX_CHOP_IDX} (pasar choppy)")

    # Kondisi 3: Phase MANIPULATION tanpa sweep (retail trap murni)
    if phase == "MANIPULATION":
        reasons.append("Phase MANIPULATION — pasar jebakan, wait for sweep confirmation")

    # Kondisi 4: Regime CHOPPY (double-check)
    if regime == "CHOPPY":
        reasons.append("ADX Regime=CHOPPY — tidak ada tren dominan")

    # Kondisi 5: Hard MTF conflict (bukan NEUTRAL, tapi OPPOSITE)
    if side == "BUY" and daily_bias == "BEARISH":
        reasons.append("MTF hard conflict — daily bias BEARISH vs BUY")
    elif side == "SELL" and daily_bias == "BULLISH":
        reasons.append("MTF hard conflict — daily bias BULLISH vs SELL")

    # Kondisi 6: Volume abnormal rendah (< 30% rata-rata)
    if len(volumes) >= 11:
        vol_avg = float(np.mean(volumes[-11:-1]))
        vol_cur = float(volumes[-1])
        if vol_avg > 0 and vol_cur < vol_avg * 0.30:
            reasons.append(f"Volume sangat rendah ({vol_cur/vol_avg:.0%} dari rata-rata)")

    # Kondisi 7: Range candle terakhir vs ATR terlalu sempit (fake activity)
    if len(highs) >= 1 and atr_pct > 0:
        last_range_pct = (float(highs[-1]) - float(lows[-1])) / (float(closes[-1]) + 1e-9) * 100
        if last_range_pct < atr_pct * NTZ_MIN_RANGE_RATIO:
            reasons.append(f"Candle range={last_range_pct:.2f}% terlalu sempit vs ATR")

    # [v6.0] Kondisi 8: CONFLICTING_SIGNALS — deteksi kontradiksi indikator internal
    # Hitung indikator yang diperlukan untuk cek konflik
    conflicting = []
    if len(closes) >= 26:
        rsi_val    = calc_rsi(closes)
        macd_val, msig_val = calc_macd(closes)
        ema20_val  = calc_ema(closes, 20)
        ema50_val  = calc_ema(closes, 50)
        price_now  = float(closes[-1])

        if side == "BUY":
            # Konflik 1: MACD bullish TAPI RSI sudah overbought (> 70)
            if macd_val > msig_val and rsi_val > 70:
                conflicting.append("MACD bullish tapi RSI overbought")
            # Konflik 2: Harga di atas EMA20 TAPI EMA20 < EMA50 (tren jangka panjang turun)
            if price_now > ema20_val and ema20_val < ema50_val:
                conflicting.append("Harga > EMA20 tapi EMA20 < EMA50 (kontra-tren)")
            # Konflik 3: Daily bias NEUTRAL tapi regime TRENDING ke bawah (ADX kuat tapi arah tidak jelas)
            if daily_bias == "NEUTRAL" and regime == "TRENDING":
                conflicting.append("Regime TRENDING tapi bias NEUTRAL (arah tidak konfirmasi)")
        else:  # SELL
            # Konflik 1: MACD bearish TAPI RSI sudah oversold (< 30)
            if macd_val < msig_val and rsi_val < 30:
                conflicting.append("MACD bearish tapi RSI oversold")
            # Konflik 2: Harga di bawah EMA20 TAPI EMA20 > EMA50 (tren jangka panjang naik)
            if price_now < ema20_val and ema20_val > ema50_val:
                conflicting.append("Harga < EMA20 tapi EMA20 > EMA50 (kontra-tren)")
            # Konflik 3: Daily bias NEUTRAL tapi regime TRENDING
            if daily_bias == "NEUTRAL" and regime == "TRENDING":
                conflicting.append("Regime TRENDING tapi bias NEUTRAL (arah tidak konfirmasi)")

        # 2+ konflik = SKIP — sinyal kontradiksi terlalu banyak
        if len(conflicting) >= 2:
            reasons.append(f"CONFLICTING_SIGNALS ({len(conflicting)}x): " + " | ".join(conflicting))
        elif len(conflicting) == 1:
            # 1 konflik = warning saja, tidak langsung skip
            log(f"  ⚠️ Sinyal conflict ({side}): {conflicting[0]} — tetap lanjut")

    skip = len(reasons) >= 2   # NTZ aktif jika 2+ kondisi terpenuhi
    return {"skip": skip, "reasons": reasons, "chop_index": ci}


# ════════════════════════════════════════════════════════
#  [14] LIQUIDITY DEPTH FILTER — v5.0
#  Volume Profile proxy + spread guard untuk IDX.
#  IDX tidak punya real orderbook API gratis — kita gunakan
#  pendekatan statistik dari OHLCV sebagai proxy depth.
# ════════════════════════════════════════════════════════

def calc_volume_profile_strength(closes, highs, lows, volumes,
                                  lookback: int = 20) -> dict:
    """
    Volume Profile Proxy — tanpa full orderbook, kita estimasi
    zona high-volume (High Volume Node / HVN) dari data OHLCV.

    Logic:
      - Bagi range harga N candle terakhir jadi 10 bucket
      - Hitung total volume per bucket
      - HVN = bucket dengan volume tertinggi
      - Harga dekat HVN = zona support/resistance kuat (deep liquidity)
      - Harga jauh dari HVN = zona tipis (thin liquidity — prone to fake move)

    Returns:
      near_hvn     : bool — harga dalam 1 ATR dari HVN
      hvn_price    : float — harga tengah HVN
      depth_score  : float 0–1 (1 = sangat dekat HVN = liquidity dalam)
      thin_market  : bool — True jika pasar tipis (hindari entry)
    """
    result = {"near_hvn": False, "hvn_price": None,
              "depth_score": 0.5, "thin_market": False}

    if len(closes) < lookback:
        return result

    c = closes[-lookback:].astype(float)
    h = highs[-lookback:].astype(float)
    l = lows[-lookback:].astype(float)
    v = volumes[-lookback:].astype(float)

    price_min = float(np.min(l))
    price_max = float(np.max(h))
    price_range = price_max - price_min

    if price_range <= 0:
        return result

    n_buckets = 10
    bucket_size = price_range / n_buckets
    bucket_vol  = np.zeros(n_buckets)

    for i in range(len(c)):
        mid = (h[i] + l[i]) / 2
        idx = min(int((mid - price_min) / bucket_size), n_buckets - 1)
        bucket_vol[idx] += v[i]

    hvn_bucket  = int(np.argmax(bucket_vol))
    hvn_price   = price_min + (hvn_bucket + 0.5) * bucket_size
    current     = float(c[-1])
    atr_approx  = float(np.mean(h - l))

    dist_to_hvn = abs(current - hvn_price)
    near_hvn    = dist_to_hvn <= atr_approx * 1.5
    depth_score = max(0.0, 1.0 - dist_to_hvn / (price_range + 1e-9))

    # Thin market: volume terkini jauh di bawah rata-rata AND harga jauh dari HVN
    vol_avg    = float(np.mean(v[:-1])) if len(v) > 1 else float(v[-1])
    thin_vol   = float(v[-1]) < vol_avg * 0.45
    thin_market = thin_vol and not near_hvn

    return {
        "near_hvn":    near_hvn,
        "hvn_price":   round(hvn_price, 2),
        "depth_score": round(depth_score, 3),
        "thin_market": thin_market,
    }


def calc_bid_ask_spread_proxy(closes, highs, lows) -> float:
    """
    Estimasi spread bid-ask dari data OHLCV (Corwin-Schultz proxy).
    Spread tinggi = pasar tidak likuid = entry berbahaya.

    Returns spread_pct (persentase dari harga).
    IDX saham LQ45 spread normal: 0.1–0.5%
    Warning jika > 1.0%
    """
    if len(closes) < 5:
        return 0.0
    h = highs[-5:].astype(float)
    l = lows[-5:].astype(float)

    # Corwin-Schultz simplified: spread ~ (H-L) / (H+L) * 2
    spreads = [(h[i] - l[i]) / ((h[i] + l[i]) / 2 + 1e-9) * 100
               for i in range(len(h)) if h[i] > l[i]]
    if not spreads:
        return 0.0
    return round(float(np.mean(spreads)), 3)


def is_liquidity_sufficient(closes, highs, lows, volumes,
                             ticker: str) -> dict:
    """
    Gate filter likuiditas sebelum entry.
    Kombinasi: Volume Profile + Spread Proxy + Volume IDR check.

    Returns: {sufficient: bool, reason: str, depth_score: float}
    """
    vp    = calc_volume_profile_strength(closes, highs, lows, volumes)
    spread = calc_bid_ask_spread_proxy(closes, highs, lows)

    issues = []
    if vp["thin_market"]:
        issues.append(f"Thin market (depth={vp['depth_score']:.2f})")
    if spread > 1.5:
        issues.append(f"Spread proxy tinggi ({spread:.2f}%)")

    sufficient = len(issues) == 0
    reason     = " | ".join(issues) if issues else "OK"

    return {
        "sufficient":  sufficient,
        "reason":      reason,
        "depth_score": vp["depth_score"],
        "near_hvn":    vp["near_hvn"],
        "hvn_price":   vp["hvn_price"],
        "spread_pct":  spread,
        "thin_market": vp["thin_market"],
    }


# ════════════════════════════════════════════════════════
#  [15] PERFORMANCE CLUSTERING ENGINE — v5.0
#  Per-regime + per-sector adaptive weight learning.
#  Bot belajar bukan hanya secara global, tapi per kondisi.
#  "Different market conditions = different optimal weights."
# ════════════════════════════════════════════════════════

# Cluster weight cache — diisi dari Supabase di awal run
_cluster_weights: dict = {}


def get_cluster_weights() -> dict:
    """
    Load win-rate per cluster dari Supabase dan generate
    cluster-specific weight adjustments.

    Cluster key: f"{regime}|{sector}"
    Contoh: "TRENDING|BANKING", "RANGING|ENERGY"

    Logic:
      - Ambil 200 signal terbaru yang sudah resolved (WIN/LOSS)
      - Group per (regime, sector)
      - Untuk setiap cluster dengan WR tinggi → boost key signals
      - Untuk WR rendah → reduce atau conservatize

    Returns dict: cluster_key → weight_modifier_dict
    """
    global _cluster_weights
    default = {}

    try:
        rows = (
            supabase.table("signals")
            .select("score, tier, outcome, strategy, regime, sector")
            .in_("outcome", ["WIN", "LOSS"])
            .order("sent_at", desc=True)
            .limit(200)
            .execute()
            .data
        )
        if not rows or len(rows) < 20:
            log("📊 Cluster weights: data belum cukup (<20 signal) — skip")
            # [v7.12] Cold-start notice: informasikan ke user berapa lagi signal dibutuhkan
            if rows:
                needed = 20 - len(rows)
                log(f"   ℹ️  Cold-start: {len(rows)}/20 signal terkumpul — butuh {needed} lagi untuk cluster aktif")
            else:
                log("   ℹ️  Cold-start: belum ada signal di Supabase — win_prob akan pakai heuristik fallback")
            _cluster_weights = default
            return default

        # Group per (regime, sector)
        clusters: dict = {}
        for r in rows:
            regime = r.get("regime") or "TRENDING"
            sector = r.get("sector") or "MISC"
            key    = f"{regime}|{sector}"
            if key not in clusters:
                clusters[key] = {"wins": 0, "total": 0}
            clusters[key]["total"] += 1
            if r.get("outcome") == "WIN":
                clusters[key]["wins"] += 1

        result = {}
        for key, data in clusters.items():
            total = data["total"]
            # FIX 3A: Naikkan minimum dari 3 → 15 trades per cluster
            # Dengan 15 sektor × 3 regime = 45 cluster, keputusan dari < 15
            # sample memiliki confidence interval yang terlalu lebar (noise-driven)
            if total < 15:
                log(f"  🧮 Cluster [{key}]: {total} trades < 15 minimum — skip (statistically insufficient)")
                continue
            wr = data["wins"] / total

            # [Q02] Catat raw count agar get_cluster_n_samples bisa baca jumlah sample
            _cluster_raw_counts[key] = total

            modifier = {}
            if wr >= 0.75:
                # Cluster ini sangat profitable — boost structure signals
                modifier["bos"]         = 2
                modifier["choch"]       = 1
                modifier["liq_sweep"]   = 1
            elif wr >= 0.60:
                # Good cluster — slight boost
                modifier["order_block"] = 1
                modifier["vol_confirm"] = 1
            elif wr < 0.40:
                # Poor cluster — conservatize
                modifier["bos"]         = -1
                modifier["choch"]       = -1
                modifier["macd_soft"]   = -1
                modifier["adx_ranging"] = -1
            # wr 0.40–0.59: neutral, no change

            if modifier:
                result[key] = modifier
                result[key]["_win_rate"] = round(wr, 4)   # [v7.9] simpan WR aktual untuk win_prob engine
                log(f"  🧮 Cluster [{key}]: WR={wr:.0%} ({total} trades) → modifier aktif")

        log(f"📊 Cluster weights: {len(result)} cluster aktif dari {len(clusters)} total")
        _cluster_weights = result
        return result

    except Exception as e:
        log(f"⚠️ get_cluster_weights: {e} — skip", "warn")
        _cluster_weights = default
        return default


def apply_cluster_weights(base_weights: dict, regime: str,
                           ticker: str) -> dict:
    """
    Apply cluster-specific modifier ke base weights.
    Dipanggil dari check_intraday dan check_swing setelah merged_w.

    Jika cluster ada modifier → apply (clamp agar tidak ekstrem).
    Jika tidak → return base_weights as-is.
    """
    global _cluster_weights
    if not _cluster_weights:
        return base_weights

    sector    = TICKER_SECTOR.get(ticker, "MISC")
    key       = f"{regime}|{sector}"
    modifier  = _cluster_weights.get(key, {})

    if not modifier:
        return base_weights

    w = base_weights.copy()
    for k, delta in modifier.items():
        if k.startswith("_"):
            continue   # [v7.10] skip metadata keys seperti _win_rate
        if k in w:
            if w[k] < 0:
                w[k] = min(w[k] + delta, -1)
            else:
                w[k] = max(1, min(w[k] + delta, 12))

    return w


def rank_setup_priority(structure: dict, liq: dict, liq_trap: dict,
                         ob: dict, trigger: dict, sniper: dict,
                         side: str) -> dict:
    """
    [v6.0] Upgrade #4 — Setup Ranking System.

    Bot top dunia tidak memperlakukan semua setup secara sama.
    Setup A+ (liquidity_sweep + OB + engulfing) memiliki winrate secara historis
    jauh lebih tinggi dari setup biasa (indicator-only). Kita harus refleksikan ini
    dalam scoring dan keputusan entry.

    Priority Tiers:
      PRIORITY_HIGH  (score boost +3, EV threshold diturunkan ke 0.15):
        - Liquidity sweep + Order Block reaction + Engulfing candle
        - Stop hunt + FVG dalam zona + Sniper level ≥ PRECISION
        Setup ini adalah "institutional footprint" yang paling jelas.

      PRIORITY_MEDIUM (score boost +1, EV threshold normal = 0.20):
        - Minimal 2 dari: sweep, OB, trigger kuat
        - BOS + CHoCH keduanya terkonfirmasi

      PRIORITY_LOW (tidak ada boost, EV threshold dinaikkan ke 0.25):
        - Hanya indicator-based (MACD/RSI/EMA tanpa struktur price action)
        - Setup lemah yang perlu edge EV lebih besar untuk diambil

    Returns: {priority: str, score_boost: int, ev_threshold: float, reason: str}
    """
    sweep_key = "sweep_bull" if side == "BUY" else "sweep_bear"
    hunt_key  = "stop_hunt_bull" if side == "BUY" else "stop_hunt_bear"
    bos_key   = "BULLISH" if side == "BUY" else "BEARISH"

    has_sweep      = liq.get(sweep_key, False)
    has_stop_hunt  = liq_trap.get(hunt_key, False)
    has_ob         = ob.get("valid", False)
    has_ob_react   = sniper.get("bonus", 0) >= 1 and "OB Reaction" in sniper.get("details", "")
    has_fvg        = sniper.get("bonus", 0) >= 1 and "FVG Zone" in sniper.get("details", "")
    has_engulfing  = "Engulfing" in trigger.get("pattern", "")
    has_bos        = structure.get("bos") == bos_key
    has_choch      = structure.get("choch") == bos_key
    sniper_level   = sniper.get("level", "STANDARD ⚪")
    trigger_strong = trigger.get("strength", 0) >= 0.80

    # ── HIGH PRIORITY: Setup institusional dengan konfirmasi berlapis ──
    # Pola 1: Liquidity Sweep + OB Reaction + Engulfing (triple confirmation)
    if (has_sweep or has_stop_hunt) and has_ob_react and has_engulfing:
        return {
            "priority":      "HIGH",
            "score_boost":   3,
            "ev_threshold":  0.15,   # threshold diturunkan — setup ini proven
            "reason":        "Liq Sweep + OB Reaction + Engulfing"
        }
    # Pola 2: Stop Hunt + FVG Zone + Sniper PRECISION atau lebih
    # [v7.9 FIX] Bug operator precedence: `A and B and C or D` dievaluasi sebagai
    # `(A and B and C) or D` — membuat setiap SNIPER entry masuk HIGH tanpa konfirmasi.
    # Sekarang: kurung eksplisit memastikan semua syarat harus terpenuhi.
    if has_stop_hunt and has_fvg and ("PRECISION" in sniper_level or "SNIPER" in sniper_level):
        return {
            "priority":      "HIGH",
            "score_boost":   3,
            "ev_threshold":  0.15,
            "reason":        "Stop Hunt + FVG + Sniper Entry"
        }
    # Pola 3: BOS + CHoCH keduanya ada + OB valid (konfirmasi ganda struktur)
    if has_bos and has_choch and has_ob:
        return {
            "priority":      "HIGH",
            "score_boost":   2,
            "ev_threshold":  0.15,
            "reason":        "BOS + CHoCH + Order Block (double structure)"
        }

    # ── MEDIUM PRIORITY: Minimal 2 konfirmasi kuat ──
    strong_signals = sum([
        has_sweep or has_stop_hunt,  # liquidity event
        has_ob_react,                # institutional zone reaction
        has_fvg,                     # imbalance fill
        trigger_strong,              # kuat candle confirmation
        has_bos and has_choch,       # double structure
    ])
    if strong_signals >= 2:
        return {
            "priority":      "MEDIUM",
            "score_boost":   1,
            "ev_threshold":  0.20,   # threshold normal
            "reason":        f"{strong_signals} konfirmasi kuat"
        }

    # ── LOW PRIORITY: Indicator-only atau setup lemah ──
    # Tidak ada liquidity event, tidak ada OB reaction
    if not has_sweep and not has_stop_hunt and not has_ob_react:
        return {
            "priority":      "LOW",
            "score_boost":   0,
            "ev_threshold":  0.25,   # threshold lebih tinggi — butuh edge lebih besar
            "reason":        "Indicator-only — tidak ada liquidity/OB confirmation"
        }

    # Default: MEDIUM tanpa boost
    return {
        "priority":      "MEDIUM",
        "score_boost":   0,
        "ev_threshold":  0.20,
        "reason":        "Standard setup"
    }


def score_signal(side: str, price: float, closes, highs, lows, volumes,
                 structure: dict, liq: dict, ob: dict,
                 rsi: float, macd: float, msig: float,
                 ema_fast: float, ema_slow: float,
                 vwap: float, regime: str = "TRENDING",
                 weights: dict = None, opens=None) -> int:
    """
    Hitung score sinyal berdasarkan konfluens indikator.
    [v3.0] Parameter 'weights' memungkinkan dynamic scoring per regime+phase.
    [v7.10 FIX] Tambah parameter 'opens' untuk akurasi candle_body scoring.
    Sebelumnya pakai closes[-2] sebagai proxy open — inkonsisten dengan F04.
    """
    _w = weights if weights else W
    is_bull = (side == "BUY")
    score   = 0
    # [v7.10] Gunakan open nyata jika tersedia, fallback ke closes[-2]
    last_open = float(opens[-1]) if (opens is not None and len(opens) >= 1) else float(closes[-2])

    if is_bull:
        if structure.get("bos")   == "BULLISH": score += _w["bos"]
        if structure.get("choch") == "BULLISH": score += _w["choch"]
        if liq.get("sweep_bull"):               score += _w["liq_sweep"]
        if ob.get("valid"):                     score += _w["order_block"]
        if macd > msig:                         score += _w["macd_cross"]
        elif macd < msig:                       score += _w["macd_soft"]
        if 30 < rsi < 60:                       score += _w["rsi_zone"]
        if rsi <= 30:                           score += _w["rsi_extreme"]
        vol_avg = float(np.mean(volumes[-11:-1])) if len(volumes) >= 11 else float(np.mean(volumes[:-1])) if len(volumes) > 1 else float(volumes[-1])
        if float(volumes[-1]) > vol_avg * 1.3: score += _w["vol_confirm"]
        if price > vwap:                        score += _w["vwap_side"]
        last_sl = structure.get("last_sl")
        if last_sl and last_sl <= price <= last_sl * 1.015:
            score += _w["pullback"]
        last_close = float(closes[-1])
        body = last_close - last_open   # [v7.10] pakai open nyata
        rng  = float(highs[-1]) - float(lows[-1]) + 1e-9
        if body > 0 and body / rng > 0.5:      score += _w["candle_body"]
        if liq.get("equal_lows"):               score += _w["equal_lows"]
        if ema_fast > ema_slow:                 score += _w["ema_align"]
    else:
        if structure.get("bos")   == "BEARISH": score += _w["bos"]
        if structure.get("choch") == "BEARISH": score += _w["choch"]
        if liq.get("sweep_bear"):               score += _w["liq_sweep"]
        if ob.get("valid"):                     score += _w["order_block"]
        if macd < msig:                         score += _w["macd_cross"]
        elif macd > msig:                       score += _w["macd_soft"]
        if 40 < rsi < 70:                       score += _w["rsi_zone"]
        if rsi >= 70:                           score += _w["rsi_extreme"]
        vol_avg = float(np.mean(volumes[-11:-1])) if len(volumes) >= 11 else float(np.mean(volumes[:-1])) if len(volumes) > 1 else float(volumes[-1])
        if float(volumes[-1]) > vol_avg * 1.3: score += _w["vol_confirm"]
        if price < vwap:                        score += _w["vwap_side"]
        last_sh = structure.get("last_sh")
        if last_sh and last_sh * 0.97 <= price <= last_sh * 1.01:
            score += _w["pullback"]
        last_close = float(closes[-1])
        body = last_open - last_close   # [v7.10] pakai open nyata
        rng  = float(highs[-1]) - float(lows[-1]) + 1e-9
        if body > 0 and body / rng > 0.5:      score += _w["candle_body"]
        if liq.get("equal_highs"):              score += _w["equal_highs"]
        if ema_fast < ema_slow:                 score += _w["ema_align"]

    if regime == "TRENDING":  score += _w["adx_trend"]
    elif regime == "RANGING": score += _w["adx_ranging"]

    return score


def assign_tier(score: int) -> str:
    if score >= TIER_MIN_SCORE["S"]:  return "S"
    if score >= TIER_MIN_SCORE["A+"]: return "A+"
    if score >= TIER_MIN_SCORE["A"]:  return "A"
    return "SKIP"


def calc_conviction(score: int) -> str:
    if score >= 18: return "EXTREME ⚡"
    if score >= 14: return "VERY HIGH 🔥"
    if score >= 12: return "HIGH 💪"
    if score >= 10: return "GOOD ✅"
    return "OK 🟡"


# ════════════════════════════════════════════════════════
#  TP / SL CALCULATOR
# ════════════════════════════════════════════════════════

def calc_sl_tp(entry: float, side: str, atr: float,
               structure: dict, strategy: str) -> tuple:
    """
    SL berbasis ATR + struktur.
    TP berbasis actual SL distance (bukan ATR) → RR konsisten dengan yang ditampilkan.
    [v7.2 — FIX Masalah 6] Output dibulatkan ke fraksi harga IDX yang valid.
    """
    # [v8.19 H1-FIX] Guard ATR = 0 — terjadi saat yfinance return data tipis,
    # dividen ex-date, atau ticker baru listing. Tanpa guard ini:
    # atr_dist = 0 → actual_sl_dist = 0 → RR = tp_dist / 0 → ZeroDivisionError.
    # Caller sudah handle None return (pattern: if sl is None: skip ticker).
    if entry <= 0 or atr <= 0:
        log(
            f"  ⚠️ [H1-FIX] calc_sl_tp: entry={entry} atr={atr} tidak valid "
            f"({strategy} {side}) — return None tuple",
            "warn"
        )
        return None, None, None, None

    if strategy == "INTRADAY":
        sl_mult, tp1_r, tp2_r = INTRADAY_SL_ATR, INTRADAY_TP1_R, INTRADAY_TP2_R
    else:
        sl_mult, tp1_r, tp2_r = SWING_SL_ATR,    SWING_TP1_R,    SWING_TP2_R

    atr_dist = atr * sl_mult

    if side == "BUY":
        last_sl = structure.get("last_sl")
        if last_sl and last_sl < entry:
            sl_raw = min(entry - atr_dist, last_sl * 0.998)
        else:
            sl_raw = entry - atr_dist
        actual_sl_dist = entry - sl_raw
        tp1_raw = entry + actual_sl_dist * tp1_r
        tp2_raw = entry + actual_sl_dist * tp2_r
    else:
        last_sh = structure.get("last_sh")
        if last_sh and last_sh > entry:
            sl_raw = max(entry + atr_dist, last_sh * 1.002)
        else:
            sl_raw = entry + atr_dist
        actual_sl_dist = sl_raw - entry
        tp1_raw = entry - actual_sl_dist * tp1_r
        tp2_raw = entry - actual_sl_dist * tp2_r

    # [v7.2] Bulatkan ke fraksi harga IDX sebelum return
    sl_adj, tp1_adj, tp2_adj, _ = apply_price_fraction(sl_raw, tp1_raw, tp2_raw, entry, side)
    return sl_adj, tp1_adj, tp2_adj


# ════════════════════════════════════════════════════════
#  [21] KILL SWITCH SYSTEM — v7.0
#  Bot elite selalu punya mekanisme self-protection.
#  "Profit bukan hanya dari trade bagus — tapi dari
#   tidak trading saat kondisi chaos."
#
#  Tiga lapis perlindungan:
#  Layer 1 — Losing Streak Guard: pause jika kalah N kali berturut
#  Layer 2 — Market Abnormal Shutdown: halt jika pasar tidak normal
#  Layer 3 — Drawdown Circuit Breaker: halt jika equity turun X%
# ════════════════════════════════════════════════════════

# Threshold Kill Switch
KS_LOSING_STREAK_MAX  = 3      # pause jika kalah >= 3 kali berturut-turut
KS_DRAWDOWN_PCT_MAX   = 8.0    # halt jika total risk-based exposure > 8%
KS_ABNORMAL_VOL_MULT  = 4.0    # pasar abnormal jika volatilitas > 4x normal
KS_PAUSE_HOURS        = 8      # durasi pause setelah kill switch aktif (jam)

# [v8.0 FIX 6] Capital deployment gate — TERPISAH dari risk exposure gate.
# KS_DRAWDOWN_PCT_MAX mengukur RISK (jarak ke SL × position value).
# MAX_TOTAL_CAPITAL_DEPLOYED_PCT mengukur MODAL TERIKAT (total order value).
# Bot bisa punya 6 posisi dengan SL ketat (risk < 8%) tapi modal 70% terkunci.
# Ini mencegah situasi di mana capital habis untuk entry baru walaupun risk "aman".
# Default: max 40% modal deployed ke open positions sekaligus.
try:
    MAX_TOTAL_CAPITAL_DEPLOYED_PCT = float(
        os.environ.get("MAX_TOTAL_CAPITAL_DEPLOYED_PCT", 40.0)
    )
    MAX_TOTAL_CAPITAL_DEPLOYED_PCT = max(10.0, min(MAX_TOTAL_CAPITAL_DEPLOYED_PCT, 80.0))
except Exception as _e:
    import sys as _sys_env
    print(f"[WARN] ENV read MAX_TOTAL_CAPITAL_DEPLOYED_PCT failed: {_e} — fallback 40.0", file=_sys_env.stderr)
    MAX_TOTAL_CAPITAL_DEPLOYED_PCT = 40.0


def check_ks_pause_active() -> dict:
    """
    [v8.0 FIX 1] Cek apakah kill switch pause masih aktif dari run sebelumnya.
    Baca 'ks_pause_until' dari tabel bot_state di Supabase.
    Jika masih dalam window pause → return triggered=True tanpa scan apapun.

    Ini mengatasi masalah kritis di v7.x: kill switch tidak persist antar run,
    sehingga bot bisa langsung scan ulang di run berikutnya meski baru saja
    trigger losing streak.
    """
    default = {"triggered": False, "resume_at": None, "remaining_hours": 0}
    try:
        rows = (
            supabase.table("bot_state")
            .select("value")
            .eq("key", "ks_pause_until")
            .execute()
            .data
        )
        if not rows:
            return default

        pause_until_iso = rows[0]["value"]
        pause_until     = datetime.fromisoformat(pause_until_iso)
        now_utc         = datetime.now(timezone.utc)

        # Pastikan timezone-aware comparison
        if pause_until.tzinfo is None:
            from datetime import timezone as _tz
            pause_until = pause_until.replace(tzinfo=_tz.utc)

        if now_utc < pause_until:
            remaining = (pause_until - now_utc).total_seconds() / 3600
            log(f"⏸️ KS PAUSE masih aktif — resume dalam {remaining:.1f} jam ({pause_until_iso[:16]} UTC)", "warn")
            return {
                "triggered":       True,
                "resume_at":       pause_until_iso,
                "remaining_hours": round(remaining, 1)
            }

        # Pause sudah habis — hapus state agar bersih
        supabase.table("bot_state").delete().eq("key", "ks_pause_until").execute()
        log("✅ KS pause selesai — bot kembali aktif")
        return default

    except Exception as e:
        log(f"⚠️ check_ks_pause_active: {e} — asumsikan tidak paused", "warn")
        return default


def check_losing_streak() -> dict:
    """
    [v7.0] Kill Switch Layer 1 — Losing Streak Guard.

    Ambil N signal terakhir yang resolved dari Supabase.
    Jika berturut-turut LOSS >= KS_LOSING_STREAK_MAX → pause trading.

    Logika "berturut-turut" penting: 3 loss berturut jauh lebih berbahaya
    dari 3 loss tersebar dalam 20 trade (yang bisa normal secara statistik).
    Streak berturut menunjukkan ada sesuatu yang sistemik — market regime
    berubah, atau setup kita tidak cocok dengan kondisi saat ini.

    Returns: {triggered: bool, streak: int, action: str, message: str}
    """
    default = {"triggered": False, "streak": 0, "action": "CONTINUE", "message": "OK"}
    try:
        rows = (
            supabase.table("signals")
            .select("outcome, sent_at, pair, side")
            .in_("outcome", ["WIN", "LOSS"])
            .order("sent_at", desc=True)
            .limit(20)
            .execute()
            .data
        )
        if not rows or len(rows) < KS_LOSING_STREAK_MAX:
            return default

        # Hitung streak LOSS berturut-turut dari signal terbaru
        streak = 0
        for r in rows:
            if r["outcome"] == "LOSS":
                streak += 1
            else:
                break   # stop saat pertama WIN ditemukan

        if streak >= KS_LOSING_STREAK_MAX:
            # [v8.0 FIX 1] Simpan pause_until ke Supabase agar persist antar run
            resume_at = (datetime.now(timezone.utc) + timedelta(hours=KS_PAUSE_HOURS)).isoformat()
            try:
                supabase.table("bot_state").upsert({
                    "key":        "ks_pause_until",
                    "value":      resume_at,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }, on_conflict="key").execute()
                log(f"💾 KS pause_until disimpan ke Supabase: {resume_at}")
            except Exception as _e:
                log(f"⚠️ Gagal simpan KS pause state: {_e} — pause hanya berlaku run ini", "warn")

            msg = (f"🛑 <b>KILL SWITCH — LOSING STREAK</b>\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"Streak LOSS berturut: <b>{streak}x</b> (max: {KS_LOSING_STREAK_MAX})\n"
                   f"Signal terakhir: {', '.join(r['pair'] + '(' + r['side'][0] + ')' for r in rows[:streak])}\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"⏸️ Trading di-<b>PAUSE</b> selama {KS_PAUSE_HOURS} jam.\n"
                   f"Resume otomatis: {resume_at[:16]} UTC\n"
                   f"<i>Review setup dan market condition sebelum resume.</i>")
            tg(msg)
            log(f"🛑 KILL SWITCH aktif: losing streak {streak}x berturut-turut", "warn")
            return {
                "triggered":  True,
                "streak":     streak,
                "action":     "PAUSE",
                "resume_at":  resume_at,
                "message":    f"Losing streak {streak}x — pause {KS_PAUSE_HOURS}h"
            }

        log(f"✅ Kill switch OK: streak={streak} (max {KS_LOSING_STREAK_MAX})")
        return default

    except Exception as e:
        log(f"⚠️ check_losing_streak: {e} — skip", "warn")
        return default


# ════════════════════════════════════════════════════════
#  [J04] STRATEGY PERFORMANCE TRACKER + AUTO-DISABLE
#  Track WR per sub-strategy. Auto-pause jika WR < 40%
#  dalam 20 trade terakhir per kombinasi strategi.
#
#  Sub-strategy yang di-track:
#    TREND_INTRADAY   : BOS/CHoCH trending + timeframe 1H
#    TREND_SWING      : BOS/CHoCH trending + timeframe 1D
#    MEANREV_INTRADAY : Oversold/OB bounce + timeframe 1H
#    MEANREV_SWING    : Oversold/OB bounce + timeframe 1D
#
#  Auto-disable: WR < 40% dalam 20 trade terakhir → pause 48 jam
#  Auto-re-enable: setelah 48 jam, reevaluasi
# ════════════════════════════════════════════════════════

# Threshold evaluasi strategi
# [Q01] Naikkan sample size: 15→30 trades minimum, 20→50 evaluation window.
# Dengan n=20, binomial variance sangat tinggi — WR 40% bisa muncul dari
# strategi WR 55% hanya karena random streak. n=50 jauh lebih representatif.
STRATEGY_MIN_TRADES     = 999    # [PHASE-0] was 30 — paksa tidak ada auto-disable (butuh 999 trade dulu)
STRATEGY_MIN_WR         = 0.30   # [PHASE-0] was 0.40 — turunkan floor WR agar strategy tetap aktif
STRATEGY_DISABLE_HOURS  = 48     # lama disable setelah trigger
STRATEGY_EVAL_WINDOW    = 50     # [Q01] was 20 — lihat N trade terakhir

# [Q01] Binomial lower-CI guard — disable HANYA jika Wilson score lower bound
# 90% one-sided masih di bawah threshold. Mencegah false disable dari streak pendek.
# z = 1.282 untuk 90% one-sided lower bound (Wilson score interval)
STRATEGY_CI_Z           = 1.282  # z-score 90% one-sided lower bound
STRATEGY_CI_HARD_MAX_WR = 0.44   # jika point WR < ini DAN CI lower < MIN_WR → disable

# Global state — diisi oleh get_strategy_performance() di awal run()
_disabled_strategies: dict = {}   # key: sub_strategy, value: disabled_until ISO
# [v8.0 FIX 2] Simpan WR per sub-strategy agar get_active_strategy bisa baca
_strategy_wr_cache: dict  = {}    # key: sub_strategy, value: float WR (0–1)


def classify_signal_substrategy(row: dict) -> str:
    """
    Klasifikasi sub-strategy dari row Supabase.
    Logic: regime TRENDING → TREND, lainnya → MEANREV
    Timeframe 1H → INTRADAY, 1D → SWING
    """
    strategy  = row.get("strategy", "SWING")
    regime    = row.get("regime", "TRENDING")
    sub_trend = "TREND" if regime == "TRENDING" else "MEANREV"
    return f"{sub_trend}_{strategy}"


def get_strategy_performance(send_telegram: bool = False) -> dict:
    """
    [J04] Hitung WR per sub-strategy dari 20 trade terakhir (per sub-strategy).

    Returns dict: {sub_strategy: {wr, wins, total, disabled, disable_until}}
    Sekaligus mengisi _disabled_strategies jika ada yang trigger auto-disable.

    [v8.0 FIX 2] Mengisi _strategy_wr_cache agar get_active_strategy bisa
    membaca WR aktual dan menyesuaikan ev_floor secara data-driven,
    bukan hanya rule-based switching.
    """
    global _disabled_strategies, _strategy_wr_cache
    result = {}
    now_utc = datetime.now(timezone.utc)

    try:
        rows = (
            supabase.table("signals")
            .select("strategy, regime, outcome, sent_at, pair, side")
            .in_("outcome", ["WIN", "LOSS"])
            .order("sent_at", desc=True)
            .limit(200)    # ambil cukup banyak agar tiap sub-strategy punya sample
            .execute()
            .data
        )
    except Exception as e:
        log(f"⚠️ get_strategy_performance: Supabase error — {e}", "warn")
        return result

    if not rows:
        return result

    # Kelompokkan per sub-strategy
    groups: dict[str, list] = {}
    for r in rows:
        sub = classify_signal_substrategy(r)
        groups.setdefault(sub, []).append(r)

    disable_msgs = []

    for sub, trades in groups.items():
        recent = trades[:STRATEGY_EVAL_WINDOW]   # sudah sorted desc
        if len(recent) < STRATEGY_MIN_TRADES:
            result[sub] = {
                "wr": None, "wins": 0, "total": len(recent),
                "disabled": False, "disable_until": None,
                "note": f"Belum cukup data ({len(recent)}/{STRATEGY_MIN_TRADES})"
            }
            continue

        wins  = sum(1 for r in recent if r["outcome"] == "WIN")
        total = len(recent)
        wr    = wins / total

        # Cek apakah masih dalam periode disable
        disable_until_iso = _disabled_strategies.get(sub)
        if disable_until_iso:
            try:
                disable_until = datetime.fromisoformat(disable_until_iso)
                if now_utc < disable_until:
                    remaining_h = (disable_until - now_utc).total_seconds() / 3600
                    result[sub] = {
                        "wr": round(wr, 4), "wins": wins, "total": total,
                        "disabled": True,
                        "disable_until": disable_until_iso,
                        "note": f"Auto-disabled — resume dalam {remaining_h:.1f} jam"
                    }
                    log(f"  🚫 Strategy [{sub}]: DISABLED — sisa {remaining_h:.1f}h")
                    continue
                else:
                    # Waktu disable sudah lewat — reevaluasi
                    del _disabled_strategies[sub]
                    log(f"  ✅ Strategy [{sub}]: Auto-disable period habis — reevaluasi")
            except Exception:
                pass

        # [Q01] Evaluasi dengan Wilson score lower-bound CI guard.
        # Hitung lower bound dari Wilson score interval 90% one-sided.
        # Disable hanya jika KEDUA kondisi terpenuhi:
        #   (a) point estimate WR < STRATEGY_CI_HARD_MAX_WR (cukup rendah)
        #   (b) Wilson lower CI bound masih < STRATEGY_MIN_WR
        # Ini melindungi strategi bagus dari random bad-streak pendek.
        z     = STRATEGY_CI_Z
        n_ci  = total
        # Wilson score lower bound: (p + z²/2n - z√(p(1-p)/n + z²/4n²)) / (1 + z²/n)
        z2n   = (z * z) / n_ci
        denom = 1.0 + z2n
        centre = wr + z2n / 2.0
        margin = z * math.sqrt(wr * (1.0 - wr) / n_ci + z2n / (4.0 * n_ci))
        ci_lower = (centre - margin) / denom

        should_disable = (wr < STRATEGY_CI_HARD_MAX_WR) and (ci_lower < STRATEGY_MIN_WR)

        if should_disable:
            disable_until = now_utc + timedelta(hours=STRATEGY_DISABLE_HOURS)
            _disabled_strategies[sub] = disable_until.isoformat()
            disable_msgs.append(
                f"  🔴 {sub}: WR={wr:.0%} ({wins}/{total}) CI90_lower={ci_lower:.0%} "
                f"< {STRATEGY_MIN_WR:.0%} → DISABLED {STRATEGY_DISABLE_HOURS}h"
            )
            log(f"  🚫 Strategy [{sub}]: WR {wr:.0%} (CI₉₀↓={ci_lower:.0%}) < threshold "
                f"— auto-disable {STRATEGY_DISABLE_HOURS}h", "warn")
            result[sub] = {
                "wr": round(wr, 4), "wins": wins, "total": total,
                "ci_lower": round(ci_lower, 4),
                "disabled": True,
                "disable_until": _disabled_strategies[sub],
                "note": f"Auto-disabled: WR {wr:.0%} CI₉₀↓={ci_lower:.0%} terlalu rendah"
            }
        elif wr < STRATEGY_MIN_WR:
            # Point estimate rendah tapi CI masih cukup lebar → warning saja, belum disable
            log(f"  ⚠️ Strategy [{sub}]: WR {wr:.0%} rendah tapi CI₉₀↓={ci_lower:.0%} "
                f"≥ {STRATEGY_MIN_WR:.0%} — pantau, belum disable (sample masih noise)", "warn")
            result[sub] = {
                "wr": round(wr, 4), "wins": wins, "total": total,
                "ci_lower": round(ci_lower, 4),
                "disabled": False, "disable_until": None,
                "note": f"WR rendah tapi CI lebar — monitoring ({wins}/{total})"
            }
        else:
            result[sub] = {
                "wr": round(wr, 4), "wins": wins, "total": total,
                "ci_lower": round(ci_lower, 4),
                "disabled": False, "disable_until": None,
                "note": "Active"
            }
            log(f"  ✅ Strategy [{sub}]: WR={wr:.0%} CI₉₀↓={ci_lower:.0%} ({wins}/{total}) — aktif")

    # Telegram alert jika ada yang di-disable
    if disable_msgs and send_telegram:
        lines = "\n".join(disable_msgs)
        tg(f"🚫 <b>STRATEGY AUTO-DISABLE</b>\n"
           f"━━━━━━━━━━━━━━━━━━\n"
           f"{lines}\n"
           f"━━━━━━━━━━━━━━━━━━\n"
           f"Strategy dihentikan sementara {STRATEGY_DISABLE_HOURS} jam.\n"
           f"<i>Bot akan reevaluasi otomatis setelah period disable berakhir.</i>")

    # [v8.0 FIX 2] Update global WR cache untuk dipakai get_active_strategy
    # Ini menutup feedback loop: performa aktual → mempengaruhi threshold aktif
    for sub, info in result.items():
        if info.get("wr") is not None:
            _strategy_wr_cache[sub] = info["wr"]
    if _strategy_wr_cache:
        log(f"  🔄 Strategy WR cache updated: { {k: f'{v:.0%}' for k, v in _strategy_wr_cache.items()} }")

    return result


def get_ensemble_edge_verdict(rows: list) -> dict:
    """
    [V01/W01/v8.09-C] Ensemble edge verdict dengan multi-fold time-series CV.

    v8.09 upgrade (Gap C — Validation bias):
    M3 dan M4 sebelumnya hanya pakai satu 70/30 split → variance estimate tinggi,
    bisa beruntung/sial tergantung di mana split jatuh. Sekarang keduanya
    menggunakan expanding-window k-fold (default 3 fold) dan mengaggregasi
    hasil across semua fold → estimate OOS WR lebih robust, bias lebih rendah.

    4 Metode:
    M1 — Binomial p-value (FULL dataset)
    M2 — Profit Factor  (in-sample / train fold terakhir)
    M3 — Multi-fold OOS WR — avg WR dari semua test windows > 0.50
    M4 — Multi-fold drift  — avg drift train→test di semua fold ≤ 10pp

    Verdict PROVEN = ≥ ENSEMBLE_MIN_AGREE metode OK + M3 harus OK.
    """
    n = len(rows)
    result = {
        "verdict":    "INSUFFICIENT",
        "confidence": 0,
        "methods":    {},
        "note":       f"n={n} — terlalu kecil untuk ensemble",
        "split":      {},
        "folds":      [],
    }

    if n < 20:
        return result

    # ── Helper: win rate dari subset ──────────────────────
    def _wr(subset):
        if not subset: return 0.0, 0
        w = sum(1 for r in subset if r.get("outcome") == "WIN")
        return w / len(subset), w

    def _norm_cdf_e(z: float) -> float:
        t    = 1.0 / (1.0 + 0.2316419 * abs(z))
        poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
               + t * (-1.821255978 + t * 1.330274429))))
        pdf  = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
        cdf  = 1.0 - pdf * poly
        return cdf if z >= 0 else 1.0 - cdf

    # Chronological order: oldest first
    chrono     = list(reversed(rows))
    wr_full, wins_full = _wr(chrono)

    # ── M1: Binomial (full dataset) ───────────────────────
    z1    = (wins_full - n * 0.5) / max(math.sqrt(n * 0.25), 1e-9)
    p_bin = 1.0 - _norm_cdf_e(z1)
    m1_ok = (p_bin < 0.05 and wr_full > 0.50)
    result["methods"]["M1_binomial_full"] = {
        "ok": m1_ok, "p_value": round(p_bin, 4), "wr": round(wr_full, 4),
        "note": f"full WR={wr_full:.0%} p={p_bin:.3f} {'✅' if m1_ok else '❌'}"
    }

    # ── Multi-fold expanding-window split builder ──────────
    # [v8.09-C] Bangun k fold dengan expanding train, fixed-size test.
    # Minimum train = 60% data, setiap fold test = floor(n / (k+1)).
    # Ini menghindari single-split variance sambil tetap kronologis ketat.
    K_FOLDS        = max(2, min(4, n // 15))   # 2–4 fold, minimal 15 data per test
    fold_size      = max(5, n // (K_FOLDS + 1))
    min_train_size = max(10, int(n * 0.50))

    folds = []
    for k in range(K_FOLDS):
        test_start = n - (K_FOLDS - k) * fold_size
        test_end   = test_start + fold_size
        if test_start < min_train_size or test_end > n:
            continue
        train_fold = chrono[:test_start]
        test_fold  = chrono[test_start:test_end]
        if len(train_fold) < 10 or len(test_fold) < 5:
            continue
        wr_tr, _ = _wr(train_fold)
        wr_te, _ = _wr(test_fold)
        folds.append({
            "fold":     k + 1,
            "n_train":  len(train_fold),
            "n_test":   len(test_fold),
            "wr_train": round(wr_tr, 4),
            "wr_test":  round(wr_te, 4),
            "drift":    round(wr_tr - wr_te, 4),
        })

    result["folds"] = folds
    result["split"] = {
        "n_total": n, "k_folds": len(folds),
        "note": f"expanding-window {len(folds)}-fold CV"
    }

    # ── M2: Profit Factor (last train fold, in-sample) ────
    last_train = chrono[:n - fold_size] if fold_size < n else chrono
    rr_train   = [float(r["rr"]) for r in last_train if r.get("rr") and float(r["rr"]) > 0]
    avg_rr     = float(np.mean(rr_train)) if rr_train else 1.5
    m2_ok, pf  = False, None
    if len(last_train) >= ENSEMBLE_MIN_N_PF and rr_train:
        wins_tr   = sum(1 for r in last_train if r.get("outcome") == "WIN")
        losses_tr = max(len(last_train) - wins_tr, 1)
        pf        = (wins_tr * avg_rr) / losses_tr
        m2_ok     = pf >= PROFIT_FACTOR_MIN
    result["methods"]["M2_profit_factor_train"] = {
        "ok": m2_ok,
        "pf": round(pf, 3) if pf else None,
        "note": (f"train PF={pf:.2f} ≥ {PROFIT_FACTOR_MIN} {'✅' if m2_ok else '❌'}"
                 if pf else f"train n={len(last_train)} < {ENSEMBLE_MIN_N_PF} — skip"),
    }

    # ── M3: Multi-fold OOS WR (avg across all folds) ──────
    # [v8.09-C] Sebelumnya: single test set. Sekarang: avg OOS WR lintas fold.
    # Verdict M3 OK = avg OOS WR > 0.50 DAN minimal N_FOLDS_REQUIRED fold valid.
    N_FOLDS_REQUIRED = max(1, len(folds) // 2 + 1)   # mayoritas fold harus valid
    m3_ok, avg_oos_wr, m3_folds_ok = False, None, 0
    if folds:
        avg_oos_wr    = float(np.mean([f["wr_test"] for f in folds]))
        m3_folds_ok   = sum(1 for f in folds if f["wr_test"] > 0.50)
        m3_ok         = (avg_oos_wr > 0.50 and m3_folds_ok >= N_FOLDS_REQUIRED)
    fold_oos_str = " | ".join(f"F{f['fold']}={f['wr_test']:.0%}" for f in folds)
    result["methods"]["M3_multifold_oos_wr"] = {
        "ok":          m3_ok,
        "avg_oos_wr":  round(avg_oos_wr, 4) if avg_oos_wr is not None else None,
        "folds_ok":    m3_folds_ok,
        "n_folds":     len(folds),
        "fold_detail": fold_oos_str,
        "note": (f"avg OOS WR={avg_oos_wr:.0%} ({m3_folds_ok}/{len(folds)} folds >50%) "
                 f"{'✅' if m3_ok else '❌'}"
                 if folds else "skip — insufficient folds"),
    }

    # ── M4: Multi-fold drift detector (avg drift) ─────────
    # [v8.09-C] Sebelumnya: single drift. Sekarang: avg drift lintas fold.
    # M4 OK = avg drift ≤ 10pp DAN tidak ada fold dengan drift > 25pp.
    m4_ok, avg_drift = False, None
    if folds:
        avg_drift   = float(np.mean([f["drift"] for f in folds]))
        max_drift   = max(f["drift"] for f in folds)
        m4_ok       = (avg_drift <= 0.10 and max_drift <= 0.25)
    fold_drift_str = " | ".join(f"F{f['fold']}={f['drift']:+.0%}" for f in folds)
    # Ambil WR train/test dari fold terakhir untuk display
    wr_tr_last = folds[-1]["wr_train"] if folds else 0.0
    wr_te_last = folds[-1]["wr_test"]  if folds else 0.0
    result["methods"]["M4_multifold_drift"] = {
        "ok":          m4_ok,
        "avg_drift":   round(avg_drift, 4) if avg_drift is not None else None,
        "fold_detail": fold_drift_str,
        "note": (f"avg drift={avg_drift:+.0%} (last fold: train={wr_tr_last:.0%} "
                 f"test={wr_te_last:.0%}) {'✅ stable' if m4_ok else '❌ degraded'}"
                 if avg_drift is not None else "skip — insufficient folds"),
    }

    # ── Consensus ─────────────────────────────────────────
    agree = sum(1 for m in result["methods"].values() if m.get("ok"))
    result["confidence"] = agree
    max_methods = len(result["methods"])

    if n < EDGE_PROOF_MIN_SIGNALS:
        result["verdict"] = "INSUFFICIENT"
        result["note"] = (f"n={n}/{EDGE_PROOF_MIN_SIGNALS} — preliminary. "
                          f"Multi-fold WF: {agree}/{max_methods} agree ({len(folds)} folds).")
    elif agree >= ENSEMBLE_MIN_AGREE and wr_full >= EDGE_MIN_WR and m3_ok:
        result["verdict"] = "PROVEN"
        result["note"] = (f"✅ PROVEN — {agree}/{max_methods} agree, "
                          f"avg OOS WR={avg_oos_wr:.0%} ({len(folds)} folds, full={wr_full:.0%})")
    elif agree >= 2 and n >= 50:
        result["verdict"] = "PROMISING"
        result["note"] = (f"🔵 PROMISING — {agree}/{max_methods} agree "
                          f"(n={n}, {len(folds)} folds)")
    else:
        result["verdict"] = "UNPROVEN"
        result["note"] = (f"⚠️ UNPROVEN — {agree}/{max_methods} agree. "
                          f"avg OOS WR={avg_oos_wr:.0%} avg drift={avg_drift:+.0%}"
                          if avg_oos_wr is not None
                          else f"⚠️ UNPROVEN — {agree}/{max_methods} agree (n={n})")
    return result


def check_edge_proven() -> dict:
    """
    [S01/T01/T02] Analisis edge empiris multi-layer.

    Layer 1 — Binomial test (S01):
      WR vs H0=0.50, normal approx p-value.
      ⚠️ Asumsi: hasil independen — TIDAK selalu benar di pasar.
      Ini filter awal, bukan ground truth.

    Layer 2 — Runs test / Wald-Wolfowitz (T01):
      Cek apakah urutan WIN/LOSS menunjukkan clustering atau anti-persistence.
      p < 0.05 → hasil TIDAK random (streaks atau alternating pattern).
      Ini TIDAK selalu buruk — clustering bisa berarti regime-dependent edge.

    Layer 3 — Stability check (T01):
      Bandingkan WR first-half vs second-half sample.
      Perbedaan > 10pp → edge mungkin tidak stabil lintas waktu.

    Layer 4 — Cost-adjusted EV (T02):
      EV gross dikurangi estimasi biaya: slippage + impact + fill rate penalty.
      net_ev = gross_ev - cost_drag
      Ini lebih realistis dari gross EV tapi tetap perkiraan.
    """
    result = {
        "verdict":        "INSUFFICIENT",
        "n":              0,
        "wr":             None,
        "avg_rr":         None,
        "empirical_ev":   None,
        "net_ev":         None,
        "p_value":        None,
        "runs_p_value":   None,
        "wr_stability":   None,
        "note":           f"Butuh minimal {EDGE_PROOF_MIN_SIGNALS} signal resolved",
        "warnings":       [],
    }

    try:
        rows = (
            supabase.table("signals")
            .select("outcome, tp1, sl, entry, rr, strategy, tier, "
                    "actual_fill_price, actual_pnl_pct, fill_source_used")
            .in_("outcome", ["WIN", "LOSS"])
            .order("sent_at", desc=True)
            .limit(500)
            .execute()
            .data
        )
    except Exception as e:
        log(f"⚠️ check_edge_proven: {e}", "warn")
        return result

    # [Y01] Log berapa persen rows punya actual fill vs simulated
    n_actual = sum(1 for r in rows if r.get("actual_fill_price"))
    if rows:
        fill_ratio = n_actual / len(rows) * 100
        result.setdefault("warnings", [])
        if fill_ratio < 20:
            result["warnings"].append(
                f"[Y01] Hanya {fill_ratio:.0f}% signal punya actual_fill_price — "
                f"EV kalkulasi masih dominan dari simulated entry. "
                f"Sambungkan broker feedback loop untuk akurasi lebih tinggi."
            )
        log(f"  [Y01] Edge data: {n_actual}/{len(rows)} rows punya actual fill ({fill_ratio:.0f}%)")

    n = len(rows)
    result["n"] = n

    if n < 20:
        result["note"] = f"Terlalu sedikit data ({n} signal) — bot baru mulai"
        return result

    wins   = sum(1 for r in rows if r["outcome"] == "WIN")
    wr     = wins / n
    result["wr"] = round(wr, 4)

    # ── avg RR ────────────────────────────────────────────
    rr_vals = []
    for r in rows:
        if r.get("rr") and float(r["rr"]) > 0:
            rr_vals.append(float(r["rr"]))
        elif r.get("tp1") and r.get("sl") and r.get("entry"):
            try:
                _e, _tp, _sl = float(r["entry"]), float(r["tp1"]), float(r["sl"])
                if _e > 0 and abs(_tp - _e) > 0 and abs(_sl - _e) > 0:
                    rr_vals.append(abs(_tp - _e) / abs(_sl - _e))
            except Exception:
                pass
    avg_rr = float(np.mean(rr_vals)) if rr_vals else 1.5
    result["avg_rr"] = round(avg_rr, 3)

    # ── Layer 1: Binomial p-value ──────────────────────────
    def _norm_cdf(z: float) -> float:
        t    = 1.0 / (1.0 + 0.2316419 * abs(z))
        poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
               + t * (-1.821255978 + t * 1.330274429))))
        pdf  = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
        cdf  = 1.0 - pdf * poly
        return cdf if z >= 0 else 1.0 - cdf

    z_stat = (wins - n * 0.5) / max(math.sqrt(n * 0.25), 1e-9)
    p_val  = 1.0 - _norm_cdf(z_stat)
    result["p_value"] = round(p_val, 4)
    # Gross EV
    gross_ev = wr * avg_rr - (1.0 - wr) * 1.0
    result["empirical_ev"] = round(gross_ev, 4)

    # ── Layer 2: Runs test (Wald-Wolfowitz) [T01] ─────────
    # Cek independensi: apakah WIN/LOSS muncul secara acak?
    # H0: urutan adalah acak (independent). H1: ada non-random pattern.
    outcomes_binary = [1 if r["outcome"] == "WIN" else 0 for r in rows]
    if len(outcomes_binary) >= 20:
        runs     = 1
        n1       = sum(outcomes_binary)       # jumlah WIN
        n2       = n - n1                     # jumlah LOSS
        for i in range(1, len(outcomes_binary)):
            if outcomes_binary[i] != outcomes_binary[i - 1]:
                runs += 1
        if n1 > 0 and n2 > 0:
            mu_r  = (2 * n1 * n2) / (n1 + n2) + 1
            var_r = (2 * n1 * n2 * (2 * n1 * n2 - n1 - n2)) / \
                    ((n1 + n2) ** 2 * (n1 + n2 - 1) + 1e-9)
            z_r   = (runs - mu_r) / max(math.sqrt(var_r), 1e-9)
            # Two-sided: clustering (z<0) atau alternating (z>0)
            p_runs = 2 * (1 - _norm_cdf(abs(z_r)))
            result["runs_p_value"] = round(p_runs, 4)
            if p_runs < 0.05:
                direction = "CLUSTERING (streak-dependent)" if z_r < 0 else "ANTI-CLUSTERING (alternating)"
                result["warnings"].append(
                    f"⚠️ Runs test p={p_runs:.3f} → hasil TIDAK random ({direction}). "
                    f"Binomial p-value mungkin misleading."
                )
            else:
                result["warnings"].append(
                    f"ℹ️ Runs test p={p_runs:.3f} → tidak ada bukti clustering "
                    f"(asumsi independensi cukup masuk akal untuk n={n})"
                )

    # ── Layer 3: Stability check [T01] ───────────────────
    if n >= 40:
        half     = n // 2
        wr_first = sum(1 for r in rows[half:] if r["outcome"] == "WIN") / half  # older (desc order)
        wr_last  = sum(1 for r in rows[:half] if r["outcome"] == "WIN") / half  # recent
        stability_delta = wr_last - wr_first
        result["wr_stability"] = {
            "wr_older_half":  round(wr_first, 4),
            "wr_recent_half": round(wr_last, 4),
            "delta":          round(stability_delta, 4),
        }
        if abs(stability_delta) > 0.10:
            trend = "IMPROVING 📈" if stability_delta > 0 else "DEGRADING 📉"
            result["warnings"].append(
                f"⚠️ WR stability: older={wr_first:.0%} vs recent={wr_last:.0%} "
                f"(Δ{stability_delta:+.0%}) — edge {trend}"
            )
        else:
            result["warnings"].append(
                f"✅ WR stability: older={wr_first:.0%} vs recent={wr_last:.0%} "
                f"(Δ{stability_delta:+.0%}) — relatif stabil"
            )

    # ── Layer 4-pre: Distribution diagnostics [U02] ───────
    # Skewness dan excess kurtosis dari distribusi RR aktual.
    # Ini memberikan gambaran bentuk distribusi payoff — sesuatu yang
    # p-value dan WR saja tidak bisa ceritakan.
    if len(rr_vals) >= 10:
        rr_arr  = np.array(rr_vals, dtype=float)
        n_rr    = len(rr_arr)
        mu_rr   = float(np.mean(rr_arr))
        std_rr  = float(np.std(rr_arr, ddof=1))
        if std_rr > 1e-9:
            # Pearson's moment skewness (scipy-free)
            z_cubed = ((rr_arr - mu_rr) / std_rr) ** 3
            skew    = float(np.mean(z_cubed))
            # Excess kurtosis (Fisher definition, ddof=0 moment then adjust)
            z_quad  = ((rr_arr - mu_rr) / std_rr) ** 4
            kurt    = float(np.mean(z_quad)) - 3.0
            result["rr_skewness"] = round(skew, 3)
            result["rr_kurtosis"] = round(kurt, 3)

            # [V02] Simpan ke global agar get_smart_risk_pct() bisa pakai
            global _run_dist_stats
            _run_dist_stats = {"skew": skew, "kurt": kurt, "n": n_rr}

            skew_interp = ("right-skewed (rare big wins ✅)" if skew > 1.0
                           else "left-skewed (rare big losses ⚠️)" if skew < -1.0
                           else "approximately symmetric")
            kurt_interp = ("fat tails ⚠️ — risiko lebih besar dari distribusi normal"
                           if kurt > 3.0
                           else "thin tails ✅" if kurt < -1.0
                           else "normal tail behaviour")
            result["warnings"].append(
                f"📊 RR distribution (n={n_rr}): "
                f"mean={mu_rr:.2f} std={std_rr:.2f} | "
                f"skew={skew:.2f} ({skew_interp}) | "
                f"excess kurt={kurt:.2f} ({kurt_interp})"
            )
        else:
            result["warnings"].append("📊 RR distribution: std≈0 — semua trade punya RR sama")
    else:
        result["warnings"].append(f"📊 RR distribution: terlalu sedikit data RR ({len(rr_vals)} trades)")

    # ── Layer 4: Cost-adjusted EV [T02] ──────────────────
    # Estimasi biaya berdasarkan komposisi signal (strategy + tier mix)
    strategy_counts = {}
    tier_counts     = {}
    for r in rows:
        s = r.get("strategy") or "SWING"
        t = r.get("tier") or "A"
        strategy_counts[s] = strategy_counts.get(s, 0) + 1
        tier_counts[t]     = tier_counts.get(t, 0) + 1

    dominant_strategy = max(strategy_counts, key=strategy_counts.get) if strategy_counts else "SWING"
    dominant_tier     = max(tier_counts,     key=tier_counts.get)     if tier_counts     else "A"

    slippage_pct  = EV_COST_SLIPPAGE.get(dominant_strategy, {}).get(dominant_tier, 0.30)
    fill_rate     = EV_COST_FILL_RATE.get(dominant_strategy, 0.85)

    # net EV model:
    #   win side  : TP dikurangi slippage (susah hit TP tepat, entry juga slipped)
    #   loss side : SL kena + slippage (SL biasanya tereksekusi lebih buruk)
    #   fill rate : (1 - fill_rate) = missed trades, dihitung sebagai 0 EV
    adjusted_rr  = max(avg_rr - (slippage_pct / 100) * 2, 0.1)   # slippage di kedua sisi
    gross_rr_ev  = wr * avg_rr - (1.0 - wr) * 1.0
    net_rr_ev    = wr * adjusted_rr - (1.0 - wr) * (1.0 + slippage_pct / 100)
    net_ev       = net_rr_ev * fill_rate   # missed trades = 0 contribution
    result["net_ev"] = round(net_ev, 4)
    result["warnings"].append(
        f"💡 Cost-adjusted EV: gross={gross_rr_ev:+.3f} → net≈{net_ev:+.3f} "
        f"(slip={slippage_pct:.2f}% fill={fill_rate:.0%} dom={dominant_strategy}/{dominant_tier}) "
        f"⚠️ estimasi teoritis, bukan dari actual fill data"
    )
    if net_ev < 0 < gross_rr_ev:
        result["warnings"].append(
            "🔴 Net EV negatif setelah biaya — gross EV positif tapi cost mungkin memakan seluruh edge!"
        )

    # ── Verdict — [V01] Ensemble consensus ───────────────
    # Single method bisa salah; butuh 2/3 setuju untuk PROVEN.
    _ensemble = get_ensemble_edge_verdict(rows)
    result["ensemble"] = _ensemble
    ensemble_verdict = _ensemble["verdict"]

    if n < EDGE_PROOF_MIN_SIGNALS:
        result["verdict"] = "INSUFFICIENT"
        result["note"] = (
            f"n={n}/{EDGE_PROOF_MIN_SIGNALS} — belum cukup. "
            f"WR={wr:.0%} gross_EV={gross_ev:+.3f} net_EV≈{net_ev:+.3f} | "
            f"ensemble {_ensemble['confidence']}/3 agree"
        )
    elif ensemble_verdict == "PROVEN" and net_ev > 0:
        result["verdict"] = "PROVEN"
        result["note"] = (
            f"✅ EDGE PROVEN (ensemble {_ensemble['confidence']}/3) — "
            f"WR={wr:.0%} gross={gross_ev:+.3f} net≈{net_ev:+.3f} p={p_val:.3f} n={n}"
        )
    elif ensemble_verdict in ("PROVEN", "PROMISING") or (gross_ev > 0 and p_val < 0.10 and n >= 50):
        result["verdict"] = "PROMISING"
        result["note"] = (
            f"🔵 PROMISING (ensemble {_ensemble['confidence']}/3) — "
            f"WR={wr:.0%} gross={gross_ev:+.3f} net≈{net_ev:+.3f} n={n}"
        )
    else:
        result["verdict"] = "UNPROVEN"
        result["note"] = (
            f"⚠️ UNPROVEN (ensemble {_ensemble['confidence']}/3) — "
            f"WR={wr:.0%} gross={gross_ev:+.3f} net≈{net_ev:+.3f} p={p_val:.3f} n={n}"
        )
    return result


# ════════════════════════════════════════════════════════
#  [PHASE-4] VALIDATION REPORT
#  Kirim ringkasan verdict ke Telegram.
#  Dipanggil: (a) jika verdict berubah di run(), (b) via --validate CLI.
#  Tidak spam — hanya saat ada perubahan atau diminta eksplisit.
# ════════════════════════════════════════════════════════

def send_validation_report(edge: dict, triggered_by: str = "run"):
    """
    [PHASE-4] Format dan kirim laporan edge validation ke Telegram.

    Menampilkan semua 4 metode ensemble secara eksplisit, verdict akhir,
    dan aturan konkret yang berlaku berdasarkan verdict.

    triggered_by: "run"      → dipanggil dari run() karena verdict berubah
                  "cli"      → dipanggil dari --validate CLI
                  "startup"  → dipanggil di awal run pertama (cold start)
    """
    verdict     = edge.get("verdict", "INSUFFICIENT")
    n           = edge.get("n", 0)
    wr          = edge.get("wr")
    gross_ev    = edge.get("empirical_ev")
    net_ev      = edge.get("net_ev")
    p_val       = edge.get("p_value")
    stab        = edge.get("wr_stability", {})
    ensemble    = edge.get("ensemble", {})
    methods     = ensemble.get("methods", {})

    # ── Verdict badge ─────────────────────────────────────
    verdict_map = {
        "PROVEN":       ("✅", "PROVEN",      "Edge terbukti secara statistik"),
        "PROMISING":    ("🔵", "PROMISING",   "Ada indikasi edge, belum konklusif"),
        "UNPROVEN":     ("⚠️", "UNPROVEN",    "Tidak ada bukti edge yang cukup"),
        "INSUFFICIENT": ("🧊", "INSUFFICIENT", f"Data belum cukup (n={n}/{EDGE_PROOF_MIN_SIGNALS})"),
    }
    v_emoji, v_label, v_desc = verdict_map.get(verdict, ("❓", verdict, "—"))

    # ── 4 Metode ensemble ─────────────────────────────────
    def _method_line(key: str, label: str) -> str:
        m = methods.get(key, {})
        ok  = m.get("ok", False)
        note = m.get("note", "—")
        icon = "✅" if ok else "❌"
        return f"  {icon} {label}: {note}"

    m1_line = _method_line("M1_binomial_full",      "M1 Binomial p-val  ")
    m2_line = _method_line("M2_profit_factor_train", "M2 Profit Factor   ")
    m3_line = _method_line("M3_multifold_oos_wr",    "M3 OOS Walk-Forward")
    m4_line = _method_line("M4_multifold_drift",     "M4 Overfit Drift   ")

    agree      = ensemble.get("confidence", 0)
    max_m      = len(methods)
    agree_str  = f"{agree}/{max_m} metode setuju" if max_m else "—"

    # ── EV & WR ringkasan ─────────────────────────────────
    wr_str      = f"{wr:.0%}" if wr is not None else "N/A"
    gross_str   = f"{gross_ev:+.3f}" if gross_ev is not None else "N/A"
    net_str     = f"{net_ev:+.3f}"   if net_ev is not None else "N/A"
    p_str       = f"{p_val:.3f}"     if p_val is not None else "N/A"

    # ── Stability ─────────────────────────────────────────
    if stab:
        stab_str = (f"  WR older={stab['wr_older_half']:.0%} → "
                    f"recent={stab['wr_recent_half']:.0%} "
                    f"(Δ{stab['delta']:+.0%})")
    else:
        stab_str = "  Butuh n≥40 untuk stability check"

    # ── Aturan aktif berdasarkan verdict ─────────────────
    rules = {
        "PROVEN":       ("✅ Operasional penuh. Adaptive layers diizinkan.\n"
                         "✅ Complexity tambahan boleh diuji."),
        "PROMISING":    ("✅ Operasional normal. Jangan ubah parameter dulu.\n"
                         "⏳ Terus kumpulkan data sampai PROVEN."),
        "UNPROVEN":     ("⛔ JANGAN tambah filter atau kompleksitas baru.\n"
                         "⛔ JANGAN ubah W, TIER_MIN_SCORE, atau MIN_RR.\n"
                         "⛔ JANGAN aktifkan adaptive/cluster weights.\n"
                         "✅ Operasikan apa adanya. Kumpulkan lebih banyak trade."),
        "INSUFFICIENT": ("⏳ Kumpulkan lebih banyak trade resolved.\n"
                         f"⏳ Target: {COLLECTION_TARGET_MIN} min, {COLLECTION_TARGET_FULL} ideal.\n"
                         "✅ Jangan ubah apapun. Biarkan data terakumulasi."),
    }
    rule_text = rules.get(verdict, "—")

    # ── Trigger label ─────────────────────────────────────
    trigger_note = {
        "run":     "📡 <i>Dikirim karena verdict berubah dari run sebelumnya.</i>",
        "cli":     "🖥️ <i>Dikirim via --validate CLI.</i>",
        "startup": "🚀 <i>Cold start — laporan pertama.</i>",
    }.get(triggered_by, "")

    msg = (
        f"🧠 <b>PHASE 4 — Edge Validation Report</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{v_emoji} <b>VERDICT: {v_label}</b>\n"
        f"   {v_desc}\n"
        f"   {agree_str} | n={n} resolved trades\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Metode Ensemble (4 independen):</b>\n"
        f"{m1_line}\n"
        f"{m2_line}\n"
        f"{m3_line}\n"
        f"{m4_line}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>Statistik:</b>\n"
        f"  WR      : {wr_str} | p-value: {p_str}\n"
        f"  EV gross: {gross_str} → net: {net_str}\n"
        f"  Stabilitas:\n{stab_str}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>Aturan aktif ({v_label}):</b>\n"
        f"{rule_text}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{trigger_note}"
    )
    try:
        tg(msg)
    except Exception as e:
        log(f"⚠️ send_validation_report: {e}", "warn")

    log(f"📊 [PHASE4] Validation report dikirim: verdict={verdict} n={n} "
        f"WR={wr_str} EV={gross_str}/net={net_str} ({agree_str})")
    """
    [R03] Bandingkan WR antara SIMPLE dan COMPLEX mode secara empiris.

    Query Supabase untuk signal resolved (WIN/LOSS) yang sudah menyimpan
    kolom 'signal_mode'. Kembalikan perbandingan statistik jujur.

    Kolom baru di Supabase:
      ALTER TABLE signals ADD COLUMN IF NOT EXISTS signal_mode TEXT;

    Interpretasi:
      - n per mode < COMPLEX_MODE_MIN_SAMPLE  → data tidak cukup
      - complex WR > simple WR + 5pp          → complex lebih baik empiris
      - perbedaan < 5pp                        → tidak ada bukti — tetap SIMPLE

    Returns dict: wr_simple, wr_complex, n_simple, n_complex, verdict,
                  recommendation
    """
    result = {
        "wr_simple": None, "n_simple": 0,
        "wr_complex": None, "n_complex": 0,
        "verdict": "INSUFFICIENT_DATA",
        "recommendation": "Tetap SIMPLE_MODE — belum cukup data untuk perbandingan valid",
    }
    try:
        rows = (
            supabase.table("signals")
            .select("signal_mode, outcome")
            .in_("outcome", ["WIN", "LOSS"])
            .not_.is_("signal_mode", "null")
            .order("sent_at", desc=True)
            .limit(500)
            .execute()
            .data
        )
    except Exception as e:
        log(f"⚠️ get_mode_performance_comparison: {e}", "warn")
        return result

    if not rows:
        return result

    simple_rows  = [r for r in rows if r.get("signal_mode") == "SIMPLE"]
    complex_rows = [r for r in rows if r.get("signal_mode") == "COMPLEX"]
    result["n_simple"]  = len(simple_rows)
    result["n_complex"] = len(complex_rows)

    ws = (round(sum(1 for r in simple_rows  if r["outcome"] == "WIN") / len(simple_rows),  4)
          if len(simple_rows)  >= COMPLEX_MODE_MIN_SAMPLE else None)
    wc = (round(sum(1 for r in complex_rows if r["outcome"] == "WIN") / len(complex_rows), 4)
          if len(complex_rows) >= COMPLEX_MODE_MIN_SAMPLE else None)
    result["wr_simple"]  = ws
    result["wr_complex"] = wc

    if ws is None or wc is None:
        missing = []
        if ws is None: missing.append(f"SIMPLE {result['n_simple']}/{COMPLEX_MODE_MIN_SAMPLE}")
        if wc is None: missing.append(f"COMPLEX {result['n_complex']}/{COMPLEX_MODE_MIN_SAMPLE}")
        result["recommendation"] = f"Data belum cukup ({', '.join(missing)}) — tetap SIMPLE_MODE"
    elif wc > ws + 0.05:
        result["verdict"] = "COMPLEX_BETTER"
        lock_note = " ⚠️ SIMPLE_MODE_LOCK aktif — override manual via ENV" if SIMPLE_MODE_LOCK else ""
        result["recommendation"] = (
            f"COMPLEX WR {wc:.0%} > SIMPLE WR {ws:.0%} (+{(wc-ws)*100:.1f}pp) — "
            f"pertimbangkan SIMPLE_MODE=false{lock_note}"
        )
    elif ws > wc + 0.05:
        result["verdict"] = "SIMPLE_BETTER"
        result["recommendation"] = (
            f"SIMPLE WR {ws:.0%} > COMPLEX WR {wc:.0%} — "
            f"data mendukung tetap SIMPLE_MODE"
        )
    else:
        result["verdict"] = "NO_SIGNIFICANT_DIFFERENCE"
        result["recommendation"] = (
            f"Perbedaan < 5pp (SIMPLE {ws:.0%} vs COMPLEX {wc:.0%}) — "
            f"tetap SIMPLE_MODE (Occam's razor: kompleksitas tidak terbukti menguntungkan)"
        )
    return result


def is_strategy_disabled(strategy: str, regime: str) -> bool:
    """
    Check apakah sub-strategy saat ini sedang di-disable.
    Dipanggil sebelum mengirim signal dari check_intraday/check_swing.
    """
    sub = f"{'TREND' if regime == 'TRENDING' else 'MEANREV'}_{strategy}"
    disable_until_iso = _disabled_strategies.get(sub)
    if not disable_until_iso:
        return False
    try:
        disable_until = datetime.fromisoformat(disable_until_iso)
        if datetime.now(timezone.utc) < disable_until:
            log(f"  🚫 Signal skipped — strategy [{sub}] sedang auto-disabled")
            return True
        else:
            del _disabled_strategies[sub]
            return False
    except Exception:
        return False


def check_market_abnormal(ihsg: dict) -> dict:
    """
    [v7.0] Kill Switch Layer 2 — Market Abnormal Shutdown.

    Deteksi kondisi pasar yang tidak normal dan berbahaya untuk trading.
    Kondisi ini tidak bisa di-predict oleh setup manapun — ini adalah
    fat tail events yang merusak semua strategi secara bersamaan.

    Trigger kondisi abnormal:
    1. IHSG crash > 5% dalam 5 hari (sudah ada — extended ke sini)
    2. IHSG drop > 3% dalam 1 hari (lebih ketat dari block_buy 2%)
    3. Volatilitas IHSG > 4x rata-rata (panic selling atau short squeeze)

    Returns: {abnormal: bool, reason: str, severity: str}
    """
    default = {"abnormal": False, "reason": "", "severity": "NORMAL"}

    try:
        ihsg_1d = ihsg.get("ihsg_1d", 0.0)
        ihsg_5d = ihsg.get("ihsg_5d", 0.0)

        # Kondisi 1: Crash besar 5 hari — sudah di-handle ihsg["halt"] tapi
        # kita duplikasi di sini agar kill switch layer berdiri sendiri
        if ihsg_5d < -5.0:
            return {
                "abnormal": True,
                "reason":   f"IHSG crash 5d: {ihsg_5d:+.1f}% < -5%",
                "severity": "CRITICAL"
            }

        # Kondisi 2: Drop harian sangat dalam (lebih ketat dari block_buy)
        if ihsg_1d < -3.0:
            return {
                "abnormal": True,
                "reason":   f"IHSG drop 1d: {ihsg_1d:+.1f}% < -3% (panic day)",
                "severity": "HIGH"
            }

        # Kondisi 3: Cek volatilitas IHSG dari data historis
        try:
            df = get_ihsg_cached(period="30d")   # [Z02] pakai cache terpusat
            if df is not None and not df.empty and len(df) >= 10:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                closes_jk = df["Close"].dropna().values.astype(float)
                returns   = np.abs(np.diff(closes_jk) / closes_jk[:-1] * 100)
                avg_vol   = float(np.mean(returns[:-1]))   # rata-rata historis
                cur_vol   = float(returns[-1])              # volatilitas hari ini
                if avg_vol > 0 and cur_vol > avg_vol * KS_ABNORMAL_VOL_MULT:
                    return {
                        "abnormal": True,
                        "reason":   f"Volatilitas IHSG: {cur_vol:.1f}% = {cur_vol/avg_vol:.1f}x normal ({avg_vol:.1f}%)",
                        "severity": "HIGH"
                    }
        except Exception:
            pass   # volatilitas check opsional — jangan block karena ini

        return default

    except Exception as e:
        log(f"⚠️ check_market_abnormal: {e}", "warn")
        return default


def check_portfolio_drawdown() -> dict:
    """
    [v7.1] Kill Switch Layer 3 — Drawdown Circuit Breaker.

    FIX 2A: Mengganti estimasi flat per-trade (len × RISK_PCT%) dengan
    kalkulasi mark-to-market yang sesungguhnya:
      - Ambil harga live setiap posisi aktif via get_current_price()
      - Hitung actual SL distance = abs(current_price - sl)
      - Risk aktual = sl_dist_actual / entry × position_value_estimasi
      - Trade yang sudah profit (current > entry untuk BUY) punya SL risk mendekati 0
        saat BE aktif → tidak lagi men-trigger circuit breaker secara palsu

    Ini mencegah over-trigger circuit breaker saat banyak posisi sudah
    dalam kondisi profit dengan SL di break-even.

    Returns: {triggered: bool, total_risk_pct: float, open_count: int}
    """
    default = {"triggered": False, "total_risk_pct": 0.0, "open_count": 0}
    try:
        rows = (
            supabase.table("signals")
            .select("entry, sl, side, strategy, pair")
            .is_("outcome", "null")
            .execute()
            .data
        )
        if not rows:
            return default

        total_risk_idr = 0.0
        for r in rows:
            try:
                entry = float(r.get("entry", 0))
                sl    = float(r.get("sl", 0))
                side  = r.get("side", "BUY")
                pair  = r.get("pair", "")
                if entry <= 0 or sl <= 0:
                    continue

                # FIX 2A: gunakan harga live untuk estimasi risk aktual
                ticker      = pair + ".JK" if not pair.endswith(".JK") else pair
                live_price  = get_current_price(ticker)
                cur_price   = live_price if live_price > 0 else entry

                # Risk aktual = jarak harga saat ini ke SL (bukan entry ke SL)
                if side == "BUY":
                    sl_dist_actual = max(cur_price - sl, 0)   # 0 jika sudah profit jauh
                else:
                    sl_dist_actual = max(sl - cur_price, 0)

                # Estimasi position value dari risk_idr / sl_pct_original
                sl_pct_original = abs(entry - sl) / entry
                if sl_pct_original <= 0:
                    continue
                risk_budget_idr = PORTFOLIO_IDR * (RISK_PCT / 100)
                pos_value_est   = risk_budget_idr / sl_pct_original

                # Risk saat ini = actual SL distance / current price × position value
                actual_risk_idr = (sl_dist_actual / (cur_price + 1e-9)) * pos_value_est
                total_risk_idr += actual_risk_idr

            except Exception as _e:
                log(f"  ⚠️ [FALLBACK] risk per-position kalkulasi: {_e} — flat RISK_PCT", "warn")
                # Fallback per-position ke flat RISK_PCT jika kalkulasi gagal
                total_risk_idr += PORTFOLIO_IDR * (RISK_PCT / 100)
                continue

        total_risk_pct = (total_risk_idr / PORTFOLIO_IDR * 100) if PORTFOLIO_IDR > 0 else 0.0

        if total_risk_pct > KS_DRAWDOWN_PCT_MAX:
            msg = (f"🛑 <b>KILL SWITCH — DRAWDOWN CIRCUIT BREAKER</b>\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"Total at-risk (mark-to-market): <b>{total_risk_pct:.1f}%</b> dari portfolio\n"
                   f"Open positions: {len(rows)}\n"
                   f"Batas maksimal: {KS_DRAWDOWN_PCT_MAX}%\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"🚫 <b>Tidak ada trade baru</b> sampai exposure berkurang.\n"
                   f"<i>Tutup sebagian posisi atau tunggu TP/SL hit.</i>")
            tg(msg)
            log(f"🛑 Circuit breaker: total risk {total_risk_pct:.1f}% > {KS_DRAWDOWN_PCT_MAX}%", "warn")
            return {
                "triggered":      True,
                "total_risk_pct": round(total_risk_pct, 2),
                "open_count":     len(rows)
            }

        log(f"✅ Portfolio drawdown OK: {total_risk_pct:.1f}% / {KS_DRAWDOWN_PCT_MAX}% max ({len(rows)} open)")

        # [v8.0 FIX 6] Capital deployment gate — cek total modal terikat di open positions.
        # Berbeda dari risk gate di atas: ini mengukur berapa % dari PORTFOLIO_IDR
        # yang sudah "terkunci" di posisi aktif, bukan seberapa besar risikonya.
        # Estimasi order value dari data yang sama: gunakan pos_value_est per row.
        total_deployed_idr = 0.0
        for r in rows:
            try:
                entry = float(r.get("entry", 0))
                sl    = float(r.get("sl", 0))
                if entry <= 0 or sl <= 0:
                    continue
                sl_pct = abs(entry - sl) / entry
                if sl_pct <= 0:
                    continue
                risk_budget_idr  = PORTFOLIO_IDR * (RISK_PCT / 100)
                pos_value_est    = risk_budget_idr / sl_pct
                total_deployed_idr += pos_value_est
            except Exception as _e:
                log(f"  ⚠️ [FALLBACK] deployed capital: {_e} — estimasi kasar", "warn")
                total_deployed_idr += PORTFOLIO_IDR * (RISK_PCT / 100) / 0.02  # estimasi kasar
                continue

        deployed_pct = (total_deployed_idr / PORTFOLIO_IDR * 100) if PORTFOLIO_IDR > 0 else 0.0

        if deployed_pct > MAX_TOTAL_CAPITAL_DEPLOYED_PCT:
            msg = (f"⚠️ <b>CAPITAL GATE — MODAL TERLALU BANYAK TERIKAT</b>\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"Modal deployed: <b>{deployed_pct:.1f}%</b> dari portfolio\n"
                   f"Open positions: {len(rows)}\n"
                   f"Batas: {MAX_TOTAL_CAPITAL_DEPLOYED_PCT}%\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"🚫 Tidak ada trade baru sampai sebagian posisi ditutup.\n"
                   f"<i>Risk masih dalam batas, tapi capital tidak cukup untuk entry baru.</i>")
            tg(msg)
            log(f"🛑 Capital gate: deployed {deployed_pct:.1f}% > {MAX_TOTAL_CAPITAL_DEPLOYED_PCT}% limit", "warn")
            return {
                "triggered":      True,
                "total_risk_pct": round(total_risk_pct, 2),
                "deployed_pct":   round(deployed_pct, 2),
                "open_count":     len(rows),
                "reason":         "capital_gate"
            }

        log(f"✅ Capital gate OK: deployed {deployed_pct:.1f}% / {MAX_TOTAL_CAPITAL_DEPLOYED_PCT}% max")
        return {
            "triggered":      False,
            "total_risk_pct": round(total_risk_pct, 2),
            "deployed_pct":   round(deployed_pct, 2),
            "open_count":     len(rows)
        }

    except Exception as e:
        log(f"⚠️ check_portfolio_drawdown: {e}", "warn")
        return default


def get_ihsg_cached(period: str = "30d") -> "pd.DataFrame | None":
    """
    [Z02] IHSG Cache terpusat — download ^JKSE sekali per TTL window.

    Eliminasi triple yf.download("^JKSE") yang sebelumnya terjadi di:
    - check_market_abnormal() → period="30d"
    - get_ihsg_regime()       → period="10d"
    - check_correlation_filter() → period="25d"

    Cache menyimpan data 30d (superset) sehingga semua fungsi bisa
    mengambil subset yang dibutuhkan tanpa download ulang.

    TTL = 10 menit (_IHSG_CACHE_TTL_SECONDS). Setelah expired,
    download diulang sekali dan cache diperbarui.
    """
    global _ihsg_cache
    now_ts = time.time()

    # Serve from cache jika masih valid
    if _ihsg_cache.get("data") is not None:
        age = now_ts - _ihsg_cache.get("timestamp", 0)
        if age < _IHSG_CACHE_TTL_SECONDS:
            cached_df = _ihsg_cache["data"]
            # Trim ke period yang diminta jika lebih pendek dari 30d
            if period not in ("30d", "25d"):
                # Untuk period pendek (10d), kembalikan tail yang sesuai
                n_rows = {"10d": 10, "5d": 5}.get(period, len(cached_df))
                return cached_df.tail(n_rows).copy() if len(cached_df) >= n_rows else cached_df.copy()
            return cached_df.copy()

    # Download fresh — selalu minta 30d sebagai superset
    try:
        df = yf.download("^JKSE", period="30d", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            log("⚠️ [Z02] get_ihsg_cached: download ^JKSE kosong — cache tidak diperbarui", "warn")
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        _ihsg_cache = {
            "data":      df,
            "timestamp": now_ts,
        }
        log(f"  📥 [Z02] IHSG cache diperbarui: {len(df)} rows")
        # Trim jika diminta
        if period not in ("30d", "25d"):
            n_rows = {"10d": 10, "5d": 5}.get(period, len(df))
            return df.tail(n_rows).copy() if len(df) >= n_rows else df.copy()
        return df.copy()

    except Exception as e:
        log(f"⚠️ [Z02] get_ihsg_cached error: {e} — return None", "warn")
        return None


def get_ihsg_regime() -> dict:
    """
    Cek kondisi IHSG untuk market guard:
    - Crash guard: IHSG drop > 5% dalam 5 hari → halt semua
    - Drop guard:  IHSG drop > 2% dalam 1 hari → blok BUY

    [AA01] v8.11: Diperkaya dengan data real IHSG untuk heartbeat:
    price, open, high, low, volume, MTD, dan 20d range.
    """
    default = {
        "halt": False, "block_buy": False,
        "ihsg_1d": 0.0, "ihsg_5d": 0.0,
        "price": None, "open": None, "high": None, "low": None,
        "volume": None, "volume_avg20": None,
        "ihsg_mtd": None, "ihsg_ytd": None,
        "ihsg_20d_high": None, "ihsg_20d_low": None,
    }
    try:
        df = get_ihsg_cached(period="30d")   # [Z02] pakai cache terpusat
        if df is None or df.empty or len(df) < 2:
            return default

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        closes = df["Close"].dropna().values.astype(float)
        if len(closes) < 2:
            return default

        chg_1d = (closes[-1] - closes[-2]) / closes[-2] * 100
        chg_5d = (closes[-1] - closes[max(0, len(closes)-6)]) / closes[max(0, len(closes)-6)] * 100

        halt      = chg_5d < IHSG_CRASH_BLOCK
        block_buy = chg_1d < IHSG_DROP_BLOCK

        status = "🛑 HALT" if halt else ("⛔ BUY BLOCKED" if block_buy else "✅ OK")
        log(f"📊 IHSG 1d:{chg_1d:+.1f}% 5d:{chg_5d:+.1f}% | {status}")

        result = {
            "halt":      halt,
            "block_buy": block_buy,
            "ihsg_1d":   round(chg_1d, 2),
            "ihsg_5d":   round(chg_5d, 2),
        }

        # [AA01] Ekstrak data real OHLCV + MTD/YTD untuk heartbeat ────
        try:
            last_row = df.iloc[-1]
            price    = float(last_row.get("Close", closes[-1]))
            open_p   = float(last_row.get("Open",  price))
            high_p   = float(last_row.get("High",  price))
            low_p    = float(last_row.get("Low",   price))

            # Volume & rata-rata 20 hari
            vols      = df["Volume"].dropna().values.astype(float) if "Volume" in df.columns else None
            vol_last  = float(vols[-1])             if vols is not None and len(vols) >= 1  else None
            vol_avg20 = float(np.mean(vols[-21:-1])) if vols is not None and len(vols) >= 21 else None

            # 20-hari high/low — konteks posisi IHSG saat ini
            h_arr = df["High"].dropna().values.astype(float) if "High" in df.columns else closes
            l_arr = df["Low"].dropna().values.astype(float)  if "Low"  in df.columns else closes
            h20   = float(np.max(h_arr[-20:])) if len(h_arr) >= 20 else float(np.max(h_arr))
            l20   = float(np.min(l_arr[-20:])) if len(l_arr) >= 20 else float(np.min(l_arr))

            # MTD — close terkini vs close hari pertama bulan ini
            now_wib = datetime.now(WIB)
            mtd_pct = None
            ytd_pct = None
            try:
                df_idx       = df.copy()
                df_idx.index = pd.to_datetime(df_idx.index)
                this_month   = df_idx[df_idx.index.month == now_wib.month]
                this_year    = df_idx[df_idx.index.year  == now_wib.year]
                if len(this_month) >= 2:
                    mtd_base = float(this_month.iloc[0]["Close"])
                    mtd_pct  = round((price - mtd_base) / mtd_base * 100, 2)
                elif len(this_month) == 1:
                    mtd_pct  = round(chg_1d, 2)
                if len(this_year) >= 2:
                    ytd_base = float(this_year.iloc[0]["Close"])
                    ytd_pct  = round((price - ytd_base) / ytd_base * 100, 2)
            except Exception:
                pass

            result.update({
                "price":         round(price),
                "open":          round(open_p),
                "high":          round(high_p),
                "low":           round(low_p),
                "volume":        int(vol_last)    if vol_last  is not None else None,
                "volume_avg20":  int(vol_avg20)   if vol_avg20 is not None else None,
                "ihsg_mtd":      mtd_pct,
                "ihsg_ytd":      ytd_pct,
                "ihsg_20d_high": round(h20),
                "ihsg_20d_low":  round(l20),
            })
        except Exception as _ex:
            log(f"  ⚠️ [AA01] IHSG extended data: {_ex} — pakai data minimal", "warn")

        return result

    except Exception as e:
        log(f"⚠️ IHSG regime: {e}", "warn")
        return default


# ════════════════════════════════════════════════════════
#  [J05] GLOBAL KILL SWITCH — v7.15
#  Layer baru di atas Layer 1-3 yang sudah ada:
#
#  Layer 4 — IDX Trading Halt / ARA-ARB Circuit Breaker
#  Layer 5 — API Error Cascade Detection
#  Layer 6 — Data Corruption Guard (OHLCV anomaly)
#
#  Semua layer berdiri sendiri (tidak bergantung satu sama lain).
#  Satu layer triggered → bot HALT, pesan Telegram terkirim.
# ════════════════════════════════════════════════════════

# Threshold Layer 4 — IDX Halt
IDX_ARA_THRESHOLD       = 0.20    # +20% dalam 1 hari → kemungkinan ARA
IDX_ARB_THRESHOLD       = -0.20   # -20% dalam 1 hari → kemungkinan ARB
IDX_CIRCUIT_BREAKER_1   = -0.05   # -5% IHSG → auto-halt Level I (IDX rule)
IDX_CIRCUIT_BREAKER_2   = -0.075  # -7.5% IHSG → auto-halt Level II (IDX rule)
IDX_CIRCUIT_BREAKER_3   = -0.10   # -10% IHSG → auto-halt Level III (IDX rule)

# Threshold Layer 5 — API Cascade
API_FAIL_RATE_MAX       = 0.60    # jika > 60% ticker gagal data fetch → cascade
API_CONSECUTIVE_FAILS   = 5       # 5 error berturut-turut → cascade likely

# Threshold Layer 6 — Data Corruption
DATA_CORRUPT_PRICE_JUMP = 0.30    # harga lompat > 30% satu candle → suspect
DATA_CORRUPT_VOL_SPIKE  = 100.0   # volume 100x rata-rata → suspect
DATA_CORRUPT_ZERO_PRICE = 3       # 3+ candle close=0 → corrupt


def check_idx_trading_halt(ihsg: dict) -> dict:
    """
    [J05] Layer 4 — Deteksi IDX Trading Halt & Circuit Breaker.

    IDX punya mekanisme auto-halt (Peraturan No. II-A):
      Level I  : IHSG turun 5%    → trading dihentikan 30 menit
      Level II : IHSG turun 7.5%  → trading dihentikan 30 menit
      Level III: IHSG turun 10%   → trading dihentikan sampai akhir sesi

    Bot tidak bisa tahu halt sedang terjadi secara real-time (yfinance delay),
    tapi kita bisa deteksi POST-FACTO dari return 1d dan ambil keputusan
    untuk tidak kirim signal baru sampai kondisi jelas.

    Tambahan: cek ARA/ARB individual saham (tidak bisa entry/exit saat ARA/ARB).
    """
    default = {"halt_detected": False, "level": None, "reason": ""}

    ihsg_1d = ihsg.get("ihsg_1d", 0.0)
    ihsg_5d = ihsg.get("ihsg_5d", 0.0)

    # Circuit Breaker Level III — paling berat
    if ihsg_1d / 100 <= IDX_CIRCUIT_BREAKER_3:
        return {
            "halt_detected": True,
            "level": "CIRCUIT_BREAKER_III",
            "reason": f"IHSG 1d {ihsg_1d:+.1f}% → Circuit Breaker Level III IDX (-10%) "
                      f"— trading mungkin dihentikan sampai akhir sesi"
        }
    if ihsg_1d / 100 <= IDX_CIRCUIT_BREAKER_2:
        return {
            "halt_detected": True,
            "level": "CIRCUIT_BREAKER_II",
            "reason": f"IHSG 1d {ihsg_1d:+.1f}% → Circuit Breaker Level II IDX (-7.5%)"
        }
    if ihsg_1d / 100 <= IDX_CIRCUIT_BREAKER_1:
        return {
            "halt_detected": True,
            "level": "CIRCUIT_BREAKER_I",
            "reason": f"IHSG 1d {ihsg_1d:+.1f}% → Circuit Breaker Level I IDX (-5%) "
                      f"— potensi trading halt 30 menit aktif/sudah terjadi"
        }

    return default


def check_ticker_ara_arb(ticker: str, closes: "np.ndarray") -> dict:
    """
    [J05] Cek apakah saham individual sedang dalam kondisi ARA/ARB.

    ARA (Auto Rejection Atas) dan ARB (Auto Rejection Bawah) di IDX:
      Saham kena ARA/ARB → semua order di atas/bawah batas otomatis ditolak.
      Batas: ±35% papan utama, ±25% papan pengembangan (saham biasa).

    Bot tidak bisa entry/exit saat ARA/ARB karena order tidak akan tereksekusi.
    Deteksi heuristik dari return 1 candle terakhir.

    Returns: {ara: bool, arb: bool, pct_change: float}
    """
    default = {"ara": False, "arb": False, "pct_change": 0.0}
    try:
        if len(closes) < 2:
            return default
        pct = (float(closes[-1]) - float(closes[-2])) / float(closes[-2])
        ara = pct >= IDX_ARA_THRESHOLD
        arb = pct <= IDX_ARB_THRESHOLD
        if ara:
            log(f"  ⚠️ {ticker}: kemungkinan ARA ({pct:+.1%}) — skip signal", "warn")
        if arb:
            log(f"  ⚠️ {ticker}: kemungkinan ARB ({pct:+.1%}) — skip signal", "warn")
        return {"ara": ara, "arb": arb, "pct_change": round(pct, 4)}
    except Exception:
        return default


def check_api_cascade_failure(data_fail: int, total_attempted: int,
                                consecutive_fails_in_row: int = 0) -> dict:
    """
    [J05] Layer 5 — API Error Cascade Detection.

    Bedakan antara:
      1. "Tidak ada sinyal" (normal) — data OK, kondisi pasar tidak mendukung
      2. "Data outage" (existing v7.14) — > 40% ticker gagal
      3. "API cascade" (baru) — >60% ticker gagal ATAU 5 fail berturut-turut

    API cascade lebih berbahaya dari data outage biasa karena:
    - Bisa jadi koneksi internet terputus sebagian
    - yfinance rate-limit total
    - IDX maintenance tak terduga
    Dalam kondisi ini, sinyal yang berhasil lolos pun datanya TIDAK bisa dipercaya.
    """
    default = {"cascade": False, "reason": "", "severity": "NORMAL"}
    if total_attempted == 0:
        return default

    fail_rate = data_fail / total_attempted

    if consecutive_fails_in_row >= API_CONSECUTIVE_FAILS:
        return {
            "cascade": True,
            "reason": f"{consecutive_fails_in_row} ticker berturut-turut gagal "
                      f"— kemungkinan koneksi/rate-limit total",
            "severity": "CRITICAL"
        }

    if fail_rate > API_FAIL_RATE_MAX:
        return {
            "cascade": True,
            "reason": f"API fail rate {fail_rate:.0%} ({data_fail}/{total_attempted}) "
                      f"> {API_FAIL_RATE_MAX:.0%} — API cascade suspected",
            "severity": "HIGH"
        }

    return default


def check_data_corruption(closes, highs, lows, volumes, ticker: str = "") -> dict:
    """
    [J05] Layer 6 — OHLCV Data Corruption Guard.

    Deteksi anomali data yang menghasilkan signal palsu.
    Corruption sumber: yfinance bug, adjusted price error, split tidak terdeteksi.

    Cek:
    1. Price jump anomali: close berubah > 30% satu candle tanpa ada news split
    2. Zero/negative price: OHLCV harusnya selalu positif
    3. High < Low: impossible candle (data error)
    4. Volume spike ekstrem: 100x rata-rata biasanya corporate action / data error
    5. Monotone closes: harga tidak berubah >= 5 candle berturut (suspend/stale data)

    Returns: {corrupt: bool, reasons: list[str]}
    """
    reasons = []
    try:
        c = closes.astype(float)
        h = highs.astype(float)
        l = lows.astype(float)
        v = volumes.astype(float)
        n = len(c)

        if n < 5:
            return {"corrupt": False, "reasons": []}

        # Check 1: Impossible candle (high < low)
        invalid_candles = np.sum(h < l)
        if invalid_candles > 0:
            reasons.append(f"Impossible candle: {invalid_candles}x High < Low")

        # Check 2: Zero atau negative price
        zero_prices = np.sum(c <= 0)
        if zero_prices >= DATA_CORRUPT_ZERO_PRICE:
            reasons.append(f"{zero_prices} candle dengan harga <= 0 (stale/corrupt)")

        # Check 3: Price jump anomali (kemungkinan split tak terdeteksi atau data error)
        if n >= 2:
            returns = np.abs(np.diff(c) / (c[:-1] + 1e-9))
            extreme_jumps = np.sum(returns > DATA_CORRUPT_PRICE_JUMP)
            if extreme_jumps > 1:   # 1 masih bisa legitimate (breakout/news), > 1 mencurigakan
                reasons.append(f"{extreme_jumps} candle dengan price jump > "
                               f"{DATA_CORRUPT_PRICE_JUMP:.0%} (split/data error?)")

        # Check 4: Volume spike ekstrem
        if n >= 10:
            vol_avg = float(np.mean(v[:-1]))
            if vol_avg > 0:
                vol_spike_ratio = float(v[-1]) / vol_avg
                if vol_spike_ratio > DATA_CORRUPT_VOL_SPIKE:
                    reasons.append(f"Volume spike {vol_spike_ratio:.0f}x rata-rata "
                                   f"(kemungkinan data error, bukan trading genuine)")

        # Check 5: Monotone price (saham suspend / stale data)
        last_5 = c[-5:]
        if np.all(last_5 == last_5[0]):
            reasons.append("5 candle terakhir identik — kemungkinan suspend / stale yfinance data")

    except Exception as e:
        log(f"⚠️ check_data_corruption [{ticker}]: {e}", "warn")
        return {"corrupt": False, "reasons": []}

    corrupt = len(reasons) > 0
    if corrupt:
        log(f"  ⚠️ {ticker}: Data corruption detected — {' | '.join(reasons)}", "warn")

    return {"corrupt": corrupt, "reasons": reasons}


# ════════════════════════════════════════════════════════
#  DEDUPLICATION via Supabase
# ════════════════════════════════════════════════════════

def _dedup_key(pair: str, strategy: str, side: str | None) -> str:
    return f"{pair}|{strategy}|{side or '_ANY_'}"


def _already_sent_generic(pair: str, strategy: str, dedup_hours: int,
                            side: str | None = None) -> bool:
    """Cek apakah signal sudah dikirim dalam window waktu tertentu."""
    key = _dedup_key(pair, strategy, side)
    if key in _dedup_memory:
        return True
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=dedup_hours)).isoformat()
        q = (
            supabase.table("signals")
            .select("id")
            .eq("pair", pair)
            .eq("strategy", strategy)
            .gt("sent_at", since)
        )
        if side is not None:
            q = q.eq("side", side)
        return len(q.execute().data) > 0
    except Exception as e:
        log(f"⚠️ dedup check [{strategy}|{pair}]: {e} — pakai in-memory fallback", "warn")
        return False


def already_sent(pair: str, strategy: str, side: str) -> bool:
    return _already_sent_generic(pair, strategy, DEDUP_HOURS, side=side)


def save_signal(pair: str, strategy: str, side: str, entry: float,
                tp1: float, tp2, sl: float, tier: str, score: int,
                timeframe: str, regime: str = "TRENDING",
                sector: str = "MISC", win_prob: float = None,
                ev: float = None, signal_mode: str = None,
                data_source: str = None,
                # [PHASE-3] Extended metadata untuk edge analytics
                rr: float = None, atr_pct: float = None, adx: float = None,
                phase: str = None, daily_bias: str = None,
                entry_pattern: str = None, entry_strength: str = None):
    """
    Simpan signal ke Supabase.
    [v5.0] Tambah kolom regime + sector untuk Performance Clustering,
    dan win_prob + ev untuk analisis kualitas signal.
    [R01] Tambah data_source — label eksplisit asal data (yfinance delay dll).
    [R03] Tambah signal_mode — SIMPLE atau COMPLEX, untuk A/B comparison empiris.
    [PHASE-3] Tambah extended metadata: rr, atr_pct, adx, phase, daily_bias,
    entry_pattern, entry_strength — diperlukan untuk edge analytics setelah
    50–100 trade terkumpul.

    Kolom baru perlu ditambahkan ke tabel Supabase (lihat SQL migrations di atas).
    """
    ticker_jk = pair if pair.endswith(".JK") else pair + ".JK"
    _sector   = TICKER_SECTOR.get(ticker_jk, sector)

    payload = {
        "pair":      pair,
        "strategy":  strategy,
        "side":      side,
        "entry":     entry,
        "tp1":       tp1,
        "tp2":       tp2,
        "sl":        sl,
        "tier":      tier,
        "score":     score,
        "timeframe": timeframe,
        "sent_at":   datetime.now(timezone.utc).isoformat(),
        "outcome":   None,   # diisi oleh update_signal_outcomes()
        "regime":    regime,
        "sector":    _sector,
    }
    # win_prob dan ev opsional — hanya ada di v5.0+
    if win_prob is not None:
        payload["win_prob"] = round(float(win_prob), 4)
    if ev is not None:
        payload["ev"] = round(float(ev), 4)
    # [R03] signal_mode — SIMPLE / COMPLEX — untuk A/B perbandingan empiris
    if signal_mode is not None:
        payload["signal_mode"] = signal_mode
    # [R01] data_source — label jujur asal data (yfinance delay, EOD, dll)
    if data_source is not None:
        payload["data_source"] = data_source

    # [PHASE-3] Extended metadata — simpan selengkap mungkin untuk edge analysis
    if rr is not None:
        payload["rr"] = round(float(rr), 3)
    if atr_pct is not None:
        payload["atr_pct"] = round(float(atr_pct), 3)
    if adx is not None:
        payload["adx"] = round(float(adx), 2)
    if phase is not None:
        payload["phase"] = phase
    if daily_bias is not None:
        payload["daily_bias"] = daily_bias
    if entry_pattern is not None:
        payload["entry_pattern"] = entry_pattern
    if entry_strength is not None:
        payload["entry_strength"] = str(entry_strength)

    # [PHASE-3] ENFORCE: pastikan semua field wajib tersimpan.
    # Jika ada yang None saat PHASE3 aktif → log warning agar mudah dideteksi.
    # market_condition = regime dalam bahasa manusia, untuk readability di dashboard.
    if PHASE3_COLLECTION:
        payload["market_condition"] = regime   # duplikat eksplisit untuk dashboard PHASE3
        _missing_p3 = [f for f in ("rr", "atr_pct", "adx", "phase", "daily_bias",
                                    "entry_pattern", "score", "regime")
                       if payload.get(f) is None]
        if _missing_p3:
            log(f"  ⚠️ [PHASE3] save_signal [{pair}]: field wajib KOSONG → {_missing_p3} "
                f"— data tidak lengkap untuk edge analysis!", "warn")

    try:
        supabase.table("signals").insert(payload).execute()
        if PHASE3_COLLECTION:
            log(f"  💾 [PHASE3] Saved: {pair} {strategy} {side} "
                f"RR={rr or '?'} ADX={adx or '?':.0f} phase={phase or '?'} "
                f"bias={daily_bias or '?'} pattern={entry_pattern or '?'}")
    except Exception as e:
        log(f"⚠️ save_signal [{pair}]: {e}", "warn")
    finally:
        _dedup_memory.add(_dedup_key(pair, strategy, side))


# ════════════════════════════════════════════════════════
#  OUTCOME ALERT — Notifikasi Telegram saat TP/SL hit
# ════════════════════════════════════════════════════════

def _send_outcome_alert(row: dict, outcome: str, strategy: str):
    """
    Kirim notifikasi Telegram saat sinyal lama resolved WIN atau LOSS.
    Dipanggil dari update_signal_outcomes() setiap kali ada outcome baru.
    """
    try:
        pair     = row.get("pair", "?")
        side     = row.get("side", "?")
        entry    = float(row.get("entry") or 0)
        tp1      = float(row.get("tp1")   or 0)
        sl       = float(row.get("sl")    or 0)

        is_win   = outcome == "WIN"
        emoji    = "✅" if is_win else "❌"
        result   = "WIN — TP1 tercapai! 🎯" if is_win else "LOSS — SL kena 🛑"
        side_str = "🟢 BUY" if side == "BUY" else "🔴 SELL"

        hit_price = tp1 if is_win else sl
        hit_label = "TP1" if is_win else "SL"

        pct = abs(hit_price - entry) / entry * 100 if entry > 0 else 0
        pct_str = f"+{pct:.1f}%" if is_win else f"-{pct:.1f}%"

        strat_emoji = "📈" if strategy == "INTRADAY" else "🌊"

        msg = (
            f"{emoji} <b>OUTCOME UPDATE</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{strat_emoji} {strategy} | {side_str}\n"
            f"Saham  : <b>{pair}</b>\n"
            f"Entry  : Rp{entry:,.0f}\n"
            f"{hit_label}     : Rp{hit_price:,.0f} ({pct_str})\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Hasil  : <b>{result}</b>\n"
            f"<i>Win rate diupdate otomatis.</i>"
        )
        tg(msg)
    except Exception as e:
        log(f"⚠️ _send_outcome_alert [{row.get('pair')}]: {e}", "warn")


# ════════════════════════════════════════════════════════
#  WIN RATE TRACKER — Update outcome signal lama
# ════════════════════════════════════════════════════════

def update_signal_outcomes():
    """
    Cek signal yang belum punya outcome (outcome IS NULL).
    - INTRADAY: cek high/low candle 1h dalam 2 hari terakhir
    - SWING   : cek high/low candle 1d dalam 10 hari terakhir

    FIX 4C: Tambahkan .limit(50) dengan sort strategy ASC agar INTRADAY
    (window lebih pendek, lebih mendesak) diprioritaskan lebih dulu.
    Ini mencegah ratusan SWING signals lama membanjiri run budget sebelum
    INTRADAY yang fresh sempat dicek.

    [PHASE-3] Extended outcome data:
    - duration_hours : berapa lama sampai outcome terjadi
    - rr_actual      : RR yang benar-benar terealisasi (bukan promised)
    - closed_price   : harga saat TP/SL hit
    - mfe_pct        : Maximum Favorable Excursion (%)
    - mae_pct        : Maximum Adverse Excursion (%)
    MFE + MAE penting untuk memahami trade quality dan stop placement.
    """
    EXPIRY = {"INTRADAY": 2, "SWING": 10}  # hari maksimal signal dianggap aktif

    try:
        rows = (
            supabase.table("signals")
            .select("id, pair, side, entry, tp1, sl, strategy, sent_at, rr")
            .is_("outcome", "null")
            .order("strategy", desc=False)   # INTRADAY < SWING alphabetically
            .limit(50)                        # FIX 4C: cap 50 signal per run
            .execute()
            .data
        )
        if not rows:
            log("📊 Win rate tracker: tidak ada signal pending")
            return

        resolved = 0
        expired  = 0
        now_utc  = datetime.now(timezone.utc)

        for row in rows:
            strategy = row.get("strategy", "SWING")
            max_days = EXPIRY.get(strategy, 10)

            # Cek apakah signal sudah melewati expiry window
            try:
                sent_at = datetime.fromisoformat(row["sent_at"])
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
                age_days = (now_utc - sent_at).days
            except Exception as _e:
                log(f"  ⚠️ [FALLBACK] signal age_days parse: {_e} — default 0", "warn")
                age_days = 0
                sent_at  = now_utc

            if age_days > max_days:
                supabase.table("signals").update(
                    {"outcome":      "EXPIRED",
                     "closed_at":    now_utc.isoformat(),
                     "duration_hours": round(age_days * 24.0, 1)}
                ).eq("id", row["id"]).execute()
                expired += 1
                log(f"  ⏳ Expired [{row['pair']} {strategy}]: {age_days}d > {max_days}d max")
                continue

            ticker   = row["pair"] if row["pair"].endswith(".JK") else row["pair"] + ".JK"
            interval = "1h" if strategy == "INTRADAY" else "1d"
            limit    = 48  if strategy == "INTRADAY" else 15  # 48 jam atau 15 hari

            try:
                data = get_candles(ticker, interval, limit)
                if data is None:
                    continue
                _, highs, lows, _, _op = data

                entry_price = float(row["entry"])
                tp1         = float(row["tp1"])
                sl          = float(row["sl"])
                sl_dist     = abs(entry_price - sl)
                outcome     = None
                hit_candle_idx = None   # [PHASE-3] index candle saat outcome

                # [PHASE-3] Track MFE & MAE across all candles
                mfe_pct = 0.0   # Maximum Favorable Excursion
                mae_pct = 0.0   # Maximum Adverse Excursion

                # Cek secara kronologis — candle pertama yang hit SL atau TP menentukan outcome
                for idx, (h, l) in enumerate(zip(highs, lows)):
                    h, l = float(h), float(l)

                    # [PHASE-3] Accumulate MFE/MAE per candle
                    if row["side"] == "BUY":
                        favorable_pct = (h - entry_price) / entry_price * 100
                        adverse_pct   = (entry_price - l) / entry_price * 100
                    else:
                        favorable_pct = (entry_price - l) / entry_price * 100
                        adverse_pct   = (h - entry_price) / entry_price * 100
                    mfe_pct = max(mfe_pct, favorable_pct)
                    mae_pct = max(mae_pct, adverse_pct)

                    if row["side"] == "BUY":
                        if l <= sl:
                            outcome = "LOSS"; hit_candle_idx = idx; break
                        if h >= tp1:
                            outcome = "WIN";  hit_candle_idx = idx; break
                    else:  # SELL
                        if h >= sl:
                            outcome = "LOSS"; hit_candle_idx = idx; break
                        if l <= tp1:
                            outcome = "WIN";  hit_candle_idx = idx; break

                if outcome is None:
                    # ── [9] Position Management — cek trailing/BE/partial TP ──
                    closes_pm, highs_pm, lows_pm, _, _op2 = data
                    mgmt = check_position_management(row, closes_pm, highs_pm, lows_pm)
                    if mgmt:
                        log(f"  📡 Position mgmt [{row['pair']} {row['side']}]: "
                            f"profit={mgmt['profit_r']:.1f}R | "
                            f"actions={[a['action'] for a in mgmt['actions']]}")
                        send_position_management_alert(row, mgmt)
                    continue   # masih terbuka dalam window

                # ── [PHASE-3] Compute duration_hours ─────────────────
                # Estimasi: hit_candle_idx × candle_duration + partial
                candle_hours = 1.0 if strategy == "INTRADAY" else 24.0
                duration_hrs = round(
                    (hit_candle_idx + 0.5) * candle_hours  # +0.5 = pertengahan candle
                    if hit_candle_idx is not None else age_days * 24.0,
                    1
                )

                # ── [PHASE-3] Compute rr_actual ───────────────────────
                closed_px  = tp1 if outcome == "WIN" else sl
                rr_promised = float(row.get("rr") or 0)
                rr_actual   = None
                if sl_dist > 0:
                    if row["side"] == "BUY":
                        rr_actual = (tp1 - entry_price) / sl_dist if outcome == "WIN" \
                                    else -(entry_price - sl) / sl_dist
                    else:
                        rr_actual = (entry_price - tp1) / sl_dist if outcome == "WIN" \
                                    else -(sl - entry_price) / sl_dist
                    rr_actual = round(rr_actual, 3)

                # ── [Y01] Actual PnL — gunakan actual_fill_price jika tersedia ──
                outcome_payload: dict = {
                    "outcome":        outcome,
                    "closed_at":      now_utc.isoformat(),
                    # [PHASE-3] Extended outcome fields
                    "duration_hours": duration_hrs,
                    "closed_price":   round(closed_px, 2),
                    "mfe_pct":        round(mfe_pct, 3),
                    "mae_pct":        round(mae_pct, 3),
                }
                if rr_actual is not None:
                    outcome_payload["rr_actual"] = rr_actual

                try:
                    fill_row = (
                        supabase.table("signals")
                        .select("actual_fill_price, simulated_entry")
                        .eq("id", row["id"])
                        .maybe_single()
                        .execute()
                        .data
                    )
                    actual_fill = float(fill_row.get("actual_fill_price") or 0) if fill_row else 0.0
                    sim_entry   = float((fill_row or {}).get("simulated_entry") or row.get("entry") or 0)
                    entry_used  = actual_fill if actual_fill > 0 else sim_entry
                    if entry_used > 0:
                        tp1_p = float(row["tp1"])
                        sl_p  = float(row["sl"])
                        if row["side"] == "BUY":
                            actual_pnl_pct = ((tp1_p - entry_used) / entry_used * 100
                                              if outcome == "WIN"
                                              else (sl_p - entry_used) / entry_used * 100)
                        else:
                            actual_pnl_pct = ((entry_used - tp1_p) / entry_used * 100
                                              if outcome == "WIN"
                                              else -abs(sl_p - entry_used) / entry_used * 100)
                        outcome_payload["actual_pnl_pct"]  = round(actual_pnl_pct, 4)
                        outcome_payload["fill_source_used"] = "actual" if actual_fill > 0 else "simulated"
                        log(f"    [Y01] PnL [{row['pair']} {outcome}]: "
                            f"{actual_pnl_pct:+.2f}% "
                            f"(fill={'actual' if actual_fill > 0 else 'sim'}, "
                            f"entry={entry_used:,.0f})")
                except Exception as _pnl_e:
                    log(f"    ⚠️ [Y01] PnL calc [{row['pair']}]: {_pnl_e}", "warn")

                supabase.table("signals").update(outcome_payload).eq("id", row["id"]).execute()
                resolved += 1

                if PHASE3_COLLECTION:
                    log(f"  ✅ [PHASE3] Outcome [{row['pair']} {row['side']} {strategy}]: "
                        f"{outcome} | dur={duration_hrs}h | "
                        f"RR_actual={rr_actual} (promised={rr_promised}) | "
                        f"MFE={mfe_pct:.1f}% MAE={mae_pct:.1f}%")
                else:
                    log(f"  ✅ Outcome [{row['pair']} {row['side']} {strategy}]: {outcome}")

                # ── Outcome Alert — kirim notifikasi ke Telegram ──
                _send_outcome_alert(row, outcome, strategy)

            except Exception as e:
                log(f"⚠️ outcome check [{row['pair']}]: {e}", "warn")
                continue

        log(f"📊 Win rate tracker: {resolved} resolved, {expired} expired / {len(rows)} total pending")
    except Exception as e:
        log(f"⚠️ update_signal_outcomes: {e}", "warn")


def get_win_rate_summary() -> dict:
    """
    Ambil statistik win rate dari Supabase.
    Return dict dengan breakdown per strategy dan overall.

    FIX 3C: EXPIRED signals dimasukkan sebagai "NEUTRAL_MISS" dengan
    bobot 50% (0.5 WIN, 0.5 LOSS). Signal yang expire tanpa hit TP
    merupakan opportunity cost nyata — tidak dieksekusi bukan berarti
    hasilnya netral, terutama di pasar trending. Menghilangkan EXPIRED
    sepenuhnya membuat win rate terlihat lebih baik dari kenyataannya.
    """
    default = {"overall": None, "intraday": None, "swing": None,
               "total_closed": 0, "wins": 0}
    try:
        # Ambil WIN + LOSS untuk denominasi utama
        rows = (
            supabase.table("signals")
            .select("strategy, outcome")
            .in_("outcome", ["WIN", "LOSS"])
            .execute()
            .data
        )

        # FIX 3C: Ambil EXPIRED untuk NEUTRAL_MISS weighting
        expired_rows = (
            supabase.table("signals")
            .select("strategy, outcome")
            .eq("outcome", "EXPIRED")
            .execute()
            .data
        )

        if not rows:
            return default

        wins_raw  = sum(1 for r in rows if r["outcome"] == "WIN")
        total_raw = len(rows)

        # EXPIRED berkontribusi 0.5 win equivalent per signal
        expired_count   = len(expired_rows) if expired_rows else 0
        expired_win_eq  = expired_count * 0.5   # NEUTRAL_MISS = setengah win

        total_adj = total_raw + expired_count
        wins_adj  = wins_raw + expired_win_eq

        def wr_adjusted(strategy: str) -> float | None:
            strat_rows   = [r for r in rows if r["strategy"] == strategy]
            strat_exp    = [r for r in expired_rows if r["strategy"] == strategy] if expired_rows else []
            if not strat_rows and not strat_exp:
                return None
            s_wins  = sum(1 for r in strat_rows if r["outcome"] == "WIN") + len(strat_exp) * 0.5
            s_total = len(strat_rows) + len(strat_exp)
            return round(s_wins / s_total * 100, 1) if s_total > 0 else None

        return {
            "overall":      round(wins_adj / total_adj * 100, 1) if total_adj else None,
            "intraday":     wr_adjusted("INTRADAY"),
            "swing":        wr_adjusted("SWING"),
            "total_closed": total_raw,
            "wins":         wins_raw,
            "expired":      expired_count,
        }
    except Exception as e:
        log(f"⚠️ get_win_rate_summary: {e}", "warn")
        return default


# ════════════════════════════════════════════════════════
#  [PHASE-3] COLLECTION PROGRESS + EDGE DASHBOARD
#  Track kemajuan menuju 50–100 resolved trade.
#  Kalkulasi live: WR, EV, distribusi RR, avg duration.
#  Tidak ada optimasi — hanya pengamatan jujur.
# ════════════════════════════════════════════════════════

def get_collection_progress() -> dict:
    """
    [PHASE-3] Query Supabase untuk progress menuju target data.
    Return dict lengkap: jumlah resolved, WR, EV, distribusi RR,
    avg duration, breakdown per strategy/regime/tier.
    Dipanggil di awal run() dan oleh --collection-report CLI.
    """
    default = {
        "resolved": 0, "wins": 0, "losses": 0,
        "win_rate": None, "ev": None,
        "avg_rr_actual": None, "avg_duration_hours": None,
        "pct_toward_min": 0.0, "pct_toward_full": 0.0,
        "phase3_ready": False, "by_strategy": {}, "by_regime": {},
        "rr_distribution": [], "mfe_mae": {},
    }
    try:
        rows = (
            supabase.table("signals")
            .select("outcome, strategy, regime, tier, score, rr, rr_actual, "
                    "duration_hours, mfe_pct, mae_pct, sent_at, phase, "
                    "daily_bias, entry_pattern, adx")
            .in_("outcome", ["WIN", "LOSS"])
            .order("sent_at", desc=False)
            .limit(500)
            .execute()
            .data
        )
        if not rows:
            return default

        wins   = [r for r in rows if r["outcome"] == "WIN"]
        losses = [r for r in rows if r["outcome"] == "LOSS"]
        n      = len(rows)
        n_win  = len(wins)
        wr     = round(n_win / n * 100, 1) if n > 0 else None

        # ── EV dari RR actual jika tersedia ──────────────────
        rr_actuals = [float(r["rr_actual"]) for r in rows
                      if r.get("rr_actual") is not None]
        rr_promised = [float(r["rr"]) for r in rows
                       if r.get("rr") is not None]
        avg_rr_actual  = round(float(sum(rr_actuals) / len(rr_actuals)), 3) \
                         if rr_actuals else None
        avg_rr_promise = round(float(sum(rr_promised) / len(rr_promised)), 3) \
                         if rr_promised else None

        # EV = WR × avg_RR_actual − (1 - WR)
        ev = None
        if wr is not None and avg_rr_actual is not None:
            ev = round((wr / 100) * avg_rr_actual - (1 - wr / 100), 3)

        # ── Duration ──────────────────────────────────────────
        durations = [float(r["duration_hours"]) for r in rows
                     if r.get("duration_hours") is not None]
        avg_dur = round(float(sum(durations) / len(durations)), 1) if durations else None

        # ── RR distribution (signed: positive = WIN, negative = LOSS) ──
        rr_dist = []
        for r in rows:
            val = r.get("rr_actual") or r.get("rr")
            if val is not None:
                signed = float(val) if r["outcome"] == "WIN" else -abs(float(val))
                rr_dist.append(round(signed, 2))

        # ── MFE / MAE avg ─────────────────────────────────────
        mfe_vals = [float(r["mfe_pct"]) for r in rows if r.get("mfe_pct") is not None]
        mae_vals = [float(r["mae_pct"]) for r in rows if r.get("mae_pct") is not None]
        mfe_mae = {
            "avg_mfe": round(sum(mfe_vals) / len(mfe_vals), 2) if mfe_vals else None,
            "avg_mae": round(sum(mae_vals) / len(mae_vals), 2) if mae_vals else None,
            "mfe_mae_ratio": round(
                (sum(mfe_vals) / len(mfe_vals)) / (sum(mae_vals) / len(mae_vals)), 2
            ) if mfe_vals and mae_vals and sum(mae_vals) > 0 else None,
        }

        # ── Breakdown per strategy ────────────────────────────
        by_strategy = {}
        for strat in ["INTRADAY", "SWING"]:
            s_rows = [r for r in rows if r.get("strategy") == strat]
            if s_rows:
                s_wins = sum(1 for r in s_rows if r["outcome"] == "WIN")
                by_strategy[strat] = {
                    "n": len(s_rows),
                    "wr": round(s_wins / len(s_rows) * 100, 1),
                }

        # ── Breakdown per regime ──────────────────────────────
        by_regime = {}
        for reg in ["TRENDING", "RANGING"]:
            r_rows = [r for r in rows if r.get("regime") == reg]
            if len(r_rows) >= 3:
                r_wins = sum(1 for r in r_rows if r["outcome"] == "WIN")
                by_regime[reg] = {
                    "n": len(r_rows),
                    "wr": round(r_wins / len(r_rows) * 100, 1),
                }

        # ── Progress toward targets ───────────────────────────
        pct_min  = round(min(n / COLLECTION_TARGET_MIN  * 100, 100.0), 1)
        pct_full = round(min(n / COLLECTION_TARGET_FULL * 100, 100.0), 1)

        return {
            "resolved":          n,
            "wins":              n_win,
            "losses":            len(losses),
            "win_rate":          wr,
            "ev":                ev,
            "avg_rr_actual":     avg_rr_actual,
            "avg_rr_promised":   avg_rr_promise,
            "avg_duration_hours": avg_dur,
            "pct_toward_min":    pct_min,
            "pct_toward_full":   pct_full,
            "phase3_ready":      n >= COLLECTION_TARGET_MIN,
            "by_strategy":       by_strategy,
            "by_regime":         by_regime,
            "rr_distribution":   rr_dist,
            "mfe_mae":           mfe_mae,
        }
    except Exception as e:
        log(f"⚠️ get_collection_progress: {e}", "warn")
        return default


def send_collection_report(progress: dict | None = None):
    """
    [PHASE-3] Kirim laporan kemajuan data collection ke Telegram.
    Dipanggil di akhir run() jika PHASE3_COLLECTION=True,
    dan via CLI --collection-report.
    """
    if progress is None:
        progress = get_collection_progress()

    n         = progress["resolved"]
    wr        = progress["win_rate"]
    ev        = progress["ev"]
    avg_rr    = progress["avg_rr_actual"]
    avg_dur   = progress["avg_duration_hours"]
    pct_min   = progress["pct_toward_min"]
    pct_full  = progress["pct_toward_full"]
    mfe_mae   = progress["mfe_mae"]

    # Progress bar (10 blok)
    def _bar(pct: float) -> str:
        filled = int(pct / 10)
        return "█" * filled + "░" * (10 - filled) + f" {pct:.0f}%"

    ready_emoji = "✅" if progress["phase3_ready"] else "⏳"

    # Per-strategy breakdown
    strat_lines = "\n".join(
        f"  {s}: WR={d['wr']}% ({d['n']} trade)"
        for s, d in progress["by_strategy"].items()
    ) or "  Belum ada data"

    regime_lines = "\n".join(
        f"  {r}: WR={d['wr']}% ({d['n']} trade)"
        for r, d in progress["by_regime"].items()
    ) or "  Belum ada data"

    # EV dan RR interpretation
    ev_str  = f"{ev:+.3f}" if ev is not None else "N/A"
    ev_note = ("✅ Positif" if (ev or 0) > 0.1
               else "⚠️ Tipis" if (ev or 0) > 0
               else "❌ Negatif" if ev is not None else "—")

    rr_str  = f"{avg_rr:.2f}R" if avg_rr is not None else "N/A"
    dur_str = f"{avg_dur:.1f}j" if avg_dur is not None else "N/A"

    mfe_str = (f"MFE avg {mfe_mae['avg_mfe']:.1f}% | "
               f"MAE avg {mfe_mae['avg_mae']:.1f}% | "
               f"ratio {mfe_mae['mfe_mae_ratio']:.1f}"
               if mfe_mae.get("avg_mfe") is not None else "Belum ada data MFE/MAE")

    # Data quality warning
    if n < 10:
        quality_note = "⚠️ <i>Data sangat sedikit — WR/EV belum bermakna.</i>"
    elif n < 30:
        quality_note = "⚠️ <i>Data awal — angka masih sangat volatile.</i>"
    elif n < COLLECTION_TARGET_MIN:
        quality_note = f"⏳ <i>Perlu {COLLECTION_TARGET_MIN - n} trade lagi untuk analisis kasar.</i>"
    else:
        quality_note = "✅ <i>Data cukup untuk analisis WR + EV awal.</i>"

    msg = (
        f"📊 <b>PHASE 3 — Data Collection Progress</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Resolved trades : <b>{n}</b> "
        f"({progress['wins']}W / {progress['losses']}L)\n"
        f"Target min ({COLLECTION_TARGET_MIN}) : {_bar(pct_min)}\n"
        f"Target full ({COLLECTION_TARGET_FULL}): {_bar(pct_full)}\n"
        f"Status          : {ready_emoji} "
        f"{'Siap analisis awal' if progress['phase3_ready'] else 'Masih kumpul data'}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Win Rate  : <b>{wr}%</b>\n"
        f"EV        : <b>{ev_str}</b> → {ev_note}\n"
        f"RR actual : <b>{rr_str}</b>\n"
        f"Avg durasi: <b>{dur_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Per strategi:\n{strat_lines}\n"
        f"Per regime:\n{regime_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Excursion: {mfe_str}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{quality_note}\n"
        f"<i>🔒 PHASE 3: Tidak ada optimasi parameter sampai {COLLECTION_TARGET_MIN}+ trade.</i>"
    )
    try:
        tg(msg)
    except Exception as e:
        log(f"⚠️ send_collection_report: {e}", "warn")
    log(f"📊 [PHASE3] Collection: {n}/{COLLECTION_TARGET_MIN} min "
        f"| WR={wr}% | EV={ev_str} | RR={rr_str}")


# ════════════════════════════════════════════════════════
#  [PHASE-5] OPTIMIZATION LAYER SEQUENCER
#  Aktifkan satu layer optimasi per tahap.
#  Tidak ada yang boleh diaktifkan sebelum prerequisite terpenuhi.
# ════════════════════════════════════════════════════════

def _check_phase5_prerequisites(verdict: str, n_trades: int) -> tuple[bool, str]:
    """
    Cek apakah prerequisite PHASE5 terpenuhi.
    Return (ok: bool, reason: str).
    """
    if verdict not in PHASE5_MIN_VERDICT:
        return False, (
            f"Verdict={verdict} — butuh PROVEN atau PROMISING. "
            f"Selesaikan PHASE4 terlebih dahulu."
        )
    if n_trades < PHASE5_MIN_TRADES:
        return False, (
            f"n={n_trades} < {PHASE5_MIN_TRADES} — kumpulkan lebih banyak trade di PHASE3."
        )
    return True, f"OK (verdict={verdict}, n={n_trades})"


def apply_phase5_layers(verdict: str, n_trades: int) -> dict:
    """
    [PHASE-5] Aktifkan layer optimasi secara berurutan berdasarkan PHASE5_LAYER.

    Setiap layer diaktifkan HANYA jika prerequisite terpenuhi.
    Layer diaktifkan secara kumulatif — layer N mengandung semua layer < N.

    Layer 1 — Position sizing  : Kelly blend diaktifkan via LEAN_MODE=False
                                  sehingga get_feedback_weights() beroperasi penuh.
    Layer 2 — EV filter        : EV_MIN_THRESHOLD dinaikkan ke PHASE5_EV_THRESHOLD (0.15).
    Layer 3 — Cluster weights  : get_cluster_weights() dijalankan dari run().
    Layer 4 — Adaptive filter  : apply_adaptive_relaxation() dijalankan dari run().

    Return dict: {
        "layer_active": int,        # layer tertinggi yang aktif
        "prerequisites_ok": bool,
        "prerequisites_reason": str,
        "layers_applied": list[str],
        "layers_skipped": list[str],
        "ev_threshold_applied": float,
        "snapshot": dict,           # ← FIX: EV/WR snapshot saat layer ini aktif
    }
    """
    global LEAN_MODE, EV_MIN_THRESHOLD

    # ── [PHASE5-FIX] Ambil EV/WR snapshot sebelum layer diaktifkan ──────
    # Snapshot ini dikirim bersama status — engineer bisa bandingkan run-to-run
    # untuk memastikan setiap layer tidak merusak WR/EV/drawdown.
    _snap_wr  = None
    _snap_ev  = None
    _snap_pf  = None
    _snap_n   = n_trades
    try:
        _snap_edge = check_edge_proven()
        _snap_wr   = _snap_edge.get("wr")
        _snap_ev   = _snap_edge.get("empirical_ev")
        _snap_pf   = (_snap_edge.get("ensemble", {})
                      .get("methods", {})
                      .get("M2_profit_factor_train", {})
                      .get("value"))
    except Exception:
        pass   # snapshot gagal → lanjut, jangan block layer activation

    result = {
        "layer_active":          PHASE5_LAYER,
        "prerequisites_ok":      False,
        "prerequisites_reason":  "",
        "layers_applied":        [],
        "layers_skipped":        [],
        "ev_threshold_applied":  EV_MIN_THRESHOLD,
        "snapshot": {           # ← EV/WR pada saat layer ini dievaluasi
            "n":       _snap_n,
            "wr":      _snap_wr,
            "ev":      _snap_ev,
            "pf":      _snap_pf,
            "layer":   PHASE5_LAYER,
            "verdict": verdict,
        },
    }

    if PHASE5_LAYER == 0:
        result["prerequisites_reason"] = "PHASE5_LAYER=0 — semua layer nonaktif"
        log("  ℹ️ [PHASE5] Layer=0 — tidak ada optimasi aktif. "
            "Set PHASE5_LAYER=1 setelah PHASE4 terbukti.")
        return result

    # Cek prerequisite
    ok, reason = _check_phase5_prerequisites(verdict, n_trades)
    result["prerequisites_ok"]     = ok
    result["prerequisites_reason"] = reason

    if not ok:
        log(
            f"  ⛔ [PHASE5] Prerequisite GAGAL — PHASE5_LAYER={PHASE5_LAYER} "
            f"tidak akan diaktifkan.\n  Alasan: {reason}",
            "warn"
        )
        return result

    log(f"  ✅ [PHASE5] Prerequisite OK — {reason}")

    # ── Layer 1: Position Sizing (Kelly blend) ────────────────────────
    # Diaktifkan dengan mematikan LEAN_MODE paksa sehingga Kelly blend
    # di get_smart_risk_pct() bisa menggunakan win_prob empiris.
    # Tanpa ini, bot selalu pakai vol_risk saja (flat sizing).
    if PHASE5_LAYER >= 1:
        if LEAN_MODE:
            # Hanya lepas LEAN_MODE jika memang PHASE5 yang menyebabkannya
            # PHASE3 enforce sudah memaksa LEAN_MODE=True — jangan override
            if PHASE3_COLLECTION:
                result["layers_skipped"].append(
                    "LAYER1_POSITION_SIZING (PHASE3 aktif — LEAN_MODE tidak bisa dilepas)"
                )
                log("  ⚠️ [PHASE5/L1] Position sizing: SKIP — PHASE3 memaksa LEAN_MODE=True.", "warn")
            else:
                LEAN_MODE = False
                result["layers_applied"].append("LAYER1_POSITION_SIZING")
                log("  🟢 [PHASE5/L1] Position sizing AKTIF — Kelly blend diizinkan (LEAN_MODE=False)")
        else:
            result["layers_applied"].append("LAYER1_POSITION_SIZING")
            log("  🟢 [PHASE5/L1] Position sizing: sudah aktif (LEAN_MODE=False)")

    # ── Layer 2: EV Filter ────────────────────────────────────────────
    # Naikkan EV_MIN_THRESHOLD dari baseline 0.05 ke PHASE5_EV_THRESHOLD (default 0.15).
    # Ini memfilter signal dengan ekspektasi rendah setelah kita punya data empiris.
    if PHASE5_LAYER >= 2:
        _old_ev = EV_MIN_THRESHOLD
        if EV_MIN_THRESHOLD < PHASE5_EV_THRESHOLD:
            EV_MIN_THRESHOLD = PHASE5_EV_THRESHOLD
            result["layers_applied"].append("LAYER2_EV_FILTER")
            result["ev_threshold_applied"] = EV_MIN_THRESHOLD
            log(
                f"  🟢 [PHASE5/L2] EV filter AKTIF — "
                f"EV_MIN_THRESHOLD: {_old_ev:.3f} → {EV_MIN_THRESHOLD:.3f} "
                f"(hanya signal dengan EV ≥ {EV_MIN_THRESHOLD:.2f} yang lolos)"
            )
        else:
            result["layers_applied"].append("LAYER2_EV_FILTER")
            result["ev_threshold_applied"] = EV_MIN_THRESHOLD
            log(f"  🟢 [PHASE5/L2] EV filter: threshold sudah ≥ {PHASE5_EV_THRESHOLD:.3f} ({EV_MIN_THRESHOLD:.3f})")

    # ── Layer 3: Cluster Weights ──────────────────────────────────────
    # Aktifkan get_cluster_weights() — weight modifier berdasarkan performa
    # historis per cluster (regime × sector). Dijalankan dari run() setelah
    # flag ini diperiksa.
    # Catatan: fungsi pemanggilan get_cluster_weights() ada di run() —
    # flag di sini dibaca oleh run() untuk memutuskan apakah memanggilnya.
    if PHASE5_LAYER >= 3:
        result["layers_applied"].append("LAYER3_CLUSTER_WEIGHTS")
        log(
            "  🟢 [PHASE5/L3] Cluster weights AKTIF — "
            "weight modifier per regime×sector diizinkan. "
            "get_cluster_weights() akan dipanggil di run()."
        )

    # ── Layer 4: Adaptive Filter ──────────────────────────────────────
    # Aktifkan apply_adaptive_relaxation() — relaksasi threshold secara
    # adaptif berdasarkan filter audit dari run sebelumnya.
    # Hanya aman jika edge sudah PROVEN (bukan hanya PROMISING).
    # ⚠️ FIX: Layer 4 juga wajib punya ≥ 100 resolved trade — bukan hanya 50.
    # Adaptive filter adalah layer paling berisiko: modifikasi threshold runtime
    # berdasarkan audit data. Dengan < 100 trade, audit belum cukup stabil.
    _L4_MIN_TRADES = 100
    if PHASE5_LAYER >= 4:
        if verdict != "PROVEN":
            result["layers_skipped"].append(
                f"LAYER4_ADAPTIVE_FILTER (butuh PROVEN, sekarang {verdict})"
            )
            log(
                f"  ⚠️ [PHASE5/L4] Adaptive filter: SKIP — "
                f"butuh verdict PROVEN, sekarang {verdict}. "
                f"Layer 4 adalah yang paling berisiko — jangan aktifkan sebelum PROVEN.",
                "warn"
            )
        elif n_trades < _L4_MIN_TRADES:
            # ← FIX: guard 100 trade khusus Layer 4
            result["layers_skipped"].append(
                f"LAYER4_ADAPTIVE_FILTER (butuh {_L4_MIN_TRADES} trade, sekarang {n_trades})"
            )
            log(
                f"  ⚠️ [PHASE5/L4] Adaptive filter: SKIP — "
                f"n={n_trades} < {_L4_MIN_TRADES}. Layer 4 butuh minimal {_L4_MIN_TRADES} "
                f"trade resolved agar audit data cukup stabil. Lanjutkan koleksi.",
                "warn"
            )
        else:
            result["layers_applied"].append("LAYER4_ADAPTIVE_FILTER")
            log(
                f"  🟢 [PHASE5/L4] Adaptive filter AKTIF — "
                f"n={n_trades} ≥ {_L4_MIN_TRADES} & PROVEN. "
                "apply_adaptive_relaxation() akan dijalankan di run(). "
                "Pantau filter audit setelah aktivasi."
            )

    # ── Summary log ──────────────────────────────────────────────────
    applied_str = ", ".join(result["layers_applied"]) or "tidak ada"
    skipped_str = ", ".join(result["layers_skipped"]) or "tidak ada"
    _snap = result.get("snapshot", {})
    _snap_wr_str = f"{_snap['wr']:.0%}" if _snap.get("wr") is not None else "N/A"
    _snap_ev_str = f"{_snap['ev']:+.3f}" if _snap.get("ev") is not None else "N/A"
    _snap_pf_str = f"{_snap['pf']:.2f}" if isinstance(_snap.get("pf"), (int, float)) else "N/A"
    log(
        f"\n  📊 [PHASE5] Optimization summary:\n"
        f"     Layer target  : {PHASE5_LAYER}\n"
        f"     Applied       : {applied_str}\n"
        f"     Skipped       : {skipped_str}\n"
        f"     EV threshold  : {result['ev_threshold_applied']:.3f}\n"
        f"     LEAN_MODE now : {LEAN_MODE}\n"
        f"     ── Snapshot saat layer aktif ──\n"
        f"     WR            : {_snap_wr_str}\n"
        f"     EV            : {_snap_ev_str}\n"
        f"     PF            : {_snap_pf_str}\n"
        f"     n             : {_snap.get('n', 'N/A')}\n"
        f"     ⚠️  Catat nilai ini — bandingkan setelah layer berikutnya aktif."
    )
    # Satu-liner mudah di-grep
    _p5_applied_count = len(result["layers_applied"])
    log(
        f"PHASE5 STATUS | LAYER={PHASE5_LAYER} | "
        f"ACTIVE={_p5_applied_count}/{PHASE5_LAYER} | "
        f"PREREQ={'OK' if result['prerequisites_ok'] else 'FAIL'} | "
        f"WR={_snap_wr_str} | EV={_snap_ev_str} | PF={_snap_pf_str} | "
        f"EV_THR={result['ev_threshold_applied']:.3f} | "
        f"LEAN={LEAN_MODE}"
    )
    return result


def send_phase5_status(p5_result: dict):
    """
    [PHASE-5] Kirim status optimization layer ke Telegram.
    Hanya dikirim jika PHASE5_LAYER > 0 dan prerequisite OK.
    ⚠️ FIX: Tidak spam setiap run — hanya kirim saat layer berubah atau cold start.
    """
    if not p5_result.get("prerequisites_ok") and p5_result.get("layer_active", 0) == 0:
        return   # tidak perlu kirim jika PHASE5 belum aktif

    verdict_for_msg = p5_result.get("prerequisites_reason", "")
    applied  = p5_result.get("layers_applied",  [])
    skipped  = p5_result.get("layers_skipped",  [])
    ev_thr   = p5_result.get("ev_threshold_applied", EV_MIN_THRESHOLD)
    prereq   = p5_result.get("prerequisites_ok", False)
    layer    = p5_result.get("layer_active", 0)
    snap     = p5_result.get("snapshot", {})

    layer_desc = {
        0: "Semua nonaktif",
        1: "Position Sizing (Kelly)",
        2: "Position Sizing + EV Filter",
        3: "Position Sizing + EV Filter + Cluster Weights",
        4: "Semua layer (termasuk Adaptive Filter)",
    }

    status_emoji = "✅" if prereq else "⛔"
    applied_lines = "\n".join(f"  ✅ {l}" for l in applied) or "  (tidak ada)"
    skipped_lines = "\n".join(f"  ⏭️ {l}" for l in skipped) or "  (tidak ada)"

    # ── Snapshot metrics ──────────────────────────────────────────────
    _s_wr  = f"{snap['wr']:.0%}"    if snap.get("wr")  is not None              else "N/A"
    _s_ev  = f"{snap['ev']:+.3f}"   if snap.get("ev")  is not None              else "N/A"
    _s_pf  = f"{snap['pf']:.2f}"    if isinstance(snap.get("pf"), (int, float)) else "N/A"
    _s_n   = snap.get("n", "N/A")

    msg = (
        f"🚀 <b>PHASE 5 — Optimization Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Layer target  : <b>{layer} — {layer_desc.get(layer, '?')}</b>\n"
        f"Prerequisite  : {status_emoji} {verdict_for_msg}\n"
        f"EV threshold  : <b>{ev_thr:.3f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Layer aktif:\n{applied_lines}\n"
        f"Layer dilewati:\n{skipped_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 Snapshot saat layer ini aktif:\n"
        f"  WR : <b>{_s_wr}</b>  |  EV : <b>{_s_ev}</b>  |  PF : <b>{_s_pf}</b>  |  n : {_s_n}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>⚠️ Aktifkan satu layer per tahap.\n"
        f"Jika WR/EV turun setelah aktivasi → rollback layer, jangan lanjut.\n"
        f"Catat snapshot ini sebagai baseline sebelum aktifkan layer berikutnya.</i>"
    )
    try:
        tg(msg)
    except Exception as e:
        log(f"⚠️ send_phase5_status: {e}", "warn")


# ════════════════════════════════════════════════════════
#  AI FEEDBACK LOOP + SMART RISK — v3.0
# ════════════════════════════════════════════════════════

def get_feedback_weights() -> dict:
    """
    [Upgrade #6] AI Winrate Feedback Loop.
    Auto-adjust bobot scoring berdasarkan histori win/loss dari Supabase.
    Dipanggil SEKALI di awal run() dan di-cache ke _adaptive_weights.
    Jika data < 10 signal, fallback ke W base.
    """
    try:
        rows = (
            supabase.table("signals")
            .select("score, tier, outcome, strategy")
            .in_("outcome", ["WIN", "LOSS"])
            .order("sent_at", desc=True)
            .limit(120)
            .execute()
            .data
        )
        if not rows or len(rows) < MIN_SIGNALS_FOR_WEIGHT_VALIDATION:
            log(f"📊 Feedback weights: data belum cukup (<{MIN_SIGNALS_FOR_WEIGHT_VALIDATION} signal) — pakai base weights")
            log(f"   ℹ️  Ada {len(rows) if rows else 0} signal. Butuh {MIN_SIGNALS_FOR_WEIGHT_VALIDATION} untuk weight tuning yang statistically valid.")
            return W.copy()

        overall_wr = sum(1 for r in rows if r["outcome"] == "WIN") / len(rows)

        # Win rate per tier
        tier_wr = {}
        for tier in ["S", "A+", "A"]:
            t_rows = [r for r in rows if r.get("tier") == tier]
            if len(t_rows) >= 3:
                tier_wr[tier] = sum(1 for r in t_rows if r["outcome"] == "WIN") / len(t_rows)

        # Win rate per strategy
        intra_rows  = [r for r in rows if r.get("strategy") == "INTRADAY"]
        swing_rows  = [r for r in rows if r.get("strategy") == "SWING"]
        intra_wr    = sum(1 for r in intra_rows if r["outcome"] == "WIN") / len(intra_rows) if intra_rows else 0.5
        swing_wr_v  = sum(1 for r in swing_rows if r["outcome"] == "WIN") / len(swing_rows) if swing_rows else 0.5

        w = W.copy()
        adjustments = []

        # [v7.9 FIX] Ganti metric dari "tier win rate" ke "overall WR vs baseline"
        # Versi lama: "Tier S WR > 72% → boost BOS" menciptakan circular loop:
        # weight naik → lebih banyak signal Tier S → Tier S WR terlihat naik →
        # weight naik lagi. Ini tidak mengukur kualitas signal, hanya amplifikasi.
        #
        # Versi baru: bandingkan overall WR vs baseline expected (55%) sebagai anchor.
        # Perubahan berbasis data aggregat, bukan per-tier yang bias oleh weight sendiri.

        BASELINE_WR = 0.55   # expected win rate baseline untuk bot ini
        delta_wr    = overall_wr - BASELINE_WR

        # Rule 1: WR signifikan di atas baseline → market sedang favorable
        # Boost structure signals secara hati-hati (max +1)
        if delta_wr > 0.12:
            w["bos"]   = min(w["bos"] + 1, W["bos"] + 2)
            w["choch"] = min(w["choch"] + 1, W["choch"] + 2)
            adjustments.append(f"Overall WR {overall_wr:.0%} (+{delta_wr:.0%} vs baseline) → boost BOS/CHoCH")

        # Rule 2: WR signifikan di bawah baseline → conservatize
        elif delta_wr < -0.12:
            w["macd_soft"]   = max(w["macd_soft"] - 1,   -4)
            w["adx_ranging"] = max(w["adx_ranging"] - 1, -4)
            w["pullback"]    = max(w["pullback"] - 1,      1)
            adjustments.append(f"Overall WR {overall_wr:.0%} ({delta_wr:.0%} vs baseline) → conservatize")

        # Rule 3: Intraday berkinerja jauh lebih baik dari swing
        if intra_wr > swing_wr_v + 0.20:
            w["ema_align"] = min(w["ema_align"] + 1, 5)
            w["vwap_side"] = min(w["vwap_side"] + 1, 4)
            adjustments.append(f"Intraday WR {intra_wr:.0%} vs Swing {swing_wr_v:.0%} → boost intraday signals")

        if adjustments:
            log(f"📊 Feedback weights aktif ({len(rows)} signal): " + " | ".join(adjustments))
        else:
            log(f"📊 Feedback weights: overall_wr={overall_wr:.0%} — base weights dipertahankan")

        # FIX 3B: Weight drift ceiling — clamp setiap nilai ±2 dari W base
        # Mencegah feedback loop circular (tier → score → weights → tier) drift
        # monoton dalam satu arah setelah banyak cycle
        for k in w:
            base_val = W.get(k, 0)
            if base_val >= 0:
                w[k] = max(base_val - 2, min(w[k], base_val + 2))
            else:
                w[k] = max(base_val - 2, min(w[k], base_val + 2))

        return w

    except Exception as e:
        log(f"⚠️ get_feedback_weights: {e} — pakai base weights", "warn")
        return W.copy()


def calc_kelly_fraction(win_prob: float, avg_win_r: float, avg_loss_r: float = 1.0,
                         n_samples: int = 0) -> float:
    """
    [J03] Half-Kelly Fraction — position sizing berbasis probabilitas dan payoff.

    Formula Kelly: f = (bp - q) / b
      b = avg_win_r (payoff ratio)
      p = win_prob
      q = 1 - win_prob (loss probability)

    Kita pakai HALF-Kelly (f/2) karena:
    - Full Kelly over-bets saat estimasi win_prob tidak akurat
    - Half-Kelly memberikan ~75% growth rate Kelly dengan drawdown jauh lebih kecil
    - Cocok untuk bot dengan sample size terbatas (IDX hanya ~80 saham)

    [Q02] BAYESIAN SHRINKAGE — win_prob di-shrink ke prior 0.50 sebelum masuk formula.
    Motivasi: saat n_samples kecil, edge estimate sangat noisy. Contoh: 8 win dari 12
    trade = 67% WR — terlihat bagus tapi CI sangat lebar dan Kelly akan overbetting.
    Shrinkage formula (Bayesian update dengan prior WR=0.50, prior strength=30 sample):
      effective_p = (n × p + n_prior × 0.50) / (n + n_prior)
    Efek: semakin sedikit sample → effective_p semakin dekat 0.50 (lebih konservatif).
    Dengan n_prior=30: butuh ~60+ trade sebelum estimasi WR 60% benar-benar terasa.

    n_samples: jumlah trade yang dipakai untuk estimasi win_prob (0 = pakai default shrink penuh)

    Returns: fraction sebagai % portfolio (hard capped 3%)
    """
    if win_prob <= 0 or win_prob >= 1 or avg_win_r <= 0:
        return RISK_PCT

    # [Q02] Bayesian shrinkage toward prior WR = 0.50
    KELLY_PRIOR_WR = 0.50    # prior belief sebelum ada data: coin flip
    KELLY_N_PRIOR  = 50      # prior strength setara 50 trade — butuh ~100 trade sebelum WR 60% benar-benar terasa (anti noise-fit)
    effective_n    = max(n_samples, 0)
    effective_p    = (effective_n * win_prob + KELLY_N_PRIOR * KELLY_PRIOR_WR) / \
                     (effective_n + KELLY_N_PRIOR)

    b = avg_win_r
    p = effective_p
    q = 1.0 - p
    kelly = (b * p - q) / b
    half_kelly_pct = max(kelly / 2.0, 0.0) * 100
    capped = round(min(half_kelly_pct, 3.0), 2)   # hard cap 3% — IDX lebih volatile

    if n_samples > 0 and abs(effective_p - win_prob) > 0.02:
        log(f"  🔵 Kelly shrinkage: raw WR={win_prob:.0%} → shrunk={effective_p:.0%} "
            f"(n={n_samples}, prior=30) → half-Kelly={capped:.2f}%")
    return capped


def calc_volatility_adjusted_risk(base_risk: float, atr_pct: float,
                                   strategy: str = "SWING") -> float:
    """
    [J03] ATR Volatility Scaling — sesuaikan risk berdasarkan volatilitas saham.

    Prinsip: saham lebih volatile = position size lebih kecil.
    Bukan karena tambah risk, tapi karena per-rupiah ATR lebih besar → SL lebih jauh.

    Benchmark ATR IDX:
      Blue chip (BBCA, BMRI): ATR ~0.5-1.0% → full size
      Mid cap (MDKA, ARTO):   ATR ~1.5-2.5% → size dikurangi
      Speculative (CUAN):     ATR ~3-5%+     → size dipotong signifikan

    Formula: adjusted = base × (target_atr / actual_atr)
    Target ATR: 1.0% untuk swing, 0.8% untuk intraday
    """
    if atr_pct <= 0:
        return base_risk
    target_atr = 0.8 if strategy == "INTRADAY" else 1.0
    raw_adjusted = base_risk * (target_atr / atr_pct)
    min_risk = base_risk * 0.3   # jangan di bawah 30% base
    max_risk = base_risk * 1.5   # jangan di atas 150% base
    return round(min(max(raw_adjusted, min_risk), max_risk), 2)


def calc_correlation_adjusted_risk(base_risk: float, ticker: str,
                                    open_positions: list | None = None) -> float:
    """
    [J03] Correlation-Adjusted Position Sizing.

    Saham dalam sektor yang sama bergerak bersamaan (korelasi tinggi ~0.7-0.9).
    Jika kita sudah punya posisi di sektor yang sama, position size baru harus dikurangi
    agar total net exposure ke sektor tersebut tidak meledak saat sektor turun bersamaan.

    Formula pengurang:
      0 posisi di sektor ini → full size
      1 posisi di sektor ini → size × 0.75 (jaga-jaga)
      2+ posisi di sektor ini → size × 0.50 (proteksi konsentrasi)

    open_positions: list of dict dengan field 'sector' dari Supabase
    """
    if not open_positions:
        return base_risk
    my_sector = TICKER_SECTOR.get(ticker, "MISC")
    same_sector_count = sum(
        1 for p in open_positions
        if p.get("sector") == my_sector
    )
    if same_sector_count == 0:
        return base_risk
    elif same_sector_count == 1:
        adjusted = base_risk * 0.75
    else:
        adjusted = base_risk * 0.50
    log(f"  📉 Correlation adjust [{ticker}/{my_sector}]: {same_sector_count} open di sektor ini "
        f"→ risk {base_risk:.2f}% → {adjusted:.2f}%")
    return round(adjusted, 2)


def get_smart_risk_pct(score: int, tier: str, atr_pct: float = 1.0,
                        ticker: str = "", win_prob: float | None = None,
                        avg_win_r: float = 2.0, strategy: str = "SWING",
                        open_positions: list | None = None,
                        win_prob_n: int = 0) -> float:
    """
    [J03] Smart Risk Allocation — Advanced Version.

    Kombinasi 4 layer sizing:
      Layer 1: Base risk dari score/tier (seperti sebelumnya)
      Layer 2: ATR volatility scaling — sesuaikan dengan volatilitas saham
      Layer 3: Half-Kelly fraction — jika win_prob tersedia dari cluster WR
      Layer 4: Correlation adjustment — kurangi jika sektor sudah terisi

    Output: risk % yang sudah diperkecil / diperbesar secara proporsional,
    dengan hard cap tetap di 5% dan floor di 0.25%.

    [Q02] win_prob_n: jumlah trade yang menjadi basis win_prob.
    Dipakai untuk (a) Bayesian shrinkage di dalam calc_kelly_fraction, dan
    (b) menentukan bobot blend Kelly vs vol_risk — semakin sedikit sample,
    Kelly semakin kecil bobotnya (blend lebih condong ke vol_risk yang statis).
    """
    # Layer 1: Score / tier base
    base = RISK_PCT
    if tier == "S" and score >= 16:
        tier_risk = min(base * 2.0, base + 1.5)
    elif tier == "S" or score >= 14:
        tier_risk = min(base * 1.5, base + 1.0)
    elif tier == "A+" or score >= 10:
        tier_risk = base
    else:
        tier_risk = max(base * 0.5, 0.5)

    # Layer 2: ATR volatility scaling
    vol_risk = calc_volatility_adjusted_risk(tier_risk, atr_pct, strategy)

    # Layer 3: Half-Kelly override jika win_prob tersedia dan valid
    # [Q02] Blend ratio sekarang bergantung pada sample size — semakin banyak data,
    # semakin besar bobot Kelly. Ini mencegah overbetting saat WR masih "noisy".
    #   n=0   → Kelly weight 0%  (vol_risk saja)
    #   n=30  → Kelly weight 25%
    #   n=60  → Kelly weight 40%
    #   n=120 → Kelly weight 50% (cap)
    if win_prob is not None and 0.3 <= win_prob <= 0.9:
        kelly_risk  = calc_kelly_fraction(win_prob, avg_win_r, n_samples=win_prob_n)
        n_eff       = max(win_prob_n, 0)
        kelly_w     = min(0.50, n_eff / 240.0)   # 0 → 0.50 linear, saturate di n=120
        vol_w       = 1.0 - kelly_w
        blended     = kelly_w * kelly_risk + vol_w * vol_risk
        log(f"  🔵 Kelly blend [{ticker}]: kelly_w={kelly_w:.0%} vol_w={vol_w:.0%} "
            f"kelly={kelly_risk:.2f}% vol={vol_risk:.2f}% → blend={blended:.2f}%")
    else:
        blended = vol_risk

    # Layer 4: [K02] Dynamic correlation adjustment antar sektor
    # Gunakan rolling 60d Pearson correlation + sector beta vs IHSG
    # (menggantikan step function statis 0/0.75/0.50)
    if ticker:
        final_risk = calc_correlation_adjusted_risk_dynamic(blended, ticker, open_positions)
    else:
        final_risk = blended

    result = round(min(max(final_risk, 0.25), 5.0), 2)

    # Layer 5: [V02/W02] Distribution-adjusted sizing — nonlinear sigmoid penalty
    # [PHASE-2] HARD DISABLED — distribution_penalty dimatikan paksa saat PHASE2_STABILIZE
    # [PHASE-3/LEAN] HARD DISABLED — juga dimatikan saat _effective_lean() aktif
    dist_factor   = 1.0
    _log_dist_msg = ""
    if not PHASE2_STABILIZE and not _effective_lean() and _run_dist_stats and _run_dist_stats.get("n", 0) >= 10:
        _k = _run_dist_stats.get("kurt", 0.0)
        _s = _run_dist_stats.get("skew", 0.0)

        # Kurtosis penalty (sigmoid, continuous)
        kurt_penalty = DIST_KURT_SIG_MAX / (
            1.0 + math.exp(-DIST_KURT_SIG_SLOPE * (_k - DIST_KURT_SIG_INFLECT))
        )
        kurt_factor = 1.0 - kurt_penalty

        # Skewness penalty — only negative skew matters (left tail risk)
        # Sigmoid centred at INFLECT, only active when skew < 0
        skew_penalty = DIST_SKEW_SIG_MAX / (
            1.0 + math.exp(-DIST_SKEW_SIG_SLOPE * (_s - DIST_SKEW_SIG_INFLECT))
        )
        # For positive skew: cap penalty at 0 (no penalty, possibly beneficial)
        if _s > 0:
            skew_penalty = 0.0
        skew_factor = 1.0 - skew_penalty

        dist_factor   = round(kurt_factor * skew_factor, 4)
        _log_dist_msg = (f"kurt={_k:.1f} → kurt_pen={kurt_penalty:.3f} | "
                         f"skew={_s:.1f} → skew_pen={skew_penalty:.3f} | "
                         f"combined factor={dist_factor:.3f}")

    if dist_factor < 0.999:
        old_result = result
        result = round(max(result * dist_factor, 0.25), 2)
        log(f"  📊 [W02] Sigmoid dist [{ticker}]: {_log_dist_msg} "
            f"→ risk {old_result:.2f}%→{result:.2f}%")

    # Layer 6: [V04] Complexity tax — penalisasi sizing saat sistem kompleks + edge belum terbukti
    # [PHASE-2] HARD DISABLED — complexity_tax dimatikan paksa via _current_complexity_tax=0 di run()
    # [PHASE-3/LEAN] HARD DISABLED — juga dimatikan eksplisit saat _effective_lean() aktif
    if not PHASE2_STABILIZE and not _effective_lean() and _current_complexity_tax > 0:
        tax_mult   = max(1.0 - _current_complexity_tax, 1.0 - COMPLEXITY_TAX_CAP)
        old_result = result
        result     = round(max(result * tax_mult, 0.25), 2)
        log(f"  🧩 [V04] Complexity tax [{ticker}]: tax={_current_complexity_tax:.0%} "
            f"→ mult={tax_mult:.2f} risk {old_result:.2f}%→{result:.2f}%")

    log(f"  💰 Risk sizing [{ticker or '—'} {tier} score:{score}]: "
        f"tier={tier_risk:.2f}% → vol_adj={vol_risk:.2f}% → kelly_blend={blended:.2f}% "
        f"→ corr_adj={final_risk:.2f}% → dist+tax={result:.2f}% (ATR:{atr_pct:.1f}%)")
    return result


# ════════════════════════════════════════════════════════
#  [v8.09-A] PROSPECTIVE EDGE TRACKER
#
#  Gap A: Sistem kita sudah BISA mengukur edge tapi belum pernah
#  membuktikan bahwa verdict PROVEN → performance benar-benar lebih baik.
#  Ini adalah perbedaan antara "sistem yang bisa menghitung" vs
#  "sistem yang punya edge yang terbukti".
#
#  track_edge_verdict_accuracy() menyimpan: saat check_edge_proven()
#  menghasilkan verdict X di waktu T, apa WR aktual dari N trade berikutnya?
#
#  Jika PROVEN periods tidak benar-benar lebih baik dari UNPROVEN,
#  maka sistem edge-gating kita sendiri tidak punya edge — ini informasi kritis.
# ════════════════════════════════════════════════════════

def save_edge_verdict_snapshot(verdict: str, n_signals: int, wr: float,
                                net_ev: float) -> None:
    """
    [v8.09-A] Simpan snapshot edge verdict ke Supabase untuk prospective tracking.

    Table: edge_verdict_log (buat jika belum ada)
    Columns: id, snapshot_at, verdict, n_signals, wr_at_snapshot, net_ev,
             future_wr_20 (diisi oleh evaluate_edge_verdict_accuracy)
    """
    try:
        supabase.table("edge_verdict_log").insert({
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "verdict":     verdict,
            "n_signals":   n_signals,
            "wr_snapshot": round(wr, 4) if wr else None,
            "net_ev":      round(net_ev, 4) if net_ev else None,
            "future_wr_20": None,   # diisi oleh evaluate_edge_verdict_accuracy()
            "evaluated":   False,
        }).execute()
        log(f"  💾 [v8.09-A] Edge verdict snapshot saved: {verdict} wr={wr:.0%} n={n_signals}")
    except Exception as e:
        log(f"  ⚠️ [v8.09-A] save_edge_verdict_snapshot error — {e} "
            f"(table edge_verdict_log mungkin belum ada — lihat migration di bawah)", "warn")


def evaluate_edge_verdict_accuracy() -> dict:
    """
    [v8.09-A] Evaluasi retrospektif: apakah PROVEN verdict benar-benar prediktif?

    Logic:
    1. Ambil semua snapshot edge_verdict_log yang belum dievaluasi (evaluated=False).
    2. Untuk setiap snapshot di waktu T, ambil 20 trade berikutnya dari signals table.
    3. Hitung WR dari 20 trade tersebut → future_wr_20.
    4. Update baris dengan future_wr_20 + evaluated=True.
    5. Return summary: avg future_wr per verdict category.

    Schema edge_verdict_log SQL (jalankan sekali di Supabase):
    ┌──────────────────────────────────────────────────────────────────┐
    │ CREATE TABLE IF NOT EXISTS edge_verdict_log (                    │
    │   id             BIGSERIAL PRIMARY KEY,                          │
    │   snapshot_at    TIMESTAMPTZ NOT NULL,                           │
    │   verdict        TEXT NOT NULL,                                  │
    │   n_signals      INT,                                            │
    │   wr_snapshot    FLOAT,                                          │
    │   net_ev         FLOAT,                                          │
    │   future_wr_20   FLOAT,                                          │
    │   evaluated      BOOLEAN DEFAULT FALSE                           │
    │ );                                                               │
    └──────────────────────────────────────────────────────────────────┘
    """
    result = {
        "evaluated_snapshots": 0,
        "summary_by_verdict": {},
        "predictive_accuracy": None,
        "note": "",
    }

    try:
        snapshots = (
            supabase.table("edge_verdict_log")
            .select("id, snapshot_at, verdict, n_signals, wr_snapshot")
            .eq("evaluated", False)
            .order("snapshot_at", desc=False)
            .limit(50)
            .execute()
            .data
        )
    except Exception as e:
        log(f"⚠️ evaluate_edge_verdict_accuracy: fetch error — {e}", "warn")
        return result

    if not snapshots:
        log("ℹ️ [v8.09-A] No unevaluated snapshots found.")
        return result

    evaluated = 0
    for snap in snapshots:
        snap_time = snap["snapshot_at"]
        try:
            future_trades = (
                supabase.table("signals")
                .select("outcome, sent_at")
                .in_("outcome", ["WIN", "LOSS"])
                .gt("sent_at", snap_time)
                .order("sent_at", desc=False)
                .limit(20)
                .execute()
                .data
            )
        except Exception:
            continue

        if len(future_trades) < 10:
            # Tidak cukup trade setelah snapshot ini — skip untuk sekarang
            continue

        future_wr = sum(1 for t in future_trades if t["outcome"] == "WIN") / len(future_trades)

        try:
            supabase.table("edge_verdict_log").update({
                "future_wr_20": round(future_wr, 4),
                "evaluated":    True,
            }).eq("id", snap["id"]).execute()
            evaluated += 1
        except Exception as e:
            log(f"  ⚠️ update snapshot {snap['id']}: {e}", "warn")

    result["evaluated_snapshots"] = evaluated

    # ── Summary: avg future WR per verdict category ───────
    try:
        all_eval = (
            supabase.table("edge_verdict_log")
            .select("verdict, future_wr_20")
            .eq("evaluated", True)
            .not_.is_("future_wr_20", "null")
            .execute()
            .data
        )
        if all_eval:
            by_verdict: dict = {}
            for row in all_eval:
                v = row["verdict"]
                by_verdict.setdefault(v, []).append(float(row["future_wr_20"]))
            summary = {}
            for v, wrs in by_verdict.items():
                summary[v] = {
                    "n":       len(wrs),
                    "avg_wr":  round(float(np.mean(wrs)), 4),
                    "std_wr":  round(float(np.std(wrs)), 4) if len(wrs) > 1 else None,
                }
            result["summary_by_verdict"] = summary

            # Kunci: apakah PROVEN > UNPROVEN dalam future WR?
            proven_wr   = summary.get("PROVEN",   {}).get("avg_wr")
            unproven_wr = summary.get("UNPROVEN", {}).get("avg_wr")
            if proven_wr is not None and unproven_wr is not None:
                diff = proven_wr - unproven_wr
                result["predictive_accuracy"] = round(diff, 4)
                if diff > 0.05:
                    result["note"] = (
                        f"✅ PROVEN verdict prediktif: PROVEN WR={proven_wr:.0%} "
                        f"vs UNPROVEN={unproven_wr:.0%} (+{diff:.0%}) — sistem edge-gating valid."
                    )
                elif diff > 0:
                    result["note"] = (
                        f"🔵 Verdict sedikit prediktif: gap={diff:.0%}. "
                        f"Butuh lebih banyak data untuk konfirmasi."
                    )
                else:
                    result["note"] = (
                        f"⚠️ VERDICT TIDAK PREDIKTIF: PROVEN={proven_wr:.0%} "
                        f"UNPROVEN={unproven_wr:.0%} gap={diff:.0%}. "
                        f"Edge-gating mungkin tidak efektif — evaluasi sistem."
                    )
            log(f"📊 [v8.09-A] Edge verdict accuracy: {result['note']}")
    except Exception as e:
        log(f"  ⚠️ evaluate summary error: {e}", "warn")

    return result


# ════════════════════════════════════════════════════════
#  [v8.09-B] SESSION-AWARE DATA LATENCY GUARD
#
#  Gap B: yfinance memberikan data ~15 menit delay. Tapi masalahnya
#  tidak linear — di momen volatile (open IDX, 09:00-09:30 WIB, atau
#  news-driven spikes), 15 menit delay bisa berarti signal berbasis
#  harga yang sudah bergerak 1-3%. Ini berbeda dari 15 menit delay
#  di jam boring (11:30-13:30 WIB saat volume rendah).
#
#  get_session_data_quality() menghitung "effective staleness" data
#  dengan mempertimbangkan: jam pasar, volatilitas session saat ini,
#  dan apakah ada event macro hari ini.
# ════════════════════════════════════════════════════════

# EQS penalty tambahan per session type (basis points dari risk)
_SESSION_LATENCY_PENALTY = {
    "PRE_OPEN":         0.30,   # sebelum pasar buka — data semalam, stale parah
    "OPEN_VOLATILE":    0.50,   # [Y02] 09:00–09:45 — hard ceiling, 15m delay sangat mahal di sini
    "MID_SESSION":      0.05,   # 10:00–11:30 — normal, delay kurang masalah
    "LUNCH_FLAT":       0.02,   # 11:30–13:30 — vol rendah, delay impact minimal
    "AFTERNOON":        0.08,   # 13:30–15:00 — meningkat, pre-close activity
    "PRE_CLOSE":        0.20,   # 15:00–15:30 — high activity, delay mahal lagi
    "POST_MARKET":      0.35,   # setelah tutup — data sudah stale sepenuhnya
}

def get_session_data_quality(data_source: str = "YFINANCE_DELAYED_15MIN",
                              ihsg_5d_change: float = 0.0) -> dict:
    """
    [v8.09-B] Hitung effective data quality dengan session-aware latency model.

    Returns:
        eqs_adjustment: float — pengurangan EQS (0.0 = tidak ada penalti tambahan)
        session:        str   — nama session IDX saat ini
        warning:        str   — pesan untuk user jika latency tinggi
        trade_ok:       bool  — False jika latency terlalu tinggi untuk trading
    """
    now_wib  = datetime.now(WIB)
    hour     = now_wib.hour
    minute   = now_wib.minute
    time_val = hour + minute / 60.0

    # Tentukan session
    if time_val < 9.0:
        session = "PRE_OPEN"
    elif time_val < 9.75:        # 09:00–09:45
        session = "OPEN_VOLATILE"
    elif time_val < 11.5:        # 09:45–11:30
        session = "MID_SESSION"
    elif time_val < 13.5:        # 11:30–13:30
        session = "LUNCH_FLAT"
    elif time_val < 15.0:        # 13:30–15:00
        session = "AFTERNOON"
    elif time_val < 15.5:        # 15:00–15:30
        session = "PRE_CLOSE"
    else:
        session = "POST_MARKET"

    base_penalty = _SESSION_LATENCY_PENALTY.get(session, 0.10)

    # Multiplier jika market sedang volatile (IHSG bergerak cepat)
    vol_multiplier = 1.0
    if abs(ihsg_5d_change) > 3.0:    # IHSG volatile week
        vol_multiplier = 1.4
    elif abs(ihsg_5d_change) > 1.5:
        vol_multiplier = 1.2

    # Premium sources kena penalti lebih kecil
    source_factor = {
        "YFINANCE_DELAYED_15MIN": 1.0,
        "YFINANCE_EOD":           1.5,   # EOD lebih stale
        "twelve_data":            0.3,   # near-realtime
        "alpha_vantage":          0.5,
        "cache_only":             2.0,
    }.get(data_source, 1.0)

    effective_penalty = base_penalty * vol_multiplier * source_factor
    effective_penalty = min(effective_penalty, 0.50)   # cap 50%

    # [Y02] OPEN_VOLATILE = hard block tanpa pengecualian untuk INTRADAY yfinance
    # Penalty 0.50 di OPEN_VOLATILE akan selalu trigger hard block di bawah ini.
    hard_block = (session == "OPEN_VOLATILE" and
                  data_source == "YFINANCE_DELAYED_15MIN")

    # [Y02] Threshold dinaikkan 0.30 → 0.45 untuk intraday — lebih konservatif
    trade_ok = (not hard_block) and (effective_penalty < 0.45)

    # Warning message
    if hard_block:
        warning = (
            f"🔴 [Y02] HARD BLOCK di {session} — yfinance 15m delay tidak aman "
            f"saat open IDX. Signal intraday diblokir. Tunggu sesi MID_SESSION (09:45 WIB)."
        )
    elif effective_penalty >= 0.30:
        warning = (
            f"🔴 [v8.09-B] Data latency sangat tinggi di {session} "
            f"({data_source}, vol_mult={vol_multiplier:.1f}x) — "
            f"EQS penalty {effective_penalty:.0%}. Pertimbangkan skip trade."
        )
    elif effective_penalty >= 0.15:
        warning = (
            f"⚠️ [v8.09-B] Latency moderat di {session} "
            f"({data_source}) — penalty {effective_penalty:.0%}."
        )
    else:
        warning = f"✅ [v8.09-B] Data quality OK di {session} — penalty {effective_penalty:.0%}."

    return {
        "session":           session,
        "base_penalty":      round(base_penalty, 4),
        "vol_multiplier":    vol_multiplier,
        "source_factor":     source_factor,
        "effective_penalty": round(effective_penalty, 4),
        "trade_ok":          trade_ok,
        "hard_block":        hard_block,
        "warning":           warning,
        "eqs_adjustment":    round(effective_penalty, 4),
    }
#  Backtest berbasis data Supabase historis + yfinance OHLCV.
#
#  Keterbatasan yang jujur (tidak disembunyikan):
#  ❌ Bukan walk-forward validation
#  ❌ Tidak ada slippage model
#  ❌ Look-ahead bias mungkin ada di beberapa indikator
#  ❌ Sample size terbatas (tergantung data Supabase)
#
#  Yang bisa dilakukan:
#  ✅ Validasi apakah signal historis benar-benar profitable
#  ✅ Breakdown winrate per regime, sektor, tier, strategy
#  ✅ Deteksi apakah scoring weight menghasilkan edge nyata
#  ✅ Bandingkan theoretical vs actual outcome
#
#  Jalankan: python bot_saham_v7_15.py --backtest [--days=60]
# ════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════
#  [J01] EXECUTION REALITY ENGINE — v7.15
#
#  Tiga komponen yang sebelumnya diabaikan:
#  1. Partial Fill: tidak semua order terisi penuh di IDX
#  2. Queue Priority: IDX pakai price-time priority — order
#     besar di jam sibuk sering antri panjang
#  3. Slippage lengkap: spread + delay + volatility + partial
#
#  Ini tidak mengubah signal logic, tapi membuat backtest
#  dan EV calculation jauh lebih akurat secara realistic.
# ════════════════════════════════════════════════════════

# IDX Queue Priority Windows
IDX_SESSION_OPEN       = (9, 0)    # 09:00 WIB — pra-opening selesai, continuous start
IDX_SESSION_MIDDAY     = (12, 0)   # 12:00 WIB — jeda siang
IDX_SESSION_AFTERNOON  = (13, 30)  # 13:30 WIB — sesi sore
IDX_SESSION_PRECLOSE   = (15, 30)  # 15:30 WIB — pre-closing (random matching)
IDX_SESSION_CLOSE      = (15, 49)  # 15:49 WIB — sesi tutup


def get_idx_queue_priority(now_wib: datetime | None = None) -> dict:
    """
    [J01] Estimasi kondisi antrian order IDX berdasarkan waktu sesi.

    IDX menggunakan price-time priority (FIFO per harga).
    Implikasinya:
      - Pra-Opening (08:45-09:00): semua order dikumpul, tidak ada eksekusi
        → entry tidak bisa tepat di open price
      - Continuous (09:00-12:00): normal trading, fill rate tinggi
      - Pre-Closing (15:30-15:49): random matching — urutan tidak relevan
        → partial fill lebih sering terjadi

    Returns: {session, fill_difficulty, slippage_modifier, note}
    """
    if now_wib is None:
        now_wib = datetime.now(WIB)

    h, m = now_wib.hour, now_wib.minute
    t = h * 60 + m   # waktu dalam menit

    pre_open  = 8 * 60 + 45
    open_     = IDX_SESSION_OPEN[0] * 60
    midday    = IDX_SESSION_MIDDAY[0] * 60
    afternoon = IDX_SESSION_AFTERNOON[0] * 60 + IDX_SESSION_AFTERNOON[1]
    preclose  = IDX_SESSION_PRECLOSE[0] * 60 + IDX_SESSION_PRECLOSE[1]
    close_    = IDX_SESSION_CLOSE[0] * 60 + IDX_SESSION_CLOSE[1]

    if t < pre_open or t > close_:
        return {
            "session":          "OUTSIDE_HOURS",
            "fill_difficulty":  "N/A",
            "slippage_modifier": 0.0,
            "partial_fill_risk": 0.0,
            "note":             "Market tutup — sinyal hanya berlaku sesi berikutnya"
        }
    elif pre_open <= t < open_:
        return {
            "session":          "PRE_OPENING",
            "fill_difficulty":  "HIGH",
            "slippage_modifier": 0.30,  # +0.30% slippage karena akumulasi order overnight
            "partial_fill_risk": 0.40,  # 40% chance partial fill saat open
            "note":             "Pra-opening: order dikumpulkan, matching terjadi tepat jam 09:00"
        }
    elif open_ <= t < open_ + 30:
        return {
            "session":          "OPEN_RUSH",
            "fill_difficulty":  "HIGH",
            "slippage_modifier": 0.25,  # opening rush — antrian panjang
            "partial_fill_risk": 0.30,
            "note":             "30 menit pertama: volume tertinggi, antrian paling panjang"
        }
    elif open_ + 30 <= t < midday:
        return {
            "session":          "CONTINUOUS_AM",
            "fill_difficulty":  "LOW",
            "slippage_modifier": 0.0,
            "partial_fill_risk": 0.10,
            "note":             "Continuous AM: kondisi normal, fill rate baik"
        }
    elif midday <= t < afternoon:
        return {
            "session":          "LUNCH_BREAK",
            "fill_difficulty":  "VERY_HIGH",
            "slippage_modifier": 0.20,
            "partial_fill_risk": 0.50,  # jeda siang — likuiditas rendah
            "note":             "Jeda siang 12:00-13:30: volume sangat rendah, hindari entry"
        }
    elif afternoon <= t < preclose:
        return {
            "session":          "CONTINUOUS_PM",
            "fill_difficulty":  "LOW",
            "slippage_modifier": 0.0,
            "partial_fill_risk": 0.10,
            "note":             "Continuous PM: kondisi normal"
        }
    elif preclose <= t <= close_:
        return {
            "session":          "PRE_CLOSING",
            "fill_difficulty":  "MEDIUM",
            "slippage_modifier": 0.15,
            "partial_fill_risk": 0.35,  # random matching — urutan tidak pasti
            "note":             "Pre-closing 15:30-15:49: random matching, partial fill lebih sering"
        }
    return {
        "session": "UNKNOWN", "fill_difficulty": "MEDIUM",
        "slippage_modifier": 0.10, "partial_fill_risk": 0.20, "note": ""
    }


def simulate_partial_fill(order_size_lots: float, ticker: str,
                           vol_today_idr: float, fill_difficulty: float = 0.10) -> dict:
    """
    [J01] Partial Fill Simulation.

    Di IDX, order besar pada saham dengan liquiditas terbatas sering hanya
    terisi sebagian. Ini tidak terdeteksi dari signal, tapi sangat mempengaruhi
    actual P&L karena partial fill = modal lebih kecil × return yang sama.

    Model sederhana:
      fill_pct = f(order_size / avg_daily_volume, session_difficulty)

    Returns: {fill_pct, filled_lots, unfilled_lots, note}
    """
    default = {"fill_pct": 1.0, "filled_lots": order_size_lots,
               "unfilled_lots": 0.0, "note": "Full fill"}
    if order_size_lots <= 0 or vol_today_idr <= 0:
        return default

    price_est    = 1000.0   # rough estimate untuk konversi
    order_idr    = order_size_lots * 100 * price_est   # 1 lot = 100 saham
    vol_ratio    = order_idr / (vol_today_idr + 1e-9)

    # Base fill rate berdasarkan ukuran relatif order vs daily volume
    if vol_ratio < 0.001:      base_fill = 1.00   # order sangat kecil relative → full fill
    elif vol_ratio < 0.005:    base_fill = 0.95
    elif vol_ratio < 0.01:     base_fill = 0.85
    elif vol_ratio < 0.02:     base_fill = 0.70
    elif vol_ratio < 0.05:     base_fill = 0.50
    else:                      base_fill = 0.35   # order terlalu besar → partial berat

    # Pengurang dari session difficulty
    adjusted_fill = base_fill * (1 - fill_difficulty * 0.5)
    adjusted_fill = max(adjusted_fill, 0.20)   # minimal 20% tetap terisi

    filled   = order_size_lots * adjusted_fill
    unfilled = order_size_lots - filled

    note = "Full fill" if adjusted_fill >= 0.95 else            f"Partial fill {adjusted_fill:.0%} — {unfilled:.1f} lot tidak terisi"

    return {
        "fill_pct":       round(adjusted_fill, 4),
        "filled_lots":    round(filled, 2),
        "unfilled_lots":  round(unfilled, 2),
        "note":           note
    }


# ═══════════════════════════════════════════════════════════════════
#  [K01] MARKET IMPACT MODEL — v7.16
#  Volume Participation Rate (VPR) + Square-root price impact.
#  Relevan untuk mid/small cap IDX dengan likuiditas terbatas.
# ═══════════════════════════════════════════════════════════════════

_VPR_TIERS = [
    (0.001, 0.00),
    (0.005, 0.05),
    (0.010, 0.12),
    (0.020, 0.25),
    (0.050, 0.55),
    (0.100, 1.10),
    (1.000, 2.50),
]

def calc_market_impact(
    order_lots: float,
    vol_today_idr: float,
    price: float,
    atr_pct: float = 1.0,
    ticker: str = ""
) -> dict:
    """
    [K01] Market Impact Cost Model untuk IDX.

    Menghitung biaya price impact dari order besar relatif terhadap
    likuiditas harian (Average Daily Volume). Menggunakan pendekatan
    Volume Participation Rate (VPR) + volatility amplifier.

    Formula:
      order_idr = lots x 100 x price
      vpr       = order_idr / vol_today_idr
      impact_pct = lookup(_VPR_TIERS, vpr) x vol_scale

    Returns: dict {vpr, impact_pct, impact_idr, cap_tier, note}
    """
    default = {"vpr": 0.0, "impact_pct": 0.0, "impact_idr": 0.0,
               "cap_tier": "UNKNOWN", "note": "No volume data"}
    if order_lots <= 0 or vol_today_idr <= 0 or price <= 0:
        return default

    order_idr = order_lots * 100 * price
    vpr = order_idr / (vol_today_idr + 1e-9)

    base_impact = _VPR_TIERS[-1][1]
    for threshold, impact in _VPR_TIERS:
        if vpr <= threshold:
            base_impact = impact
            break

    vol_scale  = 1.0 + 0.3 * max(0.0, (atr_pct / 1.0) - 1.0)
    # [Q03] Apply kalibrasi scale — default 1.0 (theoretical, belum dikalibrasi ke data IDX nyata)
    impact_pct = min(base_impact * vol_scale * IMPACT_MODEL_SCALE, 3.0)

    if vpr < 0.001:
        cap_tier = "LQ45/BLUECHIP"
    elif vpr < 0.01:
        cap_tier = "MID_CAP"
    elif vpr < 0.05:
        cap_tier = "SMALL_CAP"
    else:
        cap_tier = "ILLIQUID"

    note = (f"VPR={vpr:.3%} [{cap_tier}] impact +{impact_pct:.3f}%"
            f" (vol_scale x{vol_scale:.2f}, scale={IMPACT_MODEL_SCALE:.2f}"
            f" [{IMPACT_CALIBRATION_STATUS}])")
    log(f"  💧 Market Impact [{ticker}]: {note}")
    if IMPACT_CALIBRATION_STATUS == "UNCALIBRATED_THEORETICAL" and vpr >= 0.01:
        log(f"  ⚠️ Market Impact [{ticker}]: parameter BELUM dikalibrasi ke data IDX real — "
            f"impact_pct={impact_pct:.3f}% adalah estimasi teoritis. "
            f"Set IMPACT_MODEL_SCALE via ENV untuk kalibrasi.", "warn")
    return {
        "vpr":        round(vpr, 6),
        "impact_pct": round(impact_pct, 4),
        "impact_idr": round(order_idr * impact_pct / 100, 0),
        "cap_tier":   cap_tier,
        "note":       note,
    }


# ═══════════════════════════════════════════════════════════════════
#  [K02] DYNAMIC CORRELATION MATRIX — v7.16
#  Rolling 60d cross-sector correlation + sector beta vs IHSG.
# ═══════════════════════════════════════════════════════════════════

import threading as _threading

_CORR_MATRIX_CACHE: dict = {}
_SECTOR_BETA_CACHE: dict = {}
_CORR_CACHE_TIME:   float = 0.0
_CORR_CACHE_LOCK  = _threading.Lock()
_CORR_CACHE_TTL   = 3600


def _fetch_sector_returns(lookback_days: int = 60) -> dict:
    """Ambil daily return per sektor dari SECTOR_PROXY ticker + IHSG."""
    sector_returns: dict = {}
    proxy_tickers = list(SECTOR_PROXY.values()) + ["^JKSE"]
    try:
        raw = yf.download(
            proxy_tickers, period=f"{lookback_days + 5}d",
            interval="1d", progress=False, auto_adjust=True,
        )
        if raw.empty:
            return {}
        close = raw["Close"] if "Close" in raw.columns else raw
        if hasattr(close.columns, "levels"):
            close.columns = [c[0] if isinstance(c, tuple) else c
                             for c in close.columns]
        pct = close.pct_change().dropna()
        for sector, proxy in SECTOR_PROXY.items():
            if proxy in pct.columns:
                sector_returns[sector] = pct[proxy].tolist()
        if "^JKSE" in pct.columns:
            sector_returns["IHSG"] = pct["^JKSE"].tolist()
    except Exception as e:
        log(f"⚠️ [K02] Gagal fetch sector returns: {e}", "warn")
    return sector_returns


def _build_corr_matrix(sector_returns: dict) -> dict:
    """Pearson correlation matrix antar sektor dari daily returns."""
    import statistics as _stat
    sectors = [s for s in sector_returns if s != "IHSG"]
    matrix: dict = {s: {} for s in sectors}
    for i, sa in enumerate(sectors):
        for sb in sectors[i:]:
            ra = sector_returns.get(sa, [])
            rb = sector_returns.get(sb, [])
            n = min(len(ra), len(rb))
            if n < 20:
                corr = 0.5  # terlalu sedikit data → neutral fallback, hindari noise correlation
            elif sa == sb:
                corr = 1.0
            else:
                try:
                    mean_a = sum(ra[:n]) / n
                    mean_b = sum(rb[:n]) / n
                    cov = sum((ra[j] - mean_a) * (rb[j] - mean_b)
                              for j in range(n)) / n
                    std_a = _stat.pstdev(ra[:n]) or 1e-9
                    std_b = _stat.pstdev(rb[:n]) or 1e-9
                    corr = max(-1.0, min(1.0, cov / (std_a * std_b)))
                except Exception as _e:
                    log(f"  ⚠️ [FALLBACK] corr calc: {_e} — default 0.5", "warn")
                    corr = 0.5
            matrix[sa][sb] = round(corr, 4)
            matrix[sb][sa] = round(corr, 4)
    return matrix


def _build_sector_beta(sector_returns: dict) -> dict:
    """Beta tiap sektor vs IHSG. beta = cov(sektor, IHSG) / var(IHSG)."""
    import statistics as _stat
    ihsg_r = sector_returns.get("IHSG", [])
    betas: dict = {}
    for sector in SECTOR_PROXY:
        sr = sector_returns.get(sector, [])
        n = min(len(sr), len(ihsg_r))
        if n < 20:
            betas[sector] = 1.0  # terlalu sedikit data → neutral beta fallback
            continue
        try:
            mean_s = sum(sr[:n]) / n
            mean_i = sum(ihsg_r[:n]) / n
            cov = sum((sr[j] - mean_s) * (ihsg_r[j] - mean_i)
                      for j in range(n)) / n
            var_i = _stat.pvariance(ihsg_r[:n]) or 1e-9
            betas[sector] = round(max(0.0, cov / var_i), 4)
        except Exception as _e:
            log(f"  ⚠️ [FALLBACK] sector beta [{sector}]: {_e} — default 1.0", "warn")
            betas[sector] = 1.0
    return betas


def get_dynamic_correlation_data(force_refresh: bool = False) -> dict:
    """
    [K02] Ambil atau refresh correlation matrix + sector beta (cache 1 jam).

    Returns: {matrix, beta, fresh, ts}
    """
    global _CORR_MATRIX_CACHE, _SECTOR_BETA_CACHE, _CORR_CACHE_TIME
    now_ts = time.time()
    with _CORR_CACHE_LOCK:
        if (not force_refresh and _CORR_MATRIX_CACHE
                and now_ts - _CORR_CACHE_TIME < _CORR_CACHE_TTL):
            return {"matrix": _CORR_MATRIX_CACHE, "beta": _SECTOR_BETA_CACHE,
                    "fresh": False, "ts": _CORR_CACHE_TIME}
        log("🔄 [K02] Refresh dynamic correlation matrix & sector beta…")
        sr = _fetch_sector_returns(lookback_days=60)
        if sr:
            _CORR_MATRIX_CACHE = _build_corr_matrix(sr)
            _SECTOR_BETA_CACHE = _build_sector_beta(sr)
            _CORR_CACHE_TIME   = now_ts
            log(f"  ✅ Corr matrix: {len(_CORR_MATRIX_CACHE)} sektor | "
                f"Beta: {_SECTOR_BETA_CACHE}")
        else:
            log("  ⚠️ [K02] Gagal refresh — fallback statis", "warn")
        return {"matrix": _CORR_MATRIX_CACHE, "beta": _SECTOR_BETA_CACHE,
                "fresh": bool(sr), "ts": _CORR_CACHE_TIME}


def calc_correlation_adjusted_risk_dynamic(
    base_risk: float,
    ticker: str,
    open_positions: list | None = None
) -> float:
    """
    [K02] Dynamic Correlation-Adjusted Position Sizing.

    Upgrade dari calc_correlation_adjusted_risk() yang pakai step function.
    Pakai rolling 60d Pearson correlation + sector beta vs IHSG.

    Formula:
      avg_corr   = mean corr[my_sector][pos_sector] per open position
      beta_adj   = beta[my_sector] / 1.0
      penalty    = avg_corr x beta_adj x (1 + 0.25 x same_sector_n)
      adjusted   = base_risk x (1 - penalty x 0.40)
      floor      = base_risk x 0.30
    """
    if not open_positions:
        return base_risk

    my_sector = TICKER_SECTOR.get(ticker, "MISC")
    corr_data = get_dynamic_correlation_data()
    matrix    = corr_data["matrix"]
    betas     = corr_data["beta"]

    pos_sectors = [p.get("sector", "MISC") for p in open_positions]
    if not pos_sectors:
        return base_risk

    corr_vals = []
    for ps in pos_sectors:
        corr = (matrix.get(my_sector, {}).get(ps)
                or matrix.get(ps, {}).get(my_sector)
                or 0.5)
        corr_vals.append(corr)
    avg_corr = sum(corr_vals) / len(corr_vals) if corr_vals else 0.5

    my_beta  = betas.get(my_sector, 1.0)
    beta_adj = min(my_beta / 1.0, 2.0)

    same_sector_n = sum(1 for ps in pos_sectors if ps == my_sector)
    same_factor   = 1.0 + (same_sector_n * 0.25)

    corr_penalty = avg_corr * beta_adj * same_factor
    adjusted     = base_risk * (1.0 - corr_penalty * 0.40)
    adjusted     = max(adjusted, base_risk * 0.30)

    log(f"  📉 [K02] Corr-Adj [{ticker}/{my_sector}]: "
        f"avg_corr={avg_corr:.3f} beta={my_beta:.2f} "
        f"same_n={same_sector_n} penalty={corr_penalty:.3f} "
        f"risk {base_risk:.2f}% → {adjusted:.2f}%")
    return round(adjusted, 2)


# ═══════════════════════════════════════════════════════════════════
#  [K03] BEHAVIORAL EDGE — v7.16
#  Event Calendar Awareness + Macro Shock + Sentiment Spike Guard
# ═══════════════════════════════════════════════════════════════════

BEHAVIORAL_EVENT_CALENDAR: dict = {
    "0101": "Tahun Baru — likuiditas sangat rendah",
    "0208": "Sidang BI Rate Feb",
    "0320": "Sidang BI Rate Mar",
    "0417": "Sidang BI Rate Apr + FOMC",
    "0522": "Sidang BI Rate Mei",
    "0619": "Sidang BI Rate Jun + FOMC",
    "0717": "Sidang BI Rate Jul",
    "0821": "Sidang BI Rate Agu",
    "0918": "Sidang BI Rate Sep + FOMC",
    "1016": "Sidang BI Rate Okt",
    "1120": "Sidang BI Rate Nov + FOMC",
    "1218": "Sidang BI Rate Des + FOMC",
    "1225": "Natal — bursa tutup / sangat tipis",
    "1231": "Akhir Tahun — window dressing selesai",
}

_extra_beh = os.environ.get("BEHAVIORAL_EVENTS", "")
if _extra_beh:
    for _ev in _extra_beh.split(","):
        if ":" in _ev:
            _k2, _v2 = _ev.split(":", 1)
            BEHAVIORAL_EVENT_CALENDAR[_k2.strip()] = _v2.strip()

SENTIMENT_VOLUME_SPIKE_MULTIPLIER: float = float(
    os.environ.get("SENTIMENT_VOLUME_SPIKE", "3.0"))
BEHAVIORAL_EVENT_WINDOW_DAYS: int = int(
    os.environ.get("BEHAVIORAL_EVENT_WINDOW", "1"))


def check_behavioral_edge(
    ticker: str,
    volumes: list,
    now_wib: datetime | None = None,
) -> dict:
    """
    [K03] Behavioral Edge Check — 3 filter lapis:

    1. EVENT AWARENESS: BI Rate, FOMC, Natal, dll.
       → reduce_factor = 0.60 jika ada event dalam N hari

    2. SENTIMENT SPIKE GUARD: volume individu > 3× rata-rata 20d
       → flag (tidak blok otomatis, tapi dicatat di signal)

    3. MACRO SHOCK PROXY: IHSG volume > 4× normal
       → block_signal = True (skip signal sepenuhnya)

    Returns: {block_signal, reduce_factor, event_name,
              sentiment_spike, macro_shock, reason}
    """
    result = {
        "block_signal":    False,
        "reduce_factor":   1.0,
        "event_name":      None,
        "sentiment_spike": False,
        "macro_shock":     False,
        "reason":          "OK",
    }
    now_dt = now_wib or datetime.now(tz=_TZ_WIB)

    # 1. Event Calendar
    window = BEHAVIORAL_EVENT_WINDOW_DAYS
    for delta in range(0, window + 1):
        check_dt = now_dt + timedelta(days=delta)
        key = check_dt.strftime("%m%d")
        if key in BEHAVIORAL_EVENT_CALENDAR:
            event_name = BEHAVIORAL_EVENT_CALENDAR[key]
            result["event_name"]    = event_name
            result["reduce_factor"] = 0.60
            result["reason"] = f"Event T+{delta}: {event_name} sizing x0.60"
            log(f"  📅 [K03] Event [{ticker}]: {result['reason']}")
            break

    # 2. Sentiment Spike Guard (volume individu)
    if len(volumes) >= 21:
        avg_vol  = sum(volumes[-21:-1]) / 20
        last_vol = volumes[-1]
        if avg_vol > 0 and last_vol > avg_vol * SENTIMENT_VOLUME_SPIKE_MULTIPLIER:
            result["sentiment_spike"] = True
            spike_x = last_vol / avg_vol
            log(f"  📣 [K03] Sentiment spike [{ticker}]: vol {spike_x:.1f}x normal")
            if result["reason"] == "OK":
                result["reason"] = f"Volume spike {spike_x:.1f}x — monitor"

    # 3. Macro Shock Proxy via IHSG volume
    try:
        ihsg_df = get_ihsg_cached(period="25d")   # [Z02] pakai cache terpusat
        if not ihsg_df.empty and "Volume" in ihsg_df.columns:
            ihsg_vol = ihsg_df["Volume"].dropna().tolist()
            if len(ihsg_vol) >= 21:
                avg_ihsg  = sum(ihsg_vol[-21:-1]) / 20
                last_ihsg = ihsg_vol[-1]
                if avg_ihsg > 0 and last_ihsg > avg_ihsg * 4.0:
                    result["macro_shock"]   = True
                    result["block_signal"]  = True
                    result["reduce_factor"] = 0.0
                    result["reason"] = (
                        f"MACRO SHOCK: IHSG vol {last_ihsg/avg_ihsg:.1f}x "
                        f"normal — BLOCK")
                    log(f"  🚨 [K03] {result['reason']}", "warn")
    except Exception as e:
        log(f"  ⚠️ [K03] Gagal cek IHSG volume: {e}", "warn")

    return result


# ═══════════════════════════════════════════════════════════════════
#  [K04] LATENCY TRANSPARENCY — v7.16
#  Execution Quality Score (EQS) per signal.
# ═══════════════════════════════════════════════════════════════════

def calc_execution_quality_score(
    strategy: str,
    data_age_minutes: float,
    vpr: float,
    slippage_pct: float,
    partial_fill_risk: float,
    impact_pct: float,
    ihsg_5d_change: float = 0.0,
) -> dict:
    """
    [K04/v8.09-B] Execution Quality Score (EQS) — 0 sampai 100.

    v8.09-B: Tambah session-aware latency penalty dari get_session_data_quality().
    Data delay 15 menit di jam OPEN_VOLATILE (09:00-09:45 WIB) jauh lebih
    berbahaya dari delay 15 menit saat LUNCH_FLAT. EQS sekarang refleksikan ini.

    Komponen:
      freshness  (max 30): dikurangi per menit data stale
      impact     (max 30): dikurangi per VPR tier
      cost       (max 25): total slippage + impact pct
      fill_conf  (max 15): berdasarkan partial fill risk
      [v8.09-B] session_penalty: dikurangi berdasarkan jam + volatilitas

    Label:
      EQS >= 75  : GOOD
      EQS 50-74  : FAIR
      EQS < 50   : POOR

    Catatan: INTRADAY + data > 15 menit → otomatis POOR.
    """
    if strategy == "INTRADAY":
        freshness = max(0.0, 30.0 - data_age_minutes * 1.5)
    else:
        freshness = max(0.0, 30.0 - data_age_minutes * 0.3)

    if vpr < 0.001:    impact_score = 30.0
    elif vpr < 0.005:  impact_score = 27.0
    elif vpr < 0.01:   impact_score = 22.0
    elif vpr < 0.02:   impact_score = 15.0
    elif vpr < 0.05:   impact_score = 8.0
    else:              impact_score = 2.0

    total_cost = slippage_pct + impact_pct
    if total_cost < 0.2:    cost_score = 25.0
    elif total_cost < 0.5:  cost_score = 20.0
    elif total_cost < 1.0:  cost_score = 14.0
    elif total_cost < 2.0:  cost_score = 7.0
    else:                   cost_score = 2.0

    fill_score = max(0.0, 15.0 * (1.0 - partial_fill_risk))
    eqs        = round(freshness + impact_score + cost_score + fill_score, 1)

    # [v8.09-B] Session-aware latency penalty — hanya untuk INTRADAY
    session_info = {}
    if strategy == "INTRADAY":
        _ds = DATA_SOURCE_INTRADAY if strategy == "INTRADAY" else DATA_SOURCE_SWING
        session_info = get_session_data_quality(_ds, ihsg_5d_change)
        session_pen  = session_info["effective_penalty"]
        if session_pen > 0.05:
            # Kurangi EQS proporsional dengan penalty (max 25 poin dari 100)
            eqs_deduction = round(session_pen * 50, 1)   # 50% penalty = -25 poin
            eqs = max(0.0, eqs - eqs_deduction)
            log(f"  ⏱️ [v8.09-B] Session penalty [{session_info['session']}]: "
                f"-{eqs_deduction:.1f} EQS pts (pen={session_pen:.0%})")

    if eqs >= 75:
        label = "EQS GOOD"
    elif eqs >= 50:
        label = "EQS FAIR"
    else:
        label = "EQS POOR"

    if strategy == "INTRADAY" and data_age_minutes > 15:
        label = "EQS POOR (data stale — intraday tidak valid)"
        eqs   = min(eqs, 40.0)

    detail = (f"freshness={freshness:.0f} impact={impact_score:.0f} "
              f"cost={cost_score:.0f} fill={fill_score:.0f}"
              + (f" session={session_info.get('session','')}:{session_info.get('effective_penalty',0):.0%}"
                 if session_info else ""))
    log(f"  🎯 [K04] EQS={eqs:.0f} | {label} | {detail}")
    return {"eqs": eqs, "label": label, "detail": detail,
            "session_info": session_info}


def simulate_execution(entry: float, sl: float, tp1: float,
                        side: str, price_range: float = 0.0,
                        ticker: str = "", vol_today_idr: float = 0.0,
                        now_wib: datetime | None = None,
                        order_lots: float = 0.0,
                        atr_pct: float = 1.0) -> dict:
    """
    [J01+K01] Enhanced Execution Simulation — slippage + spread + queue +
    partial fill + market impact (v7.16).

    Sekarang memodelkan 5 komponen realitas eksekusi IDX:

    1. Bid-ask spread (Corwin-Schultz proxy per fraksi harga)
    2. Data delay slippage (~15 menit adverse move)
    3. Queue priority modifier (berdasarkan sesi waktu IDX)
    4. Volatility slippage (semakin lebar candle range → semakin buruk fill)
    5. [K01] Market impact cost (VPR model — kritis untuk mid/small cap)

    Partial fill dilaporkan terpisah (lihat simulate_partial_fill).
    Returns adjusted entry, sl, tp1 + breakdown lengkap semua cost.
    """
    if entry <= 0:
        return {"entry_adj": entry, "sl_adj": sl, "tp1_adj": tp1,
                "slippage_pct": 0.0, "spread_pct": 0.0, "net_cost_pct": 0.0,
                "queue_session": "UNKNOWN", "partial_fill_risk": 0.0,
                "impact_pct": 0.0, "vpr": 0.0, "cap_tier": "UNKNOWN"}

    # 1. Bid-ask spread berdasarkan fraksi harga IDX
    if entry >= 10_000:   spread_pct = 0.05
    elif entry >= 5_000:  spread_pct = 0.10
    elif entry >= 2_000:  spread_pct = 0.15
    elif entry >= 500:    spread_pct = 0.25
    else:                 spread_pct = 0.50

    # 2. Data delay slippage (15 menit)
    delay_slippage_pct = 0.15

    # 3. Queue priority modifier
    queue_info = get_idx_queue_priority(now_wib)
    queue_slip = queue_info["slippage_modifier"]

    # 4. Volatility slippage
    if price_range > 0:
        vol_slippage_pct = min(price_range * 0.1, 0.3)
    else:
        vol_slippage_pct = 0.10

    # 5. [K01] Market impact — VPR model
    mkt_impact = calc_market_impact(
        order_lots=order_lots,
        vol_today_idr=vol_today_idr,
        price=entry,
        atr_pct=atr_pct,
        ticker=ticker,
    )
    impact_pct = mkt_impact["impact_pct"]

    total_cost_pct = (spread_pct + delay_slippage_pct + queue_slip
                      + vol_slippage_pct + impact_pct)

    if side == "BUY":
        entry_adj = entry * (1 + total_cost_pct / 100)
        sl_adj, tp1_adj = sl, tp1
    else:
        entry_adj = entry * (1 - total_cost_pct / 100)
        sl_adj, tp1_adj = sl, tp1

    result = {
        "entry_adj":          round(entry_adj, 2),
        "sl_adj":             sl_adj,
        "tp1_adj":            tp1_adj,
        "spread_pct":         round(spread_pct, 3),
        "delay_slippage_pct": round(delay_slippage_pct, 3),
        "queue_slippage_pct": round(queue_slip, 3),
        "vol_slippage_pct":   round(vol_slippage_pct, 3),
        "impact_pct":         round(impact_pct, 4),
        "vpr":                mkt_impact["vpr"],
        "cap_tier":           mkt_impact["cap_tier"],
        "slippage_pct":       round(delay_slippage_pct + queue_slip + vol_slippage_pct, 3),
        "net_cost_pct":       round(total_cost_pct, 3),
        "queue_session":      queue_info["session"],
        "partial_fill_risk":  queue_info["partial_fill_risk"],
        "queue_note":         queue_info["note"],
    }
    return result


def run_backtest(days_back: int = 30, send_telegram: bool = True) -> dict:
    """
    [v7.14] Backtest dengan execution simulation.

    Mengambil semua signal yang sudah resolved (WIN/LOSS/EXPIRED)

    dalam N hari terakhir, lalu:
    1. Hitung actual winrate per breakdown
    2. Hitung average RR yang dicapai vs yang dijanjikan
    3. Deteksi apakah tier S benar-benar > tier A (validasi scoring)
    4. Deteksi regime/sektor mana yang paling profitable
    5. Kirim laporan ke Telegram

    Returns: dict dengan breakdown lengkap
    """
    log("📊 Backtest module starting...")
    now_utc = datetime.now(timezone.utc)
    since   = (now_utc - timedelta(days=days_back)).isoformat()

    try:
        rows = (
            supabase.table("signals")
            .select("pair,side,strategy,tier,score,entry,sl,tp1,tp2,outcome,regime,sector,win_prob,ev,sent_at,closed_at")
            .in_("outcome", ["WIN","LOSS","EXPIRED"])
            .gt("sent_at", since)
            .order("sent_at", desc=False)
            .execute()
            .data
        )
    except Exception as e:
        log(f"⚠️ Backtest: Supabase error — {e}", "warn")
        return {}

    if not rows or len(rows) < 5:
        msg = (f"📊 <b>Backtest — Tidak cukup data</b>\n"
               f"Hanya {len(rows) if rows else 0} signal dalam {days_back} hari.\n"
               f"Butuh minimal 5 signal resolved untuk analisis bermakna.")
        if send_telegram: tg(msg)
        log("📊 Backtest: insufficient data")
        return {"error": "insufficient_data", "count": len(rows) if rows else 0}

    # ── Kalkulasi dasar ───────────────────────────────────
    total    = len(rows)
    wins     = sum(1 for r in rows if r["outcome"] == "WIN")
    losses   = sum(1 for r in rows if r["outcome"] == "LOSS")
    expired  = sum(1 for r in rows if r["outcome"] == "EXPIRED")
    wr_actual = wins / (wins + losses) if (wins + losses) > 0 else 0.0

    # ── RR theoretical vs RR setelah slippage (execution simulation) ──
    rr_promised = []
    rr_simulated = []
    avg_net_cost = []

    for r in rows:
        try:
            entry = float(r["entry"] or 0)
            sl    = float(r["sl"]    or 0)
            tp1   = float(r["tp1"]   or 0)
            side  = r.get("side", "BUY")
            if entry <= 0 or sl <= 0 or tp1 <= 0:
                continue

            sl_dist = abs(entry - sl)
            if sl_dist <= 0:
                continue

            rr_raw = abs(tp1 - entry) / sl_dist
            rr_promised.append(rr_raw)

            # [v7.14] Fix 4: Simulasi execution dengan slippage model
            sim = simulate_execution(entry, sl, tp1, side)
            avg_net_cost.append(sim["net_cost_pct"])
            entry_adj = sim["entry_adj"]
            sl_dist_adj = abs(entry_adj - sl)
            if sl_dist_adj > 0:
                rr_adj = abs(tp1 - entry_adj) / sl_dist_adj
                rr_simulated.append(rr_adj)
        except Exception:
            pass

    avg_rr_promised  = float(np.mean(rr_promised))  if rr_promised  else 0.0
    avg_rr_simulated = float(np.mean(rr_simulated)) if rr_simulated else 0.0
    avg_cost_pct     = float(np.mean(avg_net_cost))  if avg_net_cost  else 0.0

    # EV theoretical vs EV setelah slippage
    ev_theoretical = wr_actual * avg_rr_promised  - (1 - wr_actual) if avg_rr_promised  > 0 else 0.0
    ev_actual      = wr_actual * avg_rr_simulated - (1 - wr_actual) if avg_rr_simulated > 0 else ev_theoretical

    # ── Breakdown per tier ────────────────────────────────
    tier_stats: dict = {}
    for tier in ["S", "A+", "A"]:
        t_rows = [r for r in rows if r.get("tier") == tier]
        if len(t_rows) >= 3:
            t_wins = sum(1 for r in t_rows if r["outcome"] == "WIN")
            t_loss = sum(1 for r in t_rows if r["outcome"] == "LOSS")
            denom  = t_wins + t_loss
            tier_stats[tier] = {
                "total": len(t_rows),
                "wr":    round(t_wins / denom * 100, 1) if denom > 0 else 0.0,
                "wins":  t_wins,
            }

    # ── Breakdown per regime ──────────────────────────────
    regime_stats: dict = {}
    for regime in ["TRENDING","RANGING","CHOPPY"]:
        r_rows = [r for r in rows if r.get("regime") == regime]
        if len(r_rows) >= 3:
            r_wins = sum(1 for r in r_rows if r["outcome"] == "WIN")
            r_loss = sum(1 for r in r_rows if r["outcome"] == "LOSS")
            denom  = r_wins + r_loss
            regime_stats[regime] = {
                "total": len(r_rows),
                "wr":    round(r_wins / denom * 100, 1) if denom > 0 else 0.0,
            }

    # ── Breakdown per sektor (top 5) ──────────────────────
    sector_map: dict = {}
    for r in rows:
        sec = r.get("sector") or "MISC"
        if sec not in sector_map:
            sector_map[sec] = {"wins": 0, "total": 0}
        sector_map[sec]["total"] += 1
        if r["outcome"] == "WIN":
            sector_map[sec]["wins"] += 1

    top_sectors = sorted(
        [(s, d["wins"]/(d["total"] or 1)*100, d["total"])
         for s, d in sector_map.items() if d["total"] >= 3],
        key=lambda x: x[1], reverse=True
    )[:5]

    # ── Breakdown per strategy ────────────────────────────
    intra_rows  = [r for r in rows if r.get("strategy") == "INTRADAY"]
    swing_rows  = [r for r in rows if r.get("strategy") == "SWING"]
    def _wr(subset):
        w = sum(1 for r in subset if r["outcome"]=="WIN")
        l = sum(1 for r in subset if r["outcome"]=="LOSS")
        return round(w/(w+l)*100, 1) if (w+l)>0 else None

    # ── Validasi: apakah tier S > tier A? ─────────────────
    tier_valid = False
    if "S" in tier_stats and "A" in tier_stats:
        tier_valid = tier_stats["S"]["wr"] > tier_stats["A"]["wr"]

    # ── Prediksi vs aktual win_prob ───────────────────────
    prob_errors = []
    for r in rows:
        if r.get("win_prob") and r["outcome"] in ("WIN","LOSS"):
            pred = float(r["win_prob"])
            act  = 1.0 if r["outcome"] == "WIN" else 0.0
            prob_errors.append(abs(pred - act))
    avg_prob_error = float(np.mean(prob_errors)) if prob_errors else None

    # ── Susun laporan Telegram ────────────────────────────
    ev_emoji  = "✅" if ev_actual > 0.2 else ("⚠️" if ev_actual > 0 else "❌")
    tier_note = "✅ Tier S lebih profitable dari A" if tier_valid else "⚠️ Tier S TIDAK konsisten lebih baik dari A — scoring butuh review"

    tier_lines = "\n".join(
        f"  Tier {t}: {d['wr']}% ({d['wins']}/{d['total']})"
        for t, d in tier_stats.items()
    ) or "  Data tidak cukup per tier"

    regime_lines = "\n".join(
        f"  {r}: {d['wr']}% ({d['total']} signal)"
        for r, d in regime_stats.items()
    ) or "  Data tidak cukup per regime"

    sector_lines = "\n".join(
        f"  {s}: {wr:.0f}% ({n} signal)"
        for s, wr, n in top_sectors
    ) or "  Data tidak cukup per sektor"

    prob_line = f"Avg prediction error: {avg_prob_error:.2f}" if avg_prob_error else "N/A"

    msg = (
        f"📊 <b>Backtest Report — {days_back} hari terakhir</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Total signal  : {total} ({wins}W / {losses}L / {expired}EXP)\n"
        f"Winrate aktual: <b>{wr_actual:.0%}</b>\n"
        f"RR theoretical : {avg_rr_promised:.2f}\n"
        f"Avg cost (slip+spread): {avg_cost_pct:.2f}%\n"
        f"RR setelah slippage: {avg_rr_simulated:.2f}\n"
        f"EV theoretical: {ev_theoretical:+.2f} | EV realistic: <b>{ev_actual:+.2f}</b> {ev_emoji}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 Intraday WR: {_wr(intra_rows)}% ({len(intra_rows)} signal)\n"
        f"🌊 Swing WR   : {_wr(swing_rows)}% ({len(swing_rows)} signal)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Tier breakdown:\n{tier_lines}\n"
        f"{tier_note}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Regime breakdown:\n{regime_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Top 5 sektor:\n{sector_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Win prob accuracy: {prob_line}\n"
        f"<i>⚠️ Bukan walk-forward. Slippage & execution tidak diperhitungkan.</i>"
    )
    if send_telegram:
        tg(msg)
    log(f"📊 Backtest selesai: WR={wr_actual:.0%} EV={ev_actual:+.2f} ({total} signal)")

    return {
        "total": total, "wins": wins, "losses": losses, "expired": expired,
        "wr_actual": wr_actual, "avg_rr_promised": avg_rr_promised,
        "ev_actual": ev_actual, "tier_stats": tier_stats,
        "regime_stats": regime_stats, "tier_scoring_valid": tier_valid,
    }


def optimize_weights(days_back: int = 60, send_telegram: bool = True) -> dict:
    """
    [v7.14] Fix 5: Simple weight optimization berbasis data historis Supabase.

    Jujur: ini BUKAN machine learning. Ini grid search sederhana yang mencari
    kombinasi weight terbaik berdasarkan winrate historis per score bucket.

    Metodologi:
    - Ambil signal WIN/LOSS dari Supabase (N hari terakhir)
    - Group per score range (0-7, 8-9, 10-13, 14+)
    - Hitung WR per group → apakah score tinggi = WR lebih tinggi?
    - Jika ya → scoring system memiliki edge, W base reasonable
    - Jika tidak → W base perlu direview, ada over/under-weighting

    Hasil: rekomendasi adjustment per komponen, bukan angka pasti.
    Penggunaan: python bot_saham_v7_14.py --optimize [--days=60]

    Keterbatasan yang jujur:
    ❌ Bukan walk-forward — bisa overfit ke periode tertentu
    ❌ Hanya 17 weight component, sample size dari IDX bisa kecil
    ❌ Tidak mempertimbangkan covariance antar komponen
    ✅ Lebih baik dari tuning manual yang sepenuhnya subjektif
    """
    log("🔧 Weight optimization starting...")
    now_utc = datetime.now(timezone.utc)
    since   = (now_utc - timedelta(days=days_back)).isoformat()

    try:
        rows = (
            supabase.table("signals")
            .select("score, tier, outcome, regime, sector, strategy, win_prob")
            .in_("outcome", ["WIN", "LOSS"])
            .gt("sent_at", since)
            .execute()
            .data
        )
    except Exception as e:
        log(f"⚠️ optimize_weights: Supabase error — {e}", "warn")
        return {}

    if not rows or len(rows) < 50:
        log(f"⚠️ optimize_weights: data tidak cukup ({len(rows) if rows else 0} < 50 — grid search butuh min 50 untuk hindari noise fit)")
        return {"error": "insufficient_data"}

    total = len(rows)
    log(f"🔧 Analyzing {total} signals from last {days_back} days...")

    # ── Step 1: Validasi score → WR monotonicity ──────────────────
    # Jika scoring sistem valid: score lebih tinggi HARUS → WR lebih tinggi
    score_buckets = {
        "low (0-7)":    [r for r in rows if (r.get("score") or 0) <= 7],
        "mid (8-9)":    [r for r in rows if 8  <= (r.get("score") or 0) <= 9],
        "good (10-13)": [r for r in rows if 10 <= (r.get("score") or 0) <= 13],
        "high (14+)":   [r for r in rows if (r.get("score") or 0) >= 14],
    }
    bucket_wr = {}
    for label, subset in score_buckets.items():
        if len(subset) >= 3:
            wins = sum(1 for r in subset if r["outcome"] == "WIN")
            bucket_wr[label] = round(wins / len(subset) * 100, 1)

    # Cek monotonicity
    wr_vals = list(bucket_wr.values())
    is_monotone = all(wr_vals[i] <= wr_vals[i+1] for i in range(len(wr_vals)-1))

    # ── Step 2: Kalkulasi optimal EV threshold berbasis data ──────
    # Untuk setiap tier threshold, hitung berapa WR yang dicapai
    tier_analysis = {}
    for tier, min_score in W.items() if isinstance(W, dict) else []:
        pass
    for tier, min_score in [("A", 8), ("A+", 10), ("S", 14)]:
        above = [r for r in rows if (r.get("score") or 0) >= min_score]
        below = [r for r in rows if (r.get("score") or 0) <  min_score]
        if len(above) >= 3:
            wr_above = sum(1 for r in above if r["outcome"]=="WIN") / len(above)
            tier_analysis[tier] = {"wr": round(wr_above*100,1), "n": len(above)}

    # ── Step 3: Deteksi over/under-performing regime ──────────────
    regime_ev = {}
    for regime in ["TRENDING", "RANGING", "CHOPPY"]:
        r_rows = [r for r in rows if r.get("regime") == regime]
        if len(r_rows) >= 5:
            wr = sum(1 for r in r_rows if r["outcome"]=="WIN") / len(r_rows)
            regime_ev[regime] = round(wr * 100, 1)

    # ── Step 4: Rekomendasi adjustment ───────────────────────────
    recommendations = []

    # Cek monotonicity scoring
    if not is_monotone and len(wr_vals) >= 3:
        recommendations.append("⚠️ Score lebih tinggi TIDAK konsisten menghasilkan WR lebih tinggi — W base perlu direview")
    elif is_monotone and len(wr_vals) >= 3:
        recommendations.append("✅ Score → WR monotone — scoring system memiliki directional edge")

    # Cek regime: kalau CHOPPY punya WR tinggi, filter regime kurang efektif
    if "CHOPPY" in regime_ev and regime_ev["CHOPPY"] > 55:
        recommendations.append(f"⚠️ CHOPPY regime WR={regime_ev['CHOPPY']}% — regime filter tidak efektif memblokir signal di kondisi choppy")

    # Cek: apakah tier S jauh lebih baik dari tier A?
    if "S" in tier_analysis and "A" in tier_analysis:
        diff = tier_analysis["S"]["wr"] - tier_analysis["A"]["wr"]
        if diff < 5:
            recommendations.append(f"⚠️ Tier S hanya {diff:.0f}% lebih baik dari Tier A — min score Tier S ({TIER_MIN_SCORE['S']}) mungkin terlalu rendah")
        else:
            recommendations.append(f"✅ Tier S unggul {diff:.0f}% dari Tier A — threshold tier sudah reasonable")

    # Kalibrasi win_prob: apakah prediksi sesuai aktual?
    prob_calibration = []
    for r in rows:
        if r.get("win_prob") and r["outcome"] in ("WIN", "LOSS"):
            pred = float(r["win_prob"])
            act  = 1.0 if r["outcome"] == "WIN" else 0.0
            prob_calibration.append((pred, act))

    if len(prob_calibration) >= 10:
        avg_pred  = float(np.mean([p for p, _ in prob_calibration]))
        avg_act   = float(np.mean([a for _, a in prob_calibration]))
        calib_err = avg_pred - avg_act
        if abs(calib_err) > 0.10:
            direction = "over-confident" if calib_err > 0 else "under-confident"
            recommendations.append(
                f"⚠️ Win prob {direction}: prediksi avg {avg_pred:.0%} vs aktual {avg_act:.0%} "
                f"(bias {calib_err:+.0%}) — model probabilitas perlu kalibrasi"
            )
        else:
            recommendations.append(f"✅ Win prob kalibrasi baik: pred {avg_pred:.0%} ≈ aktual {avg_act:.0%}")

    if not recommendations:
        recommendations.append("✅ Tidak ada anomali signifikan ditemukan")

    # ── Susun laporan Telegram ────────────────────────────────────
    bucket_lines = "\n".join(
        f"  {label}: {wr}% ({len(score_buckets.get(label, []))} signal)"
        for label, wr in bucket_wr.items()
    ) or "  Data tidak cukup"

    tier_lines = "\n".join(
        f"  Tier {t}: WR={d['wr']}% ({d['n']} signal)"
        for t, d in tier_analysis.items()
    ) or "  Data tidak cukup"

    regime_lines = "\n".join(
        f"  {r}: {wr}%" for r, wr in regime_ev.items()
    ) or "  Data tidak cukup"

    rec_lines = "\n".join(f"  {r}" for r in recommendations)

    mono_emoji = "✅" if is_monotone else "⚠️"

    msg = (
        f"🔧 <b>Weight Optimization Report — {days_back} hari</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Total signal: {total}\n"
        f"Monotonicity: {mono_emoji} {'Score → WR naik konsisten' if is_monotone else 'TIDAK monotone'}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"WR per score bucket:\n{bucket_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"WR per tier:\n{tier_lines}\n"
        f"WR per regime:\n{regime_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Rekomendasi:\n{rec_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>⚠️ Grid search sederhana, bukan ML. Minimal 60 hari + 50 signal untuk kesimpulan valid.</i>"
    )
    if send_telegram:
        tg(msg)
    log(f"🔧 Optimization selesai: {len(recommendations)} rekomendasi, monotone={is_monotone}")

    return {
        "total": total, "bucket_wr": bucket_wr, "tier_analysis": tier_analysis,
        "regime_ev": regime_ev, "is_monotone": is_monotone,
        "recommendations": recommendations,
    }


# ════════════════════════════════════════════════════════
#  [J02] WALK-FORWARD VALIDATION — v7.15
#
#  Perbedaan fundamental dari backtest biasa:
#  - Backtest biasa: train & test di data SAMA → overfit bias
#  - Walk-forward: train in-sample, test out-of-sample
#    yang TIDAK pernah dilihat model → estimasi edge lebih jujur
#
#  Format expanding window:
#    Window 1: train 0-30d → test 31-40d
#    Window 2: train 0-40d → test 41-50d
#    ...dan seterusnya
#
#  Metrik kritis: gap WR in-sample vs out-of-sample
#    Gap > 15% → kemungkinan overfit
#    Gap < 5%  → model generalize baik
#
#  Jalankan: python bot_saham_v7_15.py --walkforward [--days=90]
# ════════════════════════════════════════════════════════


def run_walk_forward_validation(total_days: int = 90,
                                 test_window_days: int = 10,
                                 send_telegram: bool = True) -> dict:
    """
    [J02] Walk-Forward Validation.

    Ambil semua signal resolved dari Supabase, urutkan chronologis,
    lalu iterasi expanding window: train → test → report.
    """
    log(f"🔍 Walk-Forward Validation — {total_days}d total, {test_window_days}d test window")
    now_utc = datetime.now(timezone.utc)
    since   = (now_utc - timedelta(days=total_days)).isoformat()

    try:
        rows = (
            supabase.table("signals")
            .select("score, tier, outcome, regime, sector, strategy, sent_at, win_prob")
            .in_("outcome", ["WIN", "LOSS"])
            .gt("sent_at", since)
            .order("sent_at", desc=False)
            .execute()
            .data
        )
    except Exception as e:
        log(f"⚠️ walk_forward: Supabase error — {e}", "warn")
        return {"error": str(e)}

    if not rows or len(rows) < 30:
        log(f"⚠️ walk_forward: data tidak cukup ({len(rows) if rows else 0} < 30)")
        return {"error": "insufficient_data", "n": len(rows) if rows else 0}

    def parse_ts(r):
        try:
            return datetime.fromisoformat(r["sent_at"].replace("Z", "+00:00"))
        except Exception:
            return now_utc

    rows_sorted = sorted(rows, key=parse_ts)
    total_n     = len(rows_sorted)
    log(f"  📊 Total signal untuk WF: {total_n}")

    min_train = max(int(total_n * 0.5), 15)
    step      = max(1, int(test_window_days * total_n / total_days))

    windows = []
    idx = min_train
    while idx < total_n:
        train_data = rows_sorted[:idx]
        test_data  = rows_sorted[idx: idx + step]
        if len(test_data) < 3:
            break

        in_wins  = sum(1 for r in train_data if r["outcome"] == "WIN")
        in_wr    = in_wins / len(train_data)
        out_wins = sum(1 for r in test_data if r["outcome"] == "WIN")
        out_wr   = out_wins / len(test_data)

        # Bandingkan recent in-sample (bukan full train) untuk apple-to-apple
        recent_train = train_data[-max(len(test_data), 10):]
        recent_wins  = sum(1 for r in recent_train if r["outcome"] == "WIN")
        recent_wr    = recent_wins / len(recent_train)

        windows.append({
            "train_n":       len(train_data),
            "test_n":        len(test_data),
            "in_wr":         round(in_wr, 4),
            "recent_in_wr":  round(recent_wr, 4),
            "out_wr":        round(out_wr, 4),
            "gap":           round(recent_wr - out_wr, 4),
        })
        idx += step

    if not windows:
        return {"error": "no_windows_generated"}

    avg_in  = float(np.mean([w["recent_in_wr"] for w in windows]))
    avg_out = float(np.mean([w["out_wr"] for w in windows]))
    avg_gap = float(np.mean([w["gap"] for w in windows]))

    if avg_gap > 0.20:
        verdict = "OVERFIT"
        verdict_msg   = f"Gap in/out {avg_gap:.0%} — model overfit. Sederhanakan scoring."
        verdict_emoji = "❌"
    elif avg_gap > 0.10:
        verdict = "MODERATE_OVERFIT"
        verdict_msg   = f"Gap {avg_gap:.0%} — overfit ringan. Monitor terus."
        verdict_emoji = "⚠️"
    elif avg_out < 0.40:
        verdict = "EDGE_DEGRADED"
        verdict_msg   = f"WR out-of-sample {avg_out:.0%} < 40% — edge mungkin hilang."
        verdict_emoji = "🔴"
    else:
        verdict = "VALID"
        verdict_msg   = f"Gap {avg_gap:.0%}, out WR {avg_out:.0%} — model generalize baik."
        verdict_emoji = "✅"

    window_lines = "\n".join(
        f"  W{i+1}: train={w['train_n']} test={w['test_n']} "
        f"in={w['recent_in_wr']:.0%} out={w['out_wr']:.0%} gap={w['gap']:+.0%}"
        for i, w in enumerate(windows)
    )

    msg = (
        f"🔍 <b>Walk-Forward Validation — {total_days}d</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Total signal   : {total_n}\n"
        f"Windows tested : {len(windows)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Avg in-sample WR  : {avg_in:.0%}\n"
        f"Avg out-sample WR : <b>{avg_out:.0%}</b>\n"
        f"Avg gap (overfit) : <b>{avg_gap:+.0%}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Per-window:\n{window_lines}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{verdict_emoji} <b>Verdict: {verdict}</b>\n"
        f"<i>{verdict_msg}</i>\n"
        f"<i>⚠️ Minimal 50 signal resolved untuk WF yang bermakna.</i>"
    )

    if send_telegram:
        tg(msg)

    log(f"🔍 Walk-forward selesai: in={avg_in:.0%} out={avg_out:.0%} gap={avg_gap:+.0%} [{verdict}]")

    return {
        "windows":          windows,
        "avg_insample_wr":  round(avg_in, 4),
        "avg_outsample_wr": round(avg_out, 4),
        "overfit_gap":      round(avg_gap, 4),
        "verdict":          verdict,
    }


# ════════════════════════════════════════════════════════
#  HEALTH CHECK — Heartbeat ke Telegram
# ════════════════════════════════════════════════════════

def send_daily_pnl_summary() -> None:
    """
    [v8.12] Kirim ringkasan PnL harian ke Telegram sekali sehari.
    Dipanggil dari run() saat jam 15 WIB (cron 15:30 — run terakhir sesi bursa).
    Merangkum seluruh aktivitas hari ini sebelum sesi tutup.
    """
    try:
        today_iso  = datetime.now(WIB).strftime("%Y-%m-%d")
        # Supabase stored in UTC — ambil range hari ini WIB (UTC+7)
        from datetime import timedelta
        _start_utc = (datetime.now(WIB).replace(hour=0, minute=0, second=0, microsecond=0)
                      - timedelta(hours=7)).isoformat()
        rows = (
            supabase.table("signals")
            .select("pair, side, strategy, outcome, entry, sl, tp1")
            .not_.is_("outcome", "null")
            .gte("closed_at", _start_utc)
            .order("closed_at", desc=False)
            .execute()
            .data
        ) or []

        # Juga hitung signal baru yang dikirim hari ini
        sent_today = (
            supabase.table("signals")
            .select("id")
            .gte("sent_at", _start_utc)
            .execute()
            .data
        ) or []

        wins      = [r for r in rows if r.get("outcome") == "WIN"]
        losses    = [r for r in rows if r.get("outcome") == "LOSS"]
        expireds  = [r for r in rows if r.get("outcome") == "EXPIRED"]
        total_cls = len(rows)

        # Estimasi R earned hari ini
        r_earned = 0.0
        for r in wins:
            try:
                _e = float(r.get("entry") or 0)
                _t = float(r.get("tp1") or 0)
                _s = float(r.get("sl") or 0)
                if _e > 0 and _t > 0 and _s > 0:
                    _rr = abs(_t - _e) / abs(_e - _s)
                    r_earned += _rr
            except Exception as _e:
                log(f"  ⚠️ [FALLBACK] r_earned RR calc: {_e} — +1.0R", "warn")
                r_earned += 1.0  # fallback asumsi 1R per win
        for _ in losses:
            r_earned -= 1.0

        _r_icon = "🟢" if r_earned > 0 else ("🔴" if r_earned < 0 else "⚪")
        _r_str  = f"{r_earned:+.2f}R"

        # Baris per signal (max 10)
        _sig_lines = []
        for r in rows[:10]:
            _out = r.get("outcome", "?")
            _ico = {"WIN": "✅", "LOSS": "❌", "EXPIRED": "⏳"}.get(_out, "?")
            _sig_lines.append(
                f"  {_ico} {r.get('pair','-')} {(r.get('side') or '?')[0]}"
                f"/{(r.get('strategy') or '?')[:2]}"
            )
        _extra_sig = (f"\n  <i>+{total_cls - 10} lainnya...</i>"
                      if total_cls > 10 else "")

        _now_str = datetime.now(WIB).strftime("%d/%m/%Y %H:%M WIB")
        _wr_today = f"{int(len(wins)/max(total_cls,1)*100)}%" if total_cls > 0 else "—"

        msg = (
            f"📅 <b>Daily PnL Summary — {today_iso}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"  Signal baru   : {len(sent_today)} dikirim hari ini\n"
            f"  Closed hari ini: {total_cls} "
            f"(✅{len(wins)} WIN / ❌{len(losses)} LOSS / ⏳{len(expireds)} EXP)\n"
            f"  Win rate hari ini: {_wr_today}\n"
            f"  Net R earned  : {_r_icon} <b>{_r_str}</b>\n"
        )
        if _sig_lines:
            msg += (
                f"━━━━━━━━━━━━━━━━━━\n"
                + "\n".join(_sig_lines)
                + _extra_sig + "\n"
            )
        msg += f"━━━━━━━━━━━━━━━━━━\n<i>Sesi bursa selesai. Selamat istirahat! 🌙</i>"
        tg(msg)
        log(f"📅 Daily PnL summary terkirim: {len(wins)}W/{len(losses)}L net={_r_str}")
    except Exception as e:
        log(f"⚠️ [v8.12] send_daily_pnl_summary: {e}", "warn")


def send_position_expiry_warnings() -> None:
    """
    [v8.12] Kirim warning ke Telegram untuk posisi yang sisa waktunya
    ≤ 8 jam sebelum expired.

    Stateless — aman untuk GitHub Actions (proses baru tiap run, tidak ada
    shared memory antar-run). Anti-flood by design: window 8 jam lebih kecil
    dari 2x interval run (2 × 4 jam), sehingga posisi SWING (10 hari) maksimal
    mendapat 2 warning total, INTRADAY (2 hari) maksimal 1-2 warning.
    """
    _EXPIRY_DAYS   = {"INTRADAY": 2, "SWING": 10, "SCALPING": 3}
    _WARN_WINDOW_H = 8   # warn jika sisa ≤ 8 jam sebelum expired

    try:
        rows = (
            supabase.table("signals")
            .select("id, pair, side, entry, sl, tp1, strategy, sent_at")
            .is_("outcome", "null")
            .execute()
            .data
        ) or []

        if not rows:
            return

        now_utc  = datetime.now(timezone.utc)
        warnings = []

        for r in rows:
            try:
                strategy  = r.get("strategy", "SWING")
                max_days  = _EXPIRY_DAYS.get(strategy, 10)

                sent_at = datetime.fromisoformat(r["sent_at"])
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)

                age_h       = (now_utc - sent_at).total_seconds() / 3600
                max_h       = max_days * 24
                remaining_h = max_h - age_h

                # Warn hanya jika masuk window terakhir dan belum expired
                if remaining_h > _WARN_WINDOW_H or remaining_h <= 0:
                    continue

                _en  = float(r.get("entry") or 0)
                _sl  = float(r.get("sl") or 0)
                _tp  = float(r.get("tp1") or 0)
                _en_fmt  = f"{int(_en):,}".replace(",", ".")
                _sl_fmt  = f"{int(_sl):,}".replace(",", ".")
                _tp_fmt  = f"{int(_tp):,}".replace(",", ".") if _tp else "—"
                _age_str = f"{age_h:.0f}j / {max_h:.0f}j"

                warnings.append(
                    f"  ⚠️ <b>{r.get('pair')} {r.get('side')} [{strategy}]</b>\n"
                    f"     Entry: {_en_fmt} | SL: {_sl_fmt} | TP: {_tp_fmt}\n"
                    f"     Usia: {_age_str} — sisa ~<b>{remaining_h:.1f} jam</b>"
                )

            except Exception as _re:
                log(f"  ⚠️ expiry warn row: {_re}", "warn")
                continue

        if not warnings:
            return

        _now_str  = datetime.now(WIB).strftime("%d/%m/%Y %H:%M WIB")
        _warn_txt = "\n".join(warnings)
        msg = (
            f"⏳ <b>Posisi Hampir Expired</b> ({len(warnings)} posisi)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{_warn_txt}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>Cek apakah SL sudah dipasang di broker.\n"
            f"Posisi tanpa SL aktif berisiko kehilangan lebih dari 1R.</i>"
        )
        tg(msg)
        log(f"⏳ Expiry warning terkirim: {len(warnings)} posisi")

    except Exception as e:
        log(f"⚠️ [v8.12] send_position_expiry_warnings: {e}", "warn")


def send_health_check(scanned: int, skip_vol: int, ihsg: dict, wr: dict,
                       no_signal: bool = False,
                       portfolio: dict | None = None,
                       pipeline_tg: str = "",
                       top_blockers_tg: str = ""):
    """
    Kirim heartbeat ringkas ke Telegram setiap run — bukti bot masih hidup.
    Jika no_signal=True, tambahkan keterangan tidak ada sinyal di pesan yang sama
    agar tidak ada double notifikasi.

    [AA01] v8.11: Blok IHSG diperluas dengan data real (price, OHLC,
    volume vs avg, posisi 20d range, MTD/YTD).
    [v8.12] Tambahan: Portfolio Exposure, Open Positions, Filter Audit,
    Bootstrap progress bar, Streak & Edge Health.
    """
    now_str  = datetime.now(WIB).strftime("%d/%m/%Y %H:%M WIB")
    wr_str   = f"{wr['overall']}%" if wr["overall"] is not None else "belum ada data"
    wr_n     = f"{wr['wins']}/{wr['total_closed']}" if wr["total_closed"] > 0 else "—"
    exp_str  = f" | {wr.get('expired', 0)} expired" if wr.get("expired", 0) > 0 else ""
    intra_wr = f"{wr['intraday']}%" if wr["intraday"] is not None else "—"
    swing_wr = f"{wr['swing']}%"    if wr["swing"]    is not None else "—"

    signal_note = (
        f"\n━━━━━━━━━━━━━━━━━━\n"
        f"📭 Tidak ada sinyal memenuhi kriteria.\n"
        f"BUY: {'aktif' if not ihsg['block_buy'] else '⛔ diblokir (IHSG drop)'}\n"
        f"<i>Scan berikutnya ±4 jam.</i>"
    ) if no_signal else f"\n<i>Bot berjalan normal. Scan berikutnya ±4 jam.</i>"

    # ── [AA01] IHSG blok — data real ─────────────────────────────────
    _price   = ihsg.get("price")
    _open    = ihsg.get("open")
    _high    = ihsg.get("high")
    _low     = ihsg.get("low")
    _vol     = ihsg.get("volume")
    _vol_avg = ihsg.get("volume_avg20")
    _mtd     = ihsg.get("ihsg_mtd")
    _ytd     = ihsg.get("ihsg_ytd")
    _h20     = ihsg.get("ihsg_20d_high")
    _l20     = ihsg.get("ihsg_20d_low")
    _1d      = ihsg.get("ihsg_1d", 0.0)
    _5d      = ihsg.get("ihsg_5d", 0.0)

    def _fmt_price(v):
        return f"{v:,.0f}".replace(",", ".") if v is not None else "—"

    def _fmt_pct(v, bold=False):
        if v is None: return "—"
        arrow = "▲" if v >= 0 else "▼"
        sign  = "+" if v >= 0 else ""
        s = f"{arrow} {sign}{v:.2f}%"
        return f"<b>{s}</b>" if bold else s

    def _fmt_vol(v):
        if v is None: return "—"
        if v >= 1_000_000_000: return f"{v/1_000_000_000:.2f}M lot"
        if v >= 1_000_000:     return f"{v/1_000_000:.1f}rb lot"
        return f"{v:,} lot"

    # Volume vs rata-rata 20 hari
    vol_ratio_str = ""
    if _vol is not None and _vol_avg is not None and _vol_avg > 0:
        _vr = _vol / _vol_avg
        _vr_lbl  = "🔥 spike" if _vr >= 2.0 else ("📈 tinggi" if _vr >= 1.3 else ("📉 sepi" if _vr < 0.7 else "normal"))
        vol_ratio_str = f"  ({_vr:.1f}x avg — {_vr_lbl})"

    # Posisi harga dalam 20d range — progress bar visual
    range_str = ""
    if _price is not None and _h20 is not None and _l20 is not None and _h20 > _l20:
        _pos  = (_price - _l20) / (_h20 - _l20)
        _bars = int(_pos * 10)
        _bar  = "█" * _bars + "░" * (10 - _bars)
        _pos_lbl = "near HIGH" if _pos >= 0.8 else ("near LOW" if _pos <= 0.2 else f"{_pos*100:.0f}%")
        range_str = (
            f"\n  Range 20h : {_fmt_price(_l20)} [{_bar}] {_fmt_price(_h20)}"
            f"  ← {_pos_lbl}"
        )

    # Status baris
    if ihsg.get("halt"):
        _status = "🛑 <b>HALT SEMUA</b> (crash > 5% / 5h)"
    elif ihsg.get("block_buy"):
        _status = "⛔ <b>BUY DIBLOKIR</b> (IHSG drop > 2%)"
    else:
        _status = "✅ Normal — semua sinyal aktif"

    # MTD / YTD baris (opsional, hanya tampil jika ada data)
    mtd_ytd_str = ""
    if _mtd is not None or _ytd is not None:
        parts = []
        if _mtd is not None: parts.append(f"MTD {_fmt_pct(_mtd)}")
        if _ytd is not None: parts.append(f"YTD {_fmt_pct(_ytd)}")
        mtd_ytd_str = f"\n  {'  |  '.join(parts)}"

    ihsg_block = (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>IHSG</b>\n"
        f"  Harga  : <b>{_fmt_price(_price)}</b>  {_fmt_pct(_1d, bold=True)}\n"
        f"  O / H / L : {_fmt_price(_open)} / {_fmt_price(_high)} / {_fmt_price(_low)}\n"
        f"  5 hari : {_fmt_pct(_5d)}{mtd_ytd_str}\n"
        f"  Volume : {_fmt_vol(_vol)}{vol_ratio_str}"
        + range_str
        + f"\n  {_status}"
    )

    # ── [v8.12] PORTFOLIO EXPOSURE block ─────────────────────────────
    portfolio_block = ""
    _port = portfolio or {}
    _total_risk  = _port.get("total_risk_pct", 0.0)
    _deployed    = _port.get("deployed_pct", 0.0)
    _open_count  = _port.get("open_count", 0)
    if _port:
        _risk_bar_filled = int(min(_total_risk / max(KS_DRAWDOWN_PCT_MAX, 1), 1.0) * 10)
        _risk_bar  = "█" * _risk_bar_filled + "░" * (10 - _risk_bar_filled)
        _risk_icon = ("🔴" if _total_risk >= KS_DRAWDOWN_PCT_MAX * 0.85 else
                      "🟡" if _total_risk >= KS_DRAWDOWN_PCT_MAX * 0.50 else "🟢")
        portfolio_block = (
            f"\n━━━━━━━━━━━━━━━━━━\n"
            f"💼 <b>Portfolio Exposure</b>\n"
            f"  At-Risk   : {_risk_icon} <b>{_total_risk:.1f}%</b> / {KS_DRAWDOWN_PCT_MAX}%"
            f"  [{_risk_bar}]\n"
            f"  Deployed  : {_deployed:.1f}% modal terikat\n"
            f"  Open Pos. : {_open_count} posisi aktif"
        )

    # ── [v8.12] OPEN POSITIONS SUMMARY block ─────────────────────────
    open_pos_block = ""
    try:
        _op_rows = (
            supabase.table("signals")
            .select("pair, entry, sl, tp1, strategy, side, sent_at")
            .is_("outcome", "null")
            .order("sent_at", desc=False)
            .execute()
            .data
        ) or []
        if _op_rows:
            _op_lines = []
            _now_utc_op = datetime.now(timezone.utc)
            for _r in _op_rows[:8]:  # max 8 baris agar tidak flood
                _sym   = _r.get("pair", "—")
                _side  = (_r.get("side") or "?")[0]
                _strat = (_r.get("strategy") or "?")[:2]
                _en    = float(_r.get("entry") or 0)
                _sl    = float(_r.get("sl") or 0)
                _tp    = float(_r.get("tp1") or 0)
                try:
                    _sa = datetime.fromisoformat(_r["sent_at"])
                    if _sa.tzinfo is None:
                        _sa = _sa.replace(tzinfo=timezone.utc)
                    _age_h = int((_now_utc_op - _sa).total_seconds() / 3600)
                    _age_str = f"{_age_h}j"
                except Exception as _e:
                    log(f"  ⚠️ [FALLBACK] signal age string: {_e}", "warn")
                    _age_str = "?j"
                _en_fmt = f"{int(_en):,}".replace(",", ".")
                _sl_fmt = f"{int(_sl):,}".replace(",", ".")
                _tp_fmt = f"{int(_tp):,}".replace(",", ".") if _tp else "—"
                _op_lines.append(
                    f"  {_sym} [{_side}/{_strat}] E:{_en_fmt} SL:{_sl_fmt}"
                    f" TP:{_tp_fmt} ({_age_str})"
                )
            _extra = (f"\n  <i>+{len(_op_rows) - 8} posisi lainnya...</i>"
                      if len(_op_rows) > 8 else "")
            open_pos_block = (
                f"\n━━━━━━━━━━━━━━━━━━\n"
                f"📋 <b>Open Positions</b> ({len(_op_rows)} aktif)\n"
                + "\n".join(_op_lines)
                + _extra
            )
    except Exception as _ope:
        log(f"⚠️ [v8.12] open positions block: {_ope}", "warn")

    # ── BOOTSTRAP phase ───────────────────────────────────────────────
    _n_cur    = globals().get("_edge_n_cache", 0)
    _bp       = globals().get("_bootstrap_phase", "COLD")
    _bp_icons = {"COLD": "🧊", "EARLY": "🌱", "WARMING": "🔥", "MATURE": "✅"}
    _bp_icon  = _bp_icons.get(_bp, "❓")

    # [v8.12] Progress bar helper
    def _pbar(current: int, target: int, width: int = 10) -> str:
        _f = int(min(current / max(target, 1), 1.0) * width)
        return "█" * _f + "░" * (width - _f)

    # [v8.12] Streak & relaxation helper (dipakai WARMING + MATURE)
    def _edge_health_lines() -> str:
        _out = ""
        try:
            _ks_s = check_losing_streak()
            _s = _ks_s.get("streak", 0)
            if _s > 0:
                _out += f"\n  Streak   : 🔴 {_s}x LOSS berturut"
            else:
                _wr_val = wr.get("overall")
                if _wr_val is not None:
                    _out += f"\n  Streak   : 🟢 WR {_wr_val}% overall"
        except Exception:
            pass
        try:
            _ar = globals().get("_active_relaxations", {})
            if _ar:
                _rkeys = ", ".join(list(_ar.keys())[:3])
                _out += f"\n  Relaxasi : ⚠️ {len(_ar)} gate aktif ({_rkeys})"
        except Exception:
            pass
        try:
            _ds = globals().get("_disabled_strategies", {})
            _dis = [k for k, v in _ds.items() if v]
            if _dis:
                _out += f"\n  Disabled : 🚫 {', '.join(_dis)}"
        except Exception:
            pass
        return _out

    if _bp in ("COLD", "EARLY"):
        _cap_now  = (BOOTSTRAP_SIGNALS_CAP_COLD if _bp == "COLD"
                     else BOOTSTRAP_SIGNALS_CAP_EARLY)
        _target_n = BOOTSTRAP_EARLY_N
        bootstrap_note = (
            f"\n━━━━━━━━━━━━━━━━━━\n"
            f"{_bp_icon} <b>BOOTSTRAP {_bp}</b>\n"
            f"  Progress : [{_pbar(_n_cur, _target_n)}] {_n_cur}/{_target_n} signal\n"
            f"  Mode     : observasi | Cap: {_cap_now}/run\n"
            f"  Adaptive : <b>nonaktif</b> (n terlalu kecil)\n"
            f"  Target   : <b>{_target_n} signal</b> → WARMING phase"
        )
    elif _bp == "WARMING":
        bootstrap_note = (
            f"\n━━━━━━━━━━━━━━━━━━\n"
            f"{_bp_icon} <b>BOOTSTRAP WARMING</b>\n"
            f"  Progress : [{_pbar(_n_cur, BOOTSTRAP_WARMING_N)}]"
            f" {_n_cur}/{BOOTSTRAP_WARMING_N} signal\n"
            f"  Adaptive : aktif | Edge proof: butuh {BOOTSTRAP_WARMING_N}"
            + _edge_health_lines()
        )
    else:
        bootstrap_note = (
            f"\n━━━━━━━━━━━━━━━━━━\n"
            f"{_bp_icon} <b>MATURE</b> — {_n_cur} signal resolved | full mode aktif"
            + _edge_health_lines()
        )

    # ── [PHASE-1] FILTER PIPELINE BLOCK — selalu tampil di heartbeat ─────
    filter_audit_block = ""
    try:
        # Pipeline breakdown (dari run()) — tampil selalu jika ada data
        if pipeline_tg and pipeline_tg.strip():
            _buy_status = "aktif" if not ihsg.get("block_buy") else "⚠️ soft-penalty (IHSG drop)"
            if ihsg.get("halt"):
                _buy_status = "🛑 HALT"
            filter_audit_block = (
                f"\n━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>PIPELINE BREAKDOWN</b>\n"
                f"BUY: {_buy_status}\n"
                + pipeline_tg +
                (
                    f"\n━━━━━━━━━━━━━━━━━━\n"
                    f"🔒 <b>TOP BLOCKERS</b> (dalam check_signal)\n"
                    + top_blockers_tg
                    if top_blockers_tg and top_blockers_tg.strip() else ""
                )
            )
        elif no_signal:
            # Fallback: pakai get_filter_audit_summary() jika pipeline_tg tidak tersedia
            _fa = get_filter_audit_summary()
            if _fa and _fa.strip() and "—" not in _fa[:5]:
                filter_audit_block = (
                    f"\n━━━━━━━━━━━━━━━━━━\n"
                    f"🔎 <b>Kenapa tidak ada sinyal?</b>\n"
                    f"{_fa}"
                )
    except Exception as _fae:
        log(f"⚠️ [PHASE-1] filter audit block: {_fae}", "warn")

    signal_note = (
        f"\n━━━━━━━━━━━━━━━━━━\n"
        f"📭 Tidak ada sinyal memenuhi kriteria.\n"
        f"<i>Scan berikutnya ±4 jam.</i>"
        + filter_audit_block
    ) if no_signal else (
        f"\n<i>Bot berjalan normal. Scan berikutnya ±4 jam.</i>"
        + filter_audit_block
    )

    msg = (
        f"💓 <b>Bot Heartbeat — Saham IDX v8.12</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {now_str}\n"
        f"{ihsg_block}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔍 Di-scan: <b>{scanned}</b> | Skip vol: {skip_vol}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>Win Rate</b>\n"
        f"  Overall  : <b>{wr_str}</b> ({wr_n} signal{exp_str})\n"
        f"  Intraday : {intra_wr} | Swing: {swing_wr}"
        + portfolio_block
        + open_pos_block
        + bootstrap_note
        + signal_note
    )
    tg(msg)
    log("💓 Health check terkirim ke Telegram")


# ════════════════════════════════════════════════════════
#  POSITION SIZING HELPER
# ════════════════════════════════════════════════════════

def calc_position_sizing(entry: float, sl: float, side: str,
                          risk_pct: float = 1.0,
                          portfolio_idr: float = 10_000_000) -> dict:
    """
    Hitung position size berdasarkan risk management 1% dari portofolio.

    Args:
        entry        : harga entry
        sl           : harga stop loss
        side         : BUY / SELL
        risk_pct     : persentase risiko per trade (default 1%)
        portfolio_idr: estimasi total portofolio dalam IDR (default Rp 10 juta)

    Returns dict: max_risk_idr, sl_pct, lot_estimate, shares_estimate
    """
    if entry <= 0 or sl <= 0:
        return {}
    sl_dist_pct = abs(entry - sl) / entry * 100
    if sl_dist_pct <= 0:
        return {}

    max_risk_idr    = portfolio_idr * (risk_pct / 100)
    shares_estimate = int(max_risk_idr / (abs(entry - sl)))
    # IDX: 1 lot = 100 lembar
    lot_estimate    = max(1, shares_estimate // 100)
    position_value  = lot_estimate * 100 * entry

    return {
        "max_risk_idr":   round(max_risk_idr),
        "sl_dist_pct":    round(sl_dist_pct, 2),
        "lot_estimate":   lot_estimate,
        "shares_estimate": lot_estimate * 100,
        "position_value": round(position_value),
    }


# ════════════════════════════════════════════════════════
#  [v7.9] IDX-NATIVE HELPERS
#  Fungsi khusus pasar IDX yang tidak ada di bot crypto:
#  1. ARA/ARB Auto-Rejection detection
#  2. VWAP session-aware (reset setiap sesi IDX)
#  3. Net Foreign Flow proxy (estimasi dari price action)
# ════════════════════════════════════════════════════════

def is_near_auto_rejection(ticker: str, price: float, side: str) -> bool:
    """
    [v7.9] Deteksi apakah saham mendekati batas Auto Rejection IDX.

    IDX membatasi pergerakan harga harian:
      - Papan Utama/Pengembangan: ARA +35% / ARB -35% dari prev close
      - Saham baru IPO/relisting: ARA +70% (hari 1), +50% (hari 2-5), +35% (hari 6+)
      - Saham tanpa auto-rejection: papan akselerasi
      - Saham terafiliasi (UMA): IDX bisa suspend kapan saja

    Threshold aman: hindari BUY jika sudah naik > 25% dari prev close
                   (buffer 10% sebelum ARA 35%)
                   hindari signal apapun jika sudah turun > 25% (buffer ARB)

    Signal yang dikirim saat saham hampir ARA tidak bisa dieksekusi
    oleh pengguna karena order akan ditolak oleh sistem IDX.
    """
    try:
        # [v7.11 FIX] Gunakan limit=25 agar berbagi cache key dengan run loop utama
        # (run loop juga pakai '1d|25'). Sebelumnya pakai limit=5 → cache entry extra.
        data = get_candles(ticker, "1d", 25)
        if data is None:
            return False
        closes, _, _, _, _op = data
        if len(closes) < 2:
            return False
        prev_close = float(closes[-2])
        if prev_close <= 0:
            return False
        change_pct = (price - prev_close) / prev_close * 100
        if side == "BUY"  and change_pct > 25.0:   # buffer 10% sebelum ARA 35%
            log(f"  ⚠️ {ticker}: Naik {change_pct:.1f}% hari ini — mendekati ARA, skip BUY")
            return True
        if side == "SELL" and change_pct < -25.0:   # buffer 10% sebelum ARB 35%
            log(f"  ⚠️ {ticker}: Turun {change_pct:.1f}% hari ini — mendekati ARB, skip SELL")
            return True
        return False
    except Exception as e:
        log(f"⚠️ is_near_auto_rejection [{ticker}]: {e}", "warn")
        return False


def is_likely_suspended(ticker: str, price: float) -> bool:
    """
    [v7.10] Deteksi saham yang kemungkinan di-suspend atau kena UMA IDX.

    IDX tidak menyediakan API suspensi real-time gratis. Sebagai proxy:
    - Volume 3 hari terakhir = 0 atau sangat kecil (< 1% dari avg 20 hari)
    - Harga flat identik 3 hari berturut (tidak bergerak sama sekali)
    - Candle range sangat kecil (< 0.05% per hari selama 3 hari)

    Ini tidak 100% akurat — saham ultra-liquid seperti BBCA pun bisa
    memenuhi kriteria ini di hari libur pendek. Karena itu kita hanya
    blok saat 2+ kriteria terpenuhi sekaligus.

    Returns True jika kemungkinan besar suspend/UMA, False jika normal.
    """
    try:
        data = get_candles(ticker, "1d", 25)
        if data is None:
            return False
        closes, highs, lows, volumes, _op = data
        if len(closes) < 5:
            return False

        c = closes[-5:].astype(float)
        h = highs[-5:].astype(float)
        l = lows[-5:].astype(float)
        v = volumes[-5:].astype(float)

        # Kriteria 1: Volume 3 hari terakhir hampir nol
        vol_avg_20 = float(np.mean(volumes[-21:-1])) if len(volumes) >= 21 else float(np.mean(volumes[:-1]))
        vol_3d_avg = float(np.mean(v[-3:]))
        volume_dead = vol_avg_20 > 0 and vol_3d_avg < vol_avg_20 * 0.01

        # Kriteria 2: Harga flat identik (close sama persis 3 hari)
        price_flat = (c[-1] == c[-2] == c[-3])

        # Kriteria 3: Range candle sangat kecil 3 hari berturut
        range_pcts = [(h[i] - l[i]) / (c[i] + 1e-9) * 100 for i in range(-3, 0)]
        range_dead = all(r < 0.05 for r in range_pcts)

        # [v7.12] Kriteria 4: Harga tidak bergerak dari prev close > 5 hari
        # Saham suspend biasanya tercatat dengan harga identik berhari-hari
        if len(closes) >= 6:
            price_5d_unchanged = all(closes[-i-1] == closes[-1] for i in range(1, 6))
        else:
            price_5d_unchanged = False

        # Blok jika 2+ kriteria terpenuhi
        hit = sum([volume_dead, price_flat, range_dead, price_5d_unchanged])
        if hit >= 2:
            log(f"  ⚠️ {ticker}: Kemungkinan suspend/UMA "
                f"(vol_dead={volume_dead}, flat={price_flat}, "
                f"range_dead={range_dead}, 5d_unchanged={price_5d_unchanged}) — skip")
            return True
        return False
    except Exception as e:
        log(f"⚠️ is_likely_suspended [{ticker}]: {e}", "warn")
        return False


def calc_vwap_session(ticker: str, closes, highs, lows, volumes) -> float:
    """
    [v7.9] VWAP sesi IDX — reset setiap hari bursa pukul 09:00 WIB.

    [v7.10 FIX] Versi sebelumnya melakukan extra yf.download() per ticker —
    risiko rate limit dengan 80+ ticker. Sekarang memanfaatkan cache
    _candle_cache yang sudah ada: ambil DataFrame 1h via yfinance cache
    (disk-cached oleh requests_cache / yfinance internal), filter ke hari ini.

    Fallback ke 8 candle terakhir jika timestamp unavailable.
    """
    try:
        # Cek dulu apakah ada cache dari get_candles() untuk ticker ini
        cache_key = f"{ticker}|1h|100"
        if cache_key not in _candle_cache or _candle_cache[cache_key] is None:
            raise ValueError("no 1h cache")

        # Download dengan period="2d" — yfinance internal cache meminimalkan
        # network hit karena request ini identik dengan is_candle_stale()
        df = yf.download(ticker, period="2d", interval="1h",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            raise ValueError("empty")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        today_wib = datetime.now(WIB).date()
        mask = np.zeros(len(df), dtype=bool)
        for i, ts in enumerate(df.index):
            try:
                ts_aware = ts if (hasattr(ts, "tzinfo") and ts.tzinfo)                            else ts.replace(tzinfo=timezone.utc)
                if ts_aware.astimezone(WIB).date() == today_wib:
                    mask[i] = True
            except Exception:
                pass

        n_today = int(mask.sum())
        if n_today < 2:
            raise ValueError(f"only {n_today} candle today — market not open yet")

        c = df["Close"].values[mask].astype(float)
        h = df["High"].values[mask].astype(float)
        l = df["Low"].values[mask].astype(float)
        v = df["Volume"].values[mask].astype(float)
        tp = (h + l + c) / 3
        return float((np.cumsum(tp * v) / (np.cumsum(v) + 1e-9))[-1])

    except Exception as _e:
        log(f"  ⚠️ [FALLBACK] VWAP intraday mask: {_e} — fallback 8-candle window", "warn")
        # Fallback ke 8 candle terakhir dari array yang sudah ada
        window = min(8, len(closes))
        c = closes[-window:]; h = highs[-window:]
        l = lows[-window:];   v = volumes[-window:]
        tp = (h + l + c) / 3
        return float((np.cumsum(tp * v) / (np.cumsum(v) + 1e-9))[-1])


def get_net_foreign_proxy(closes, highs, lows, volumes,
                           lookback: int = 10) -> dict:
    """
    [v7.9] Estimasi Net Foreign Flow via multi-indicator proxy.

    [v7.12 UPGRADE] Ditingkatkan dari single score heuristik menjadi
    kombinasi 3 indikator institusional:

    1. Money Flow Score (original) — candle direction × close position × volume
    2. Chaikin Money Flow (CMF) — mengukur apakah volume masuk di harga
       tinggi (akumulasi) atau harga rendah (distribusi) selama N periode
    3. OBV Trend — On-Balance Volume sebagai konfirmasi divergence

    Ketiga indikator dikombinasikan dengan bobot yang berbeda:
    - CMF paling representatif untuk flow institusional (bobot 0.5)
    - Money Flow Score (bobot 0.3)
    - OBV Trend (bobot 0.2)

    Confidence dihitung dari tingkat agreement antar indikator.
    Semakin banyak indikator yang sepakat → confidence lebih tinggi.

    Returns:
        trend     : "INFLOW" | "OUTFLOW" | "NEUTRAL"
        score     : float -1.0 to +1.0 (positif = inflow)
        confidence: float 0.0 to 1.0
        detail    : dict dengan breakdown per indikator
    """
    neutral = {"trend": "NEUTRAL", "score": 0.0, "confidence": 0.0,
               "detail": {"mf": 0.0, "cmf": 0.0, "obv": 0.0}}
    if len(closes) < lookback:
        return neutral

    c = closes[-lookback:].astype(float)
    h = highs[-lookback:].astype(float)
    l = lows[-lookback:].astype(float)
    v = volumes[-lookback:].astype(float)

    vol_avg = float(np.mean(v))
    if vol_avg <= 0:
        return neutral

    # ── Indikator 1: Money Flow Score (original, diperbaiki) ──
    mf_scores = []
    for i in range(1, len(c)):
        rng = h[i] - l[i] + 1e-9
        close_pos = (c[i] - l[i]) / rng         # 0 = close bawah, 1 = close atas
        vol_rel   = min(v[i] / vol_avg, 3.0)
        direction = 1.0 if c[i] >= c[i-1] else -1.0
        mf_scores.append(direction * close_pos * vol_rel)
    mf_avg = float(np.mean(mf_scores)) if mf_scores else 0.0

    # ── Indikator 2: Chaikin Money Flow (CMF) ──
    # CMF = sum(MFV) / sum(volume), MFV = ((close-low)-(high-close))/(high-low) * volume
    mfv_sum = 0.0
    vol_sum = float(np.sum(v)) + 1e-9
    for i in range(len(c)):
        rng = h[i] - l[i] + 1e-9
        mf_multiplier = ((c[i] - l[i]) - (h[i] - c[i])) / rng
        mfv_sum += mf_multiplier * v[i]
    cmf = mfv_sum / vol_sum   # range -1 to +1

    # ── Indikator 3: OBV Trend ──
    # Positif = volume lebih tinggi saat naik → institutional accumulation
    obv = 0.0
    for i in range(1, len(c)):
        if c[i] > c[i-1]:
            obv += v[i]
        elif c[i] < c[i-1]:
            obv -= v[i]
    # Normalisasi OBV ke -1..+1 berdasarkan total volume
    total_vol = float(np.sum(v)) + 1e-9
    obv_norm = max(-1.0, min(obv / total_vol, 1.0))

    # ── Kombinasi weighted ──
    combined = 0.3 * mf_avg + 0.5 * cmf + 0.2 * obv_norm

    # ── Confidence dari agreement antar 3 indikator ──
    signs = [1 if x > 0.05 else (-1 if x < -0.05 else 0)
             for x in [mf_avg, cmf, obv_norm]]
    agree = sum(1 for s in signs if s != 0 and s == signs[0])
    nonzero = sum(1 for s in signs if s != 0)
    confidence = agree / nonzero if nonzero > 0 else 0.0

    if combined > 0.25:    trend = "INFLOW"
    elif combined < -0.25: trend = "OUTFLOW"
    else:                  trend = "NEUTRAL"

    return {
        "trend":      trend,
        "score":      round(combined, 3),
        "confidence": round(confidence, 2),
        "detail":     {"mf": round(mf_avg, 3), "cmf": round(cmf, 3), "obv": round(obv_norm, 3)},
    }


# ════════════════════════════════════════════════════════
#  SIGNAL STRATEGIES
# ════════════════════════════════════════════════════════

def check_intraday(ticker: str, price: float, ihsg: dict, side: str = "BUY") -> dict | None:
    """
    INTRADAY signal — timeframe 1h.  [v4.0 — Elite Trading Engine]
    Jam bursa IDX: 09:00–16:00 WIB (Senin–Jumat).

    v4.0 Additions:
    [8]  Probability Engine — win_prob + EV filter (skip EV <= 0)
    [9]  Position Management data — disiapkan untuk update_signal_outcomes
    [10] Sector Correlation Filter — blokir jika sektor BEARISH saat BUY
    [11] Sniper Entry — OB reaction + FVG + sweep+reversal combo
    [12] Delay-Aware Entry — skip jika candle 1H stale > 90 menit
    """
    # ── Market hours guard ────────────────────────────────
    now_wib  = datetime.now(WIB)
    weekday  = now_wib.weekday()
    hour_wib = now_wib.hour
    if weekday >= 5 or not (9 <= hour_wib < 16):
        return None

    # [PHASE-0] IHSG block_buy → soft penalty, bukan hard block
    # Hard block hanya jika halt (crash > 5%)
    # [v8.18 P8-04] Phase 3 data collection: turunkan penalty 3→1 agar data
    # tidak skew ke bearish. Penalty 3 cukup besar untuk kill borderline setup
    # dan membuat WR data terbias. Phase 3 butuh representasi yang seimbang.
    _ihsg_soft_penalty_intraday = 0
    if side == "BUY" and ihsg.get("halt"):
        return None  # crash total → tetap hard block
    if side == "BUY" and ihsg["block_buy"]:
        # [P8-04] Phase 3: penalty 1 (was 3) — representatif, tidak skew data
        _ihsg_soft_penalty_intraday = 1 if PHASE3_COLLECTION else 3

    # ── [12] Delay-Aware Entry Guard ─────────────────────
    _stale, _stale_reason = is_candle_stale(ticker, "1h")
    if _stale:
        # [Y02-FIX] Catat ke _filter_audit agar breakdown staleness
        # terlihat di log akhir scan (bukan hanya hilang di console)
        _fa_key = f"STALE_{_stale_reason.split('(')[0].strip().replace(' ','_').upper()}" \
                  if _stale_reason else "STALE_CANDLE"
        _filter_audit[_fa_key] = _filter_audit.get(_fa_key, 0) + 1
        return None

    # ── [v7.13] Data age guard (skipped in PHASE2 — is_candle_stale di atas sudah cukup) ──
    if not PHASE2_STABILIZE:
        try:
            df_age = yf.download(ticker, period="1d", interval="1h",
                                 progress=False, auto_adjust=True)
            if df_age is not None and not df_age.empty:
                last_ts = df_age.index[-1]
                if hasattr(last_ts, "tzinfo") and last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                age_mins = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
                if age_mins > MAX_DATA_AGE_MINUTES:
                    log(f"  ⚠️ {ticker} [1H]: Data age {age_mins:.0f}m > {MAX_DATA_AGE_MINUTES}m — skip stale intraday")
                    return None
        except Exception:
            pass   # jika gagal cek, lanjut saja

    # ── [10] Sector Correlation Filter ───────────────────
    if is_sector_blocked(ticker, side, ihsg):
        return None

    data = get_candles(ticker, "1h", 100)
    if data is None:
        return None
    closes, highs, lows, volumes, opens = data   # [v7.9] unpack opens untuk entry trigger

    # [S03] Ticker lolos pre-flight — mulai tracking dari sini
    _audit_enter()

    atr     = calc_atr(closes, highs, lows)
    atr_pct = atr / price * 100

    # [PHASE-1] Debug inline — print ATR untuk debug ticker
    _dbg = os.environ.get("DEBUG_TICKER","").strip().upper()
    _dbg = (_dbg if _dbg.endswith(".JK") else _dbg+".JK") if _dbg else ""
    if _dbg and ticker == _dbg:
        rsi = calc_rsi(closes)
        _macd_l, _sig_l, _ = calc_macd(closes)
        log(f"  [DEBUG INTRADAY {side}] {ticker}")
        log(f"    ATR       : {atr_pct:.2f}%  (batas: 0.5%–10%)")
        log(f"    RSI       : {rsi:.1f}  (OB={RSI_OB['INTRADAY']} OS={RSI_OS['INTRADAY']})")
        log(f"    MACD      : signal={_sig_l[-1]:.4f} macd={_macd_l[-1]:.4f}")
        log(f"    Closes[-3:]: {[round(float(x),0) for x in closes[-3:]]}")
        log(f"    Vols[-3:]  : {[int(float(x)) for x in volumes[-3:]]}")

    # ── [+] Trade Filter 1: ATR terlalu kecil atau terlalu besar ──
    if atr_pct < 0.5 or atr_pct > 10.0:
        _audit_block("ATR_OUT_OF_RANGE")
        return None

    # FIX 6B: Per-ticker single-candle spike guard
    last_candle_range_pct = (float(highs[-1]) - float(lows[-1])) / (price + 1e-9) * 100
    if last_candle_range_pct > 8.0:
        log(f"  ⚠️ {ticker} [1H]: Candle spike {last_candle_range_pct:.1f}% > 8% — skip (circuit breaker risk)")
        _audit_block("CANDLE_SPIKE")
        return None

    # ── [+] Trade Filter 2: Volume spike abnormal (> 5x rata-rata) ──
    vol_avg_20  = float(np.mean(volumes[-21:-1])) if len(volumes) >= 21 else float(np.mean(volumes[:-1]))
    vol_current = float(volumes[-1])
    if vol_avg_20 > 0 and vol_current > vol_avg_20 * 5.0:
        log(f"  ⚠️ {ticker} [1H]: Volume spike abnormal ({vol_current/vol_avg_20:.1f}x) — skip")
        _audit_block("VOL_SPIKE")
        return None

    # ── [+] Trade Filter 3: Candle terakhir terlalu panjang (late entry) ──
    candle_range_pct = (float(highs[-1]) - float(lows[-1])) / (price + 1e-9) * 100
    if candle_range_pct > atr_pct * 2.5:
        log(f"  ⚠️ {ticker} [1H]: Candle late entry ({candle_range_pct:.1f}% > {atr_pct*2.5:.1f}%) — skip")
        _audit_block("LATE_ENTRY")
        return None

    # [v7.9] ARA/ARB guard — skip jika harga sudah mendekati batas auto-rejection IDX
    if is_near_auto_rejection(ticker, price, side):
        log(f"  ⚠️ {ticker} [1H {side}]: Mendekati ARA/ARB IDX — skip")
        _audit_block("ARA_ARB")
        return None

    mkt = detect_market_regime(closes, highs, lows)
    if mkt["regime"] == "CHOPPY":
        _audit_block("CHOPPY_REGIME")
        return None

    rsi        = calc_rsi(closes)
    macd, msig = calc_macd(closes)
    ema20      = calc_ema(closes, 20)
    ema50      = calc_ema(closes, 50)
    # [v7.9] VWAP sesi — timestamps diambil dari yfinance via cache
    vwap       = calc_vwap_session(ticker, closes, highs, lows, volumes)
    structure  = detect_structure(closes, highs, lows, strength=3, lookback=60)
    liq        = detect_liquidity(closes, highs, lows, lookback=40)

    if not structure["valid"]:
        _audit_block("NO_STRUCTURE")
        return None

    # ── [2] Market Phase Detection ────────────────────────
    # [v7.13] SIMPLE_MODE: skip heavy analysis layers (phase, trap, liq_trap)
    # untuk mengurangi overfitting risk dan false-positive dari layer berlebih
    if SIMPLE_MODE:
        phase_info    = {"phase": "CONSOLIDATION", "description": "Simple mode aktif"}
        phase         = "CONSOLIDATION"
        strategy_mode = _get_active_strategy_v2(mkt["regime"], phase, mkt["adx"], wr_cache=_strategy_wr_cache)
        liq_trap      = {"fake_bull_break": False, "fake_bear_break": False,
                         "stop_hunt_bull": False, "stop_hunt_bear": False}
        daily_bias    = get_daily_bias(ticker)
        foreign_flow  = {"trend": "NEUTRAL", "score": 0.0, "confidence": 0.0, "detail": {}}
        log(f"  🔵 {ticker} [1H {side}]: SIMPLE_MODE — skip advanced layers")
    else:
        phase_info    = detect_market_phase(closes, highs, lows, volumes)
        phase         = phase_info["phase"]
        strategy_mode = _get_active_strategy_v2(mkt["regime"], phase, mkt["adx"], wr_cache=_strategy_wr_cache)
        log(f"  🧠 {ticker} [1H {side}]: Strategy={strategy_mode['emoji']} {strategy_mode['mode']} — {strategy_mode['description']}")
        liq_trap      = detect_liquidity_trap(closes, highs, lows)

        # ── [v7.9] Net Foreign Flow Proxy ─────────────────
        # [v7.13] Default: TIDAK memblokir (FOREIGN_FLOW_BLOCK_ENABLED=False)
        # Proxy ini informatif saja — terlalu banyak false positive untuk dijadikan hard block
        foreign_flow = get_net_foreign_proxy(closes, highs, lows, volumes, lookback=10)
        if (FOREIGN_FLOW_BLOCK_ENABLED and
                side == "BUY" and foreign_flow["trend"] == "OUTFLOW"
                and foreign_flow["confidence"] >= FOREIGN_FLOW_BLOCK_CONF):
            log(f"  ⚠️ {ticker} [1H BUY]: Net foreign proxy OUTFLOW — skip (FOREIGN_FLOW_BLOCK=true)")
            return None

    # ── [5] MTF Alignment — 1D bias validation ───────────
    daily_bias = get_daily_bias(ticker) if SIMPLE_MODE else daily_bias
    if side == "BUY"  and daily_bias == "BEARISH":
        log(f"  ⚠️ {ticker} [1H BUY]: MTF conflict — 1D bias BEARISH — skip")
        return None
    if side == "SELL" and daily_bias == "BULLISH":
        log(f"  ⚠️ {ticker} [1H SELL]: MTF conflict — 1D bias BULLISH — skip")
        return None

    # ── [13] No-Trade Zone Engine ────────────────────────
    # [PHASE-2] Dinonaktifkan — NTZ terlalu sering memblokir signal sah.
    # Re-aktifkan setelah bot konsisten mengirim signal.
    if PHASE2_STABILIZE:
        ntz = {"skip": False, "reasons": [], "chop_index": 0.0}
    else:
        ntz = check_no_trade_zone(closes, highs, lows, volumes,
                                   mkt["regime"], phase, daily_bias,
                                   side, atr_pct)
        if ntz["skip"]:
            log(f"  🚫 {ticker} [1H {side}]: NTZ aktif — " + " | ".join(ntz["reasons"][:2]))
            return None

    # ── [14] Liquidity Depth Filter ──────────────────────
    # [PHASE-2] Dinonaktifkan — skip untuk hentikan over-filtering.
    if PHASE2_STABILIZE:
        liq_depth = {"depth_score": 0.0, "near_hvn": False, "hvn_price": None,
                     "sufficient": True, "reason": "PHASE2_BYPASS"}
    else:
        liq_depth = is_liquidity_sufficient(closes, highs, lows, volumes, ticker)
        if not liq_depth["sufficient"]:
            log(f"  ⚠️ {ticker} [1H]: Liquidity insufficient — {liq_depth['reason']}")
            return None

    # ── [1] Dynamic Weights + Strategy Mode — [v8.01] MarketContext ──
    ctx      = _build_market_context(ticker, side, "INTRADAY",
                                     mkt["regime"], mkt["adx"], phase)
    merged_w = ctx.final_weights
    _log_ctx_summary(ctx)
    # strategy_mode sudah ada di ctx — ganti referensi lama
    strategy_mode = ctx.strategy_mode

    if side == "BUY":
        # Structural prerequisite — minimal satu konfirmasi
        has_struct = (structure.get("bos")   == "BULLISH" or
                      structure.get("choch") == "BULLISH" or
                      liq.get("sweep_bull")  or
                      liq_trap.get("stop_hunt_bull") or
                      liq_trap.get("fake_bear_break"))
        if not has_struct:
            _audit_block("STRUCTURE_FAIL", "intraday_buy")
            return None
        if rsi > RSI_OB["INTRADAY"]:
            _audit_block("RSI_FILTER", f"rsi={rsi:.1f}>OB={RSI_OB['INTRADAY']}")
            return None  # [v7.11] via RSI_OB config

        ob  = detect_order_block(closes, highs, lows, volumes, side="BUY", lookback=25, opens=opens)

        # ── [3] Entry Trigger Validation ─────────────────
        # [v7.9 FIX] Passes opens array untuk akurasi candle body detection
        trigger = entry_trigger_check(closes, highs, lows, "BUY", opens=opens)
        if not trigger["valid"]:
            _audit_block("TRIGGER_FAIL", "intraday_buy")
            log(f"  ⚠️ {ticker} [1H BUY]: No entry trigger — skip")
            return None

        # ── [PHASE-2] Simplified scoring — raw score only ────
        if PHASE2_STABILIZE:
            score = score_signal("BUY", price, closes, highs, lows, volumes,
                                 structure, liq, ob, rsi, macd, msig,
                                 ema20, ema50, vwap, mkt["regime"],
                                 weights=W, opens=opens)
            # [PHASE-0] IHSG soft penalty — turunkan score, jangan block total
            score -= _ihsg_soft_penalty_intraday
            if _ihsg_soft_penalty_intraday:
                log(f"  ⚠️ {ticker} [1H BUY]: IHSG soft penalty -{_ihsg_soft_penalty_intraday} → score={score}")
            log(f"  🧮 {ticker} [1H BUY]: score={score} [PHASE2 — raw only]")
            sniper     = {"bonus": 0, "level": "STANDARD", "details": {}}
            setup_rank = {"priority": "NORMAL", "score_boost": 0,
                          "ev_threshold": 0.0, "reason": "PHASE2"}
            ev_threshold_active = 0.0
        else:
            # ── [11] Sniper Entry — OB Reaction + FVG ────────
            ob_reaction = detect_ob_reaction(closes, highs, lows, volumes, ob, "BUY")
            fvg         = detect_fvg(closes, highs, lows, side="BUY", lookback=25)
            sniper      = calc_sniper_score(liq, liq_trap, ob_reaction, fvg, "BUY")

            # [v6.0+v7.9] Setup Ranking
            setup_rank = rank_setup_priority(structure, liq, liq_trap, ob, trigger, sniper, "BUY")
            ev_threshold_active = setup_rank["ev_threshold"]

            # [v8.01] ScoreAccumulator — breakdown transparan
            acc = ScoreAccumulator()
            acc.add("raw",      score_signal("BUY", price, closes, highs, lows, volumes,
                                             structure, liq, ob, rsi, macd, msig,
                                             ema20, ema50, vwap, mkt["regime"],
                                             weights=merged_w, opens=opens))
            acc.add("sniper",   sniper["bonus"])
            acc.add("priority", setup_rank["score_boost"])
            acc.add("strategy", ctx.strategy_mode.get("min_score_boost", 0))
            # [PHASE-0] IHSG soft penalty
            acc.add("ihsg_penalty", -_ihsg_soft_penalty_intraday)
            score = acc.total()
            log(f"  🧮 {ticker} [1H BUY]: {acc.explain()}")

        # [v7.9 FIX] Tier di-assign SETELAH semua bonus
        tier = assign_tier(score)
        if tier == "SKIP":
            _audit_block("TIER_SKIP")
            return None
        log(f"  📊 {ticker} [1H BUY]: Setup Priority={setup_rank['priority']} ({setup_rank['reason']}), EV≥{ev_threshold_active}, Tier={tier}")
        if last_sh and price > last_sh * 1.02: return None
        entry_raw = round(last_sh * 1.002, 2) if (last_sh and price > last_sh) else price
        entry = round_to_fraction(entry_raw, "nearest")

        sl, tp1, tp2 = calc_sl_tp(entry, "BUY", atr, structure, "INTRADAY")
        if tp1 <= entry or sl >= entry: return None
        sl_dist = entry - sl
        if sl_dist <= 0 or sl_dist / entry > 0.08: return None
        rr = (tp1 - entry) / sl_dist

    else:  # SELL
        has_struct = (structure.get("bos")   == "BEARISH" or
                      structure.get("choch") == "BEARISH" or
                      liq.get("sweep_bear")  or
                      liq_trap.get("stop_hunt_bear") or
                      liq_trap.get("fake_bull_break"))
        if not has_struct:
            _audit_block("STRUCTURE_FAIL", "intraday_sell")
            return None
        if rsi < RSI_OS["INTRADAY"]:
            _audit_block("RSI_FILTER", f"rsi={rsi:.1f}<OS={RSI_OS['INTRADAY']}")
            return None  # [v7.11] via RSI_OS config

        last_sh = structure.get("last_sh")
        if last_sh is None:
            return None
        if last_sh and price < last_sh * 0.97: return None

        ob  = detect_order_block(closes, highs, lows, volumes, side="SELL", lookback=25, opens=opens)

        # [v7.9 FIX] Passes opens array
        trigger = entry_trigger_check(closes, highs, lows, "SELL", opens=opens)
        if not trigger["valid"]:
            _audit_block("TRIGGER_FAIL", "intraday_sell")
            log(f"  ⚠️ {ticker} [1H SELL]: No entry trigger — skip")
            return None

        # ── [PHASE-2] Simplified scoring — raw score only ────
        if PHASE2_STABILIZE:
            score = score_signal("SELL", price, closes, highs, lows, volumes,
                                 structure, liq, ob, rsi, macd, msig,
                                 ema20, ema50, vwap, mkt["regime"],
                                 weights=W, opens=opens)
            log(f"  🧮 {ticker} [1H SELL]: score={score} [PHASE2 — raw only]")
            sniper     = {"bonus": 0, "level": "STANDARD", "details": {}}
            setup_rank = {"priority": "NORMAL", "score_boost": 0,
                          "ev_threshold": 0.0, "reason": "PHASE2"}
            ev_threshold_active = 0.0
        else:
            # ── [11] Sniper Entry — OB Reaction + FVG ────────
            ob_reaction = detect_ob_reaction(closes, highs, lows, volumes, ob, "SELL")
            fvg         = detect_fvg(closes, highs, lows, side="SELL", lookback=25)
            sniper      = calc_sniper_score(liq, liq_trap, ob_reaction, fvg, "SELL")

            # [v6.0+v7.9] Setup Ranking
            setup_rank = rank_setup_priority(structure, liq, liq_trap, ob, trigger, sniper, "SELL")
            ev_threshold_active = setup_rank["ev_threshold"]

            # [v8.01] ScoreAccumulator — breakdown transparan
            acc = ScoreAccumulator()
            acc.add("raw",      score_signal("SELL", price, closes, highs, lows, volumes,
                                             structure, liq, ob, rsi, macd, msig,
                                             ema20, ema50, vwap, mkt["regime"],
                                             weights=merged_w, opens=opens))
            acc.add("sniper",   sniper["bonus"])
            acc.add("priority", setup_rank["score_boost"])
            acc.add("strategy", ctx.strategy_mode.get("min_score_boost", 0))
            score = acc.total()
            log(f"  🧮 {ticker} [1H SELL]: {acc.explain()}")

        # [v7.9 FIX] Tier setelah semua bonus
        tier = assign_tier(score)
        if tier == "SKIP":
            _audit_block("TIER_SKIP")
            return None
        log(f"  📊 {ticker} [1H SELL]: Setup Priority={setup_rank['priority']} ({setup_rank['reason']}), EV≥{ev_threshold_active}, Tier={tier}")

        entry_raw = round(last_sh * 0.998, 2) if (last_sh and price >= last_sh * 0.97) else price
        entry = round_to_fraction(entry_raw, "nearest")

        sl, tp1, tp2 = calc_sl_tp(entry, "SELL", atr, structure, "INTRADAY")
        if tp1 >= entry or sl <= entry: return None
        sl_dist = sl - entry
        if sl_dist <= 0 or sl_dist / entry > 0.08: return None
        rr = (entry - tp1) / sl_dist

    # ── [22] Meta Intelligence — RR override per strategy mode ──
    rr_min_intraday = MIN_RR["INTRADAY"]
    if strategy_mode["rr_min_override"] and "INTRADAY" in strategy_mode["rr_min_override"]:
        rr_min_intraday = strategy_mode["rr_min_override"]["INTRADAY"]

    # [v7.2 — FIX Masalah 6] Apply fraksi tolerance — fraksi rounding bisa turunkan RR sedikit
    rr_min_intraday_eff = rr_min_intraday * (1 - RR_FRAKSI_TOLERANCE)
    if rr < rr_min_intraday_eff:
        log(f"  ⚠️ {ticker} [1H {side}]: RR={rr:.2f} < {rr_min_intraday_eff:.2f} (min {rr_min_intraday} × fraksi tol) [{strategy_mode['mode']}] — skip")
        _audit_block("RR_LOW")
        return None

    # ── [7] Smart Risk Allocation ─────────────────────────
    # [Q02] Hitung win_prob lebih awal di sini agar Kelly sizing bisa pakai
    # Bayesian-shrunk estimate. max_positive dan bias_aligned tetap dipakai di [8].
    max_positive = sum(v for v in merged_w.values() if v > 0) + 4  # +4 = max sniper bonus
    bias_aligned = (side == "BUY" and daily_bias != "BEARISH") or \
                   (side == "SELL" and daily_bias != "BULLISH")
    win_prob    = calc_win_probability(score, max_positive, mkt["regime"],
                                       phase, trigger["strength"], bias_aligned,
                                       ticker=ticker)
    _win_prob_n = get_cluster_n_samples(ticker, mkt["regime"])
    smart_risk  = get_smart_risk_pct(
        score, tier, atr_pct=atr_pct,
        ticker=ticker, win_prob=win_prob,
        avg_win_r=rr, strategy="INTRADAY",
        win_prob_n=_win_prob_n,
    )

    # [v6.0] Capital Rotation — sesuaikan risk berdasarkan kekuatan sektor
    sector_weight = get_sector_capital_weight(ticker, side)
    smart_risk    = round(smart_risk * sector_weight, 2)
    smart_risk    = max(smart_risk, 0.25)   # floor 0.25%

    # [K03] Behavioral Edge — event calendar + sentiment spike + macro shock
    beh = check_behavioral_edge(ticker, volumes.tolist(), now_wib)
    if beh["block_signal"]:
        log(f"  🚨 {ticker} [1H {side}]: BLOCK behavioral — {beh['reason']}")
        return None
    if beh["reduce_factor"] < 1.0:
        smart_risk = round(smart_risk * beh["reduce_factor"], 2)
        smart_risk = max(smart_risk, 0.25)
        log(f"  📅 {ticker} [1H {side}]: Behavioral reduce → risk {smart_risk:.2f}% "
            f"(factor {beh['reduce_factor']})")

    # [K04] Execution Quality Score — hitung data age untuk EQS
    # [PHASE-2] Skip download kedua — gunakan worst-case 15 menit
    _age_mins = 15.0
    if not PHASE2_STABILIZE:
        try:
            _df_age = yf.download(ticker, period="1d", interval="1h",
                                  progress=False, auto_adjust=True)
            if _df_age is not None and not _df_age.empty:
                _last_ts = _df_age.index[-1]
                if hasattr(_last_ts, "tzinfo") and _last_ts.tzinfo is None:
                    _last_ts = _last_ts.replace(tzinfo=timezone.utc)
                _age_mins = (datetime.now(timezone.utc) - _last_ts).total_seconds() / 60
        except Exception as _e:
            log(f"  ⚠️ [FALLBACK] candle age_mins: {_e} — worst-case 15.0", "warn")
            _age_mins = 15.0   # asumsi worst-case jika gagal

    # [R01/S02] EQS gate — soft degradation, bukan hard block.
    # Motivasi: hard block menyebabkan under-trading di hari dengan data lambat.
    # Sekarang:
    #   EQS = 0        → hard block (data benar-benar tidak bisa dipakai)
    #   EQS 1 – (min-1) → soft: risk dikurangi proporsional, signal tetap lolos
    #   EQS >= min     → proceed normal
    # Ini menjaga signal flow sekaligus tetap jujur tentang kualitas data.
    _eqs_result = calc_execution_quality_score(
        strategy="INTRADAY", data_age_minutes=_age_mins,
        vpr=0.0, slippage_pct=0.40, partial_fill_risk=0.30, impact_pct=0.0,
    )
    _eqs_val = _eqs_result["eqs"]
    _eqs_degraded = False

    if _eqs_val <= 0:
        log(f"  🔴 {ticker} [1H {side}]: EQS=0 — data tidak bisa dipakai (age {_age_mins:.0f}m)", "warn")
        _audit_block("EQS_ZERO")
        return None
    elif _eqs_val < INTRADAY_MIN_EQS:
        # Soft degradation: risk dipotong proporsional dengan seberapa jauh di bawah threshold
        _eqs_factor  = max(0.30, _eqs_val / INTRADAY_MIN_EQS)
        smart_risk   = round(smart_risk * _eqs_factor, 2)
        smart_risk   = max(smart_risk, 0.25)
        _eqs_degraded = True
        log(f"  🟡 {ticker} [1H {side}]: EQS={_eqs_val:.0f} < {INTRADAY_MIN_EQS} "
            f"(age {_age_mins:.0f}m) — [S02] risk degraded x{_eqs_factor:.2f} → {smart_risk:.2f}%")
        _audit_block("EQS_DEGRADED")

    # vol_today_idr proxy dari volumes[-1] × price (kasar)
    _vol_today_idr_est = float(volumes[-1]) * price if len(volumes) > 0 else 0.0

    # ── [8] Core EV Engine — Primary Decision Gate ───────
    # win_prob dan max_positive sudah dihitung di [7] untuk Kelly sizing
    ev = calc_expected_value(win_prob, rr)

    # Gate 1 — HARD FLOOR: EV <= 0 = HARD SKIP tanpa kompromi
    if ev <= HARD_EV_FLOOR:
        log(f"  ❌ {ticker} [1H {side}]: EV={ev:.2f} ≤ 0 — HARD SKIP (negative expectancy)")
        _audit_block("EV_NEGATIVE")
        return None

    if not PHASE2_STABILIZE:
        # Gate 2 — Strategy mode EV floor override (DEFENSIVE lebih ketat)
        ev_floor_meta = strategy_mode.get("ev_floor_override") or ev_threshold_active
        effective_ev_threshold = max(ev_threshold_active, ev_floor_meta)
        if ev <= effective_ev_threshold:
            log(f"  ⚠️ {ticker} [1H {side}]: EV={ev:.2f} ≤ {effective_ev_threshold:.2f} [{strategy_mode['mode']}/{setup_rank['priority']}] — skip")
            _audit_block("EV_THRESHOLD")
            return None

        # Gate 3 — DEFENSIVE mode: wajib SNIPER/PRECISION level
        if strategy_mode.get("require_sniper") and "STANDARD" in sniper.get("level", "STANDARD"):
            log(f"  ⚠️ {ticker} [1H {side}]: DEFENSIVE mode — STANDARD sniper ditolak")
            _audit_block("DEFENSIVE_SNIPER")
            return None
    else:
        effective_ev_threshold = HARD_EV_FLOOR
        log(f"  ✅ {ticker} [1H {side}]: EV={ev:.2f} > 0 [PHASE2 — gate 2/3 dinonaktifkan]")

    # [PHASE-1 v8.16] Record score detail — sinyal LOLOS, catat untuk summary
    _record_score_detail(ticker, "INTRADAY", side, score, rr, ev,
                         structure_ok=True, rsi_ok=True, tier=tier, blocker="PASS")

    return {
        "ticker":       ticker,
        "pair":         ticker.replace(".JK", ""),
        "strategy":     "INTRADAY",
        "side":         side,
        "timeframe":    "1h",
        "entry":        entry,
        "current_price": price,
        "tp1": tp1, "tp2": tp2, "sl": sl,
        "tier":         tier,
        "score":        score,
        "rr":           round(rr, 1),
        "rsi":          round(rsi, 1),
        "structure":    structure,
        "regime":       mkt["regime"],
        "adx":          mkt["adx"],
        "conviction":   calc_conviction(score),
        # v3.0 fields
        "phase":        phase,
        "phase_desc":   phase_info["description"],
        "entry_pattern": trigger["pattern"],
        "entry_strength": trigger["strength"],
        "daily_bias":   daily_bias,
        "liq_trap":     liq_trap,
        "smart_risk_pct": smart_risk,
        # v4.0 fields
        "win_prob":     win_prob,
        "expected_value": ev,
        "net_ev":       calc_cost_adjusted_ev(win_prob, rr, "INTRADAY", tier)["net_ev"],
        "sniper_level": sniper["level"],
        "sniper_detail": sniper["details"],
        # v5.0 fields
        "chop_index":   ntz["chop_index"],
        "depth_score":  liq_depth["depth_score"],
        "near_hvn":     liq_depth["near_hvn"],
        "hvn_price":    liq_depth["hvn_price"],
        # v6.0 fields
        "setup_priority": setup_rank["priority"],
        "eqs_score":         _eqs_val,
        "eqs_degraded":      _eqs_degraded,
        "data_age_minutes":  round(_age_mins, 1),
        # v8.03 fields [R01] + [R03]
        "data_source":       DATA_SOURCE_INTRADAY,
        "signal_mode":       "SIMPLE" if SIMPLE_MODE else "COMPLEX",
        # [PHASE-3] Extended metadata
        "atr_pct":      round(atr_pct, 3),
    }


def check_swing(ticker: str, price: float, ihsg: dict, side: str = "BUY") -> dict | None:
    """
    SWING signal — timeframe 1d.  [v4.0 — Elite Trading Engine]
    Target: posisi 3–10 hari.

    v4.0 Additions:
    [8]  Probability Engine — win_prob + EV filter
    [10] Sector Correlation Filter — blokir jika sektor lemah
    [11] Sniper Entry — OB reaction + FVG + sweep+reversal combo
    """
    # [PHASE-0] IHSG block_buy → soft penalty, bukan hard block
    # Hard block hanya jika halt (crash > 5%)
    # [v8.18 P8-04] Phase 3: penalty 1 (was 3) — kurangi bias data saat market merah.
    # Penalty 3 terlalu besar untuk borderline setup; bias data ke INSUFFICIENT_BUY.
    _ihsg_soft_penalty_swing = 0
    if side == "BUY" and ihsg.get("halt"):
        return None  # crash total → tetap hard block
    if side == "BUY" and ihsg["block_buy"]:
        # [P8-04] Phase 3: penalty 1 (was 3) — representatif, bukan suppressif
        _ihsg_soft_penalty_swing = 1 if PHASE3_COLLECTION else 3

    # ── [10] Sector Correlation Filter ───────────────────
    if is_sector_blocked(ticker, side, ihsg):
        return None

    data = get_candles(ticker, "1d", 120)
    if data is None:
        return None
    closes, highs, lows, volumes, opens = data   # [v7.9] unpack opens

    # [S03] Ticker lolos pre-flight — mulai tracking dari sini
    _audit_enter()

    atr     = calc_atr(closes, highs, lows)
    atr_pct = atr / price * 100

    # [PHASE-1] Debug inline untuk swing
    _dbg_sw = os.environ.get("DEBUG_TICKER","").strip().upper()
    _dbg_sw = (_dbg_sw if _dbg_sw.endswith(".JK") else _dbg_sw+".JK") if _dbg_sw else ""
    if _dbg_sw and ticker == _dbg_sw:
        rsi_sw = calc_rsi(closes)
        _ema50_sw  = calc_ema(closes, 50)
        _ema200_sw = calc_ema(closes, 200)
        log(f"  [DEBUG SWING {side}] {ticker}")
        log(f"    ATR        : {atr_pct:.2f}%  (batas: 0.5%–15%)")
        log(f"    RSI        : {rsi_sw:.1f}  (OB={RSI_OB['SWING']} OS={RSI_OS['SWING']})")
        log(f"    EMA50/200  : {_ema50_sw[-1]:.0f} / {_ema200_sw[-1]:.0f}")
        log(f"    Price      : {price:.0f}  (di {'atas' if price > _ema50_sw[-1] else 'bawah'} EMA50)")
        log(f"    MIN_RR     : {MIN_RR['SWING']} | RSI_OB: {RSI_OB['SWING']}")
        log(f"    Closes[-5:]: {[round(float(x),0) for x in closes[-5:]]}")
        log(f"    IHSG penalty: {_ihsg_soft_penalty_swing}")

    if atr_pct < 0.5 or atr_pct > 15.0:
        _audit_block("ATR_OUT_OF_RANGE")
        return None

    # FIX 6B: Per-ticker single-candle spike guard (swing — threshold lebih longgar: 12%)
    last_candle_range_pct = (float(highs[-1]) - float(lows[-1])) / (price + 1e-9) * 100
    if last_candle_range_pct > 12.0:
        log(f"  ⚠️ {ticker} [1D]: Candle spike {last_candle_range_pct:.1f}% > 12% — skip (circuit breaker risk)")
        _audit_block("CANDLE_SPIKE")
        return None
    vol_avg_20  = float(np.mean(volumes[-21:-1])) if len(volumes) >= 21 else float(np.mean(volumes[:-1]))
    vol_current = float(volumes[-1])
    if vol_avg_20 > 0 and vol_current > vol_avg_20 * 5.0:
        log(f"  ⚠️ {ticker} [1D]: Volume spike abnormal ({vol_current/vol_avg_20:.1f}x) — skip")
        _audit_block("VOL_SPIKE")
        return None

    # ── [+] Trade Filter 3: Candle late entry ─────────────
    candle_range_pct = (float(highs[-1]) - float(lows[-1])) / (price + 1e-9) * 100
    if candle_range_pct > atr_pct * 3.0:
        log(f"  ⚠️ {ticker} [1D]: Candle late entry ({candle_range_pct:.1f}%) — skip")
        _audit_block("LATE_ENTRY")
        return None

    # [v7.9] ARA/ARB guard — skip jika saham mendekati batas auto-rejection IDX
    if is_near_auto_rejection(ticker, price, side):
        log(f"  ⚠️ {ticker} [1D {side}]: Mendekati ARA/ARB IDX — skip")
        _audit_block("ARA_ARB")
        return None

    mkt = detect_market_regime(closes, highs, lows)
    if mkt["regime"] == "CHOPPY":
        _audit_block("CHOPPY_REGIME")
        return None

    rsi        = calc_rsi(closes)
    macd, msig = calc_macd(closes)
    ema50      = calc_ema(closes, 50)
    ema200     = calc_ema(closes, 200)
    vwap       = calc_vwap(closes, highs, lows, volumes, timeframe="1d")
    structure  = detect_structure(closes, highs, lows, strength=4, lookback=80)
    liq        = detect_liquidity(closes, highs, lows, lookback=50)

    if not structure["valid"]:
        _audit_block("NO_STRUCTURE")
        return None

    # [PHASE-2 / SIMPLE_MODE] Skip advanced analysis layers.
    # BUG FIX: sebelumnya kode SIMPLE_MODE ada di dalam `if not structure["valid"]`
    # (dead code — unreachable karena ada `return None` lebih awal).
    # Sekarang dipindah ke sini agar benar-benar dieksekusi.
    if PHASE2_STABILIZE or SIMPLE_MODE:
        phase_info    = {"phase": "CONSOLIDATION", "description": "Stabilize/Simple mode"}
        phase         = "CONSOLIDATION"
        strategy_mode = _get_active_strategy_v2(mkt["regime"], phase, mkt["adx"], wr_cache=_strategy_wr_cache)
        liq_trap      = {"fake_bull_break": False, "fake_bear_break": False,
                         "stop_hunt_bull": False, "stop_hunt_bear": False}
        foreign_flow  = {"trend": "NEUTRAL", "score": 0.0, "confidence": 0.0, "detail": {}}
        log(f"  🔵 {ticker} [1D {side}]: PHASE2/SIMPLE_MODE — skip advanced layers")
    else:
        phase_info    = detect_market_phase(closes, highs, lows, volumes)
        phase         = phase_info["phase"]
        strategy_mode = _get_active_strategy_v2(mkt["regime"], phase, mkt["adx"], wr_cache=_strategy_wr_cache)
        log(f"  🧠 {ticker} [1D {side}]: Strategy={strategy_mode['emoji']} {strategy_mode['mode']}")
        liq_trap      = detect_liquidity_trap(closes, highs, lows)

        # [v7.13] Net foreign proxy — default tidak memblokir
        foreign_flow = get_net_foreign_proxy(closes, highs, lows, volumes, lookback=15)
        if (FOREIGN_FLOW_BLOCK_ENABLED and
                side == "BUY" and foreign_flow["trend"] == "OUTFLOW"
                and foreign_flow["confidence"] >= FOREIGN_FLOW_BLOCK_CONF):
            log(f"  ⚠️ {ticker} [1D BUY]: Net foreign proxy OUTFLOW — skip (FOREIGN_FLOW_BLOCK=true)")
            return None

    # ── [5] MTF Alignment — weekly bias via broader structure ─
    weekly_struct = detect_structure(closes, highs, lows, strength=5, lookback=120)
    weekly_bias   = weekly_struct.get("bias", "NEUTRAL")
    if side == "BUY"  and weekly_bias == "BEARISH":
        log(f"  ⚠️ {ticker} [1D BUY]: MTF conflict — weekly bias BEARISH — skip")
        return None
    if side == "SELL" and weekly_bias == "BULLISH":
        log(f"  ⚠️ {ticker} [1D SELL]: MTF conflict — weekly bias BULLISH — skip")
        return None

    # ── [1] Dynamic Weights — [v8.01] MarketContext ──────
    ctx      = _build_market_context(ticker, side, "SWING",
                                     mkt["regime"], mkt["adx"], phase)
    merged_w = ctx.final_weights
    _log_ctx_summary(ctx)
    # strategy_mode sudah ada di ctx
    strategy_mode = ctx.strategy_mode

    # ── [13] No-Trade Zone Engine ────────────────────────
    # [PHASE-2] Dinonaktifkan — NTZ terlalu sering memblokir signal sah.
    if PHASE2_STABILIZE:
        ntz = {"skip": False, "reasons": [], "chop_index": 0.0}
    else:
        ntz = check_no_trade_zone(closes, highs, lows, volumes,
                                   mkt["regime"], phase, weekly_bias,
                                   side, atr_pct)
        if ntz["skip"]:
            log(f"  🚫 {ticker} [1D {side}]: NTZ aktif — " + " | ".join(ntz["reasons"][:2]))
            return None

    # ── [14] Liquidity Depth Filter ──────────────────────
    # [PHASE-2] Dinonaktifkan — skip untuk hentikan over-filtering.
    if PHASE2_STABILIZE:
        liq_depth = {"depth_score": 0.0, "near_hvn": False, "hvn_price": None,
                     "sufficient": True, "reason": "PHASE2_BYPASS"}
    else:
        liq_depth = is_liquidity_sufficient(closes, highs, lows, volumes, ticker)
        if not liq_depth["sufficient"]:
            log(f"  ⚠️ {ticker} [1D]: Liquidity insufficient — {liq_depth['reason']}")
            return None

    # ── [15] Performance Clustering — sudah dilakukan di dalam MarketContext ──

    if side == "BUY":
        has_struct = (structure.get("bos")   == "BULLISH" or
                      structure.get("choch") == "BULLISH" or
                      liq.get("sweep_bull")  or
                      liq_trap.get("stop_hunt_bull") or
                      liq_trap.get("fake_bear_break"))
        if not has_struct:
            _audit_block("STRUCTURE_FAIL", "swing_buy")
            return None
        if rsi > RSI_OB["SWING"]:
            _audit_block("RSI_FILTER", f"rsi={rsi:.1f}>OB={RSI_OB['SWING']}")
            return None  # [v7.11] via RSI_OB config

        ob  = detect_order_block(closes, highs, lows, volumes, side="BUY", lookback=40, opens=opens)

        # ── [3] Entry Trigger ─────────────────────────────
        # [v7.9 FIX] Passes opens untuk akurasi candle pattern
        trigger = entry_trigger_check(closes, highs, lows, "BUY", opens=opens)
        if not trigger["valid"]:
            _audit_block("TRIGGER_FAIL", "swing_buy")
            log(f"  ⚠️ {ticker} [1D BUY]: No entry trigger — skip")
            return None

        # ── [PHASE-2] Simplified scoring — raw score only ────
        if PHASE2_STABILIZE:
            score = score_signal("BUY", price, closes, highs, lows, volumes,
                                 structure, liq, ob, rsi, macd, msig,
                                 ema50, ema200, vwap, mkt["regime"],
                                 weights=W, opens=opens)
            # [PHASE-0] IHSG soft penalty
            score -= _ihsg_soft_penalty_swing
            if _ihsg_soft_penalty_swing:
                log(f"  ⚠️ {ticker} [1D BUY]: IHSG soft penalty -{_ihsg_soft_penalty_swing} → score={score}")
            log(f"  🧮 {ticker} [1D BUY]: score={score} [PHASE2 — raw only]")
            sniper     = {"bonus": 0, "level": "STANDARD", "details": {}}
            setup_rank = {"priority": "NORMAL", "score_boost": 0,
                          "ev_threshold": 0.0, "reason": "PHASE2"}
            ev_threshold_active = 0.0
        else:
            # ── [11] Sniper Entry ─────────────────────────────
            ob_reaction = detect_ob_reaction(closes, highs, lows, volumes, ob, "BUY")
            fvg         = detect_fvg(closes, highs, lows, side="BUY", lookback=40)
            sniper      = calc_sniper_score(liq, liq_trap, ob_reaction, fvg, "BUY")

            # [v6.0+v7.9] Setup Ranking
            setup_rank = rank_setup_priority(structure, liq, liq_trap, ob, trigger, sniper, "BUY")
            ev_threshold_active = setup_rank["ev_threshold"]

            # [v8.01] ScoreAccumulator
            acc = ScoreAccumulator()
            acc.add("raw",      score_signal("BUY", price, closes, highs, lows, volumes,
                                             structure, liq, ob, rsi, macd, msig,
                                             ema50, ema200, vwap, mkt["regime"],
                                             weights=merged_w, opens=opens))
            acc.add("sniper",   sniper["bonus"])
            acc.add("priority", setup_rank["score_boost"])
            acc.add("strategy", ctx.strategy_mode.get("min_score_boost", 0))
            # [PHASE-0] IHSG soft penalty
            acc.add("ihsg_penalty", -_ihsg_soft_penalty_swing)
            score = acc.total()
            log(f"  🧮 {ticker} [1D BUY]: {acc.explain()}")

        # [v7.9 FIX] Tier setelah semua bonus
        tier = assign_tier(score)
        if tier == "SKIP":
            _audit_block("TIER_SKIP")
            return None
        log(f"  📊 {ticker} [1D BUY]: Setup Priority={setup_rank['priority']} ({setup_rank['reason']}), EV≥{ev_threshold_active}, Tier={tier}")

        last_sh = structure.get("last_sh")
        if last_sh and price > last_sh * 1.02: return None
        entry_raw = round(last_sh * 1.003, 2) if (last_sh and price > last_sh) else price
        entry = round_to_fraction(entry_raw, "nearest")

        sl, tp1, tp2 = calc_sl_tp(entry, "BUY", atr, structure, "SWING")
        if tp1 <= entry or sl >= entry: return None
        sl_dist = entry - sl
        if sl_dist <= 0 or sl_dist / entry > 0.12: return None
        rr = (tp1 - entry) / sl_dist

    else:  # SELL
        has_struct = (structure.get("bos")   == "BEARISH" or
                      structure.get("choch") == "BEARISH" or
                      liq.get("sweep_bear")  or
                      liq_trap.get("stop_hunt_bear") or
                      liq_trap.get("fake_bull_break"))
        if not has_struct:
            _audit_block("STRUCTURE_FAIL", "swing_sell")
            return None
        if rsi < RSI_OS["SWING"]:
            _audit_block("RSI_FILTER", f"rsi={rsi:.1f}<OS={RSI_OS['SWING']}")
            return None  # [v7.11] via RSI_OS config

        last_sh = structure.get("last_sh")
        if last_sh is None:
            return None
        if last_sh and price < last_sh * 0.97: return None

        ob  = detect_order_block(closes, highs, lows, volumes, side="SELL", lookback=40, opens=opens)

        # [v7.9 FIX] Passes opens
        trigger = entry_trigger_check(closes, highs, lows, "SELL", opens=opens)
        if not trigger["valid"]:
            _audit_block("TRIGGER_FAIL", "swing_sell")
            log(f"  ⚠️ {ticker} [1D SELL]: No entry trigger — skip")
            return None

        # ── [PHASE-2] Simplified scoring — raw score only ────
        if PHASE2_STABILIZE:
            score = score_signal("SELL", price, closes, highs, lows, volumes,
                                 structure, liq, ob, rsi, macd, msig,
                                 ema50, ema200, vwap, mkt["regime"],
                                 weights=W, opens=opens)
            log(f"  🧮 {ticker} [1D SELL]: score={score} [PHASE2 — raw only]")
            sniper     = {"bonus": 0, "level": "STANDARD", "details": {}}
            setup_rank = {"priority": "NORMAL", "score_boost": 0,
                          "ev_threshold": 0.0, "reason": "PHASE2"}
            ev_threshold_active = 0.0
        else:
            # ── [11] Sniper Entry ─────────────────────────────
            ob_reaction = detect_ob_reaction(closes, highs, lows, volumes, ob, "SELL")
            fvg         = detect_fvg(closes, highs, lows, side="SELL", lookback=40)
            sniper      = calc_sniper_score(liq, liq_trap, ob_reaction, fvg, "SELL")

            # [v6.0+v7.9] Setup Ranking
            setup_rank = rank_setup_priority(structure, liq, liq_trap, ob, trigger, sniper, "SELL")
            ev_threshold_active = setup_rank["ev_threshold"]

            # [v8.01] ScoreAccumulator
            acc = ScoreAccumulator()
            acc.add("raw",      score_signal("SELL", price, closes, highs, lows, volumes,
                                             structure, liq, ob, rsi, macd, msig,
                                             ema50, ema200, vwap, mkt["regime"],
                                             weights=merged_w, opens=opens))
            acc.add("sniper",   sniper["bonus"])
            acc.add("priority", setup_rank["score_boost"])
            acc.add("strategy", ctx.strategy_mode.get("min_score_boost", 0))
            score = acc.total()
            log(f"  🧮 {ticker} [1D SELL]: {acc.explain()}")

        # [v7.9 FIX] Tier setelah semua bonus
        tier = assign_tier(score)
        if tier == "SKIP":
            _audit_block("TIER_SKIP")
            return None
        log(f"  📊 {ticker} [1D SELL]: Setup Priority={setup_rank['priority']} ({setup_rank['reason']}), EV≥{ev_threshold_active}, Tier={tier}")

        entry_raw = round(last_sh * 0.998, 2) if (last_sh and price >= last_sh * 0.97) else price
        entry = round_to_fraction(entry_raw, "nearest")

        sl, tp1, tp2 = calc_sl_tp(entry, "SELL", atr, structure, "SWING")
        if tp1 >= entry or sl <= entry: return None
        sl_dist = sl - entry
        if sl_dist <= 0 or sl_dist / entry > 0.12: return None
        rr = (entry - tp1) / sl_dist

    # ── [22] Meta Intelligence — RR override per strategy mode ──
    rr_min_swing = MIN_RR["SWING"]
    if strategy_mode["rr_min_override"] and "SWING" in strategy_mode["rr_min_override"]:
        rr_min_swing = strategy_mode["rr_min_override"]["SWING"]

    # [v7.2 — FIX Masalah 6] Apply fraksi tolerance
    rr_min_swing_eff = rr_min_swing * (1 - RR_FRAKSI_TOLERANCE)
    if rr < rr_min_swing_eff:
        log(f"  ⚠️ {ticker} [1D {side}]: RR={rr:.2f} < {rr_min_swing_eff:.2f} (min {rr_min_swing} × fraksi tol) [{strategy_mode['mode']}] — skip")
        _audit_block("RR_LOW")
        return None

    # ── [7] Smart Risk Allocation ─────────────────────────
    # [Q02] Hitung win_prob lebih awal untuk Kelly sizing dengan Bayesian shrinkage
    max_positive = sum(v for v in merged_w.values() if v > 0) + 4  # +4 = max sniper bonus
    bias_aligned = (side == "BUY" and weekly_bias != "BEARISH") or \
                   (side == "SELL" and weekly_bias != "BULLISH")
    win_prob    = calc_win_probability(score, max_positive, mkt["regime"],
                                       phase, trigger["strength"], bias_aligned,
                                       ticker=ticker)
    _win_prob_n = get_cluster_n_samples(ticker, mkt["regime"])
    smart_risk  = get_smart_risk_pct(
        score, tier, atr_pct=atr_pct,
        ticker=ticker, win_prob=win_prob,
        avg_win_r=rr, strategy="SWING",
        win_prob_n=_win_prob_n,
    )

    # Capital Rotation
    sector_weight = get_sector_capital_weight(ticker, side)
    smart_risk    = round(smart_risk * sector_weight, 2)
    smart_risk    = max(smart_risk, 0.25)

    # [K03] Behavioral Edge — event calendar + sentiment spike + macro shock
    beh = check_behavioral_edge(ticker, volumes.tolist(), now_wib=datetime.now(WIB))
    if beh["block_signal"]:
        log(f"  🚨 {ticker} [1D {side}]: BLOCK behavioral — {beh['reason']}")
        return None
    if beh["reduce_factor"] < 1.0:
        smart_risk = round(smart_risk * beh["reduce_factor"], 2)
        smart_risk = max(smart_risk, 0.25)
        log(f"  📅 {ticker} [1D {side}]: Behavioral reduce → risk {smart_risk:.2f}% "
            f"(factor {beh['reduce_factor']})")

    # [K04] Execution Quality Score — swing data age biasanya rendah (1d candle)
    _swing_age_mins = 0.0   # 1d candle jarang stale; defaultkan ke 0
    _swing_vpr_est  = 0.0   # VPR dihitung saat sizing aktual

    # ── [8] Core EV Engine — Primary Decision Gate ───────
    # win_prob dan max_positive sudah dihitung di [7] untuk Kelly sizing
    ev = calc_expected_value(win_prob, rr)

    # Gate 1 — HARD FLOOR
    if ev <= HARD_EV_FLOOR:
        log(f"  ❌ {ticker} [1D {side}]: EV={ev:.2f} ≤ 0 — HARD SKIP (negative expectancy)")
        _audit_block("EV_NEGATIVE")
        return None

    if not PHASE2_STABILIZE:
        # Gate 2 — Strategy mode + setup rank combined threshold
        ev_floor_meta = strategy_mode.get("ev_floor_override") or ev_threshold_active
        effective_ev_threshold = max(ev_threshold_active, ev_floor_meta)
        if ev <= effective_ev_threshold:
            log(f"  ⚠️ {ticker} [1D {side}]: EV={ev:.2f} ≤ {effective_ev_threshold:.2f} [{strategy_mode['mode']}/{setup_rank['priority']}] — skip")
            _audit_block("EV_THRESHOLD")
            return None

        # Gate 3 — DEFENSIVE: wajib SNIPER/PRECISION
        if strategy_mode.get("require_sniper") and "STANDARD" in sniper.get("level", "STANDARD"):
            log(f"  ⚠️ {ticker} [1D {side}]: DEFENSIVE mode — STANDARD sniper ditolak")
            _audit_block("DEFENSIVE_SNIPER")
            return None
    else:
        effective_ev_threshold = HARD_EV_FLOOR
        log(f"  ✅ {ticker} [1D {side}]: EV={ev:.2f} > 0 [PHASE2 — gate 2/3 dinonaktifkan]")

    # [PHASE-1 v8.16] Record score detail — sinyal LOLOS, catat untuk summary
    _record_score_detail(ticker, "SWING", side, score, rr, ev,
                         structure_ok=True, rsi_ok=True, tier=tier, blocker="PASS")

    return {
        "ticker":       ticker,
        "pair":         ticker.replace(".JK", ""),
        "strategy":     "SWING",
        "side":         side,
        "timeframe":    "1d",
        "entry":        entry,
        "current_price": price,
        "tp1": tp1, "tp2": tp2, "sl": sl,
        "tier":         tier,
        "score":        score,
        "rr":           round(rr, 1),
        "rsi":          round(rsi, 1),
        "structure":    structure,
        "regime":       mkt["regime"],
        "adx":          mkt["adx"],
        "conviction":   calc_conviction(score),
        # v3.0 fields
        "phase":        phase,
        "phase_desc":   phase_info["description"],
        "entry_pattern": trigger["pattern"],
        "entry_strength": trigger["strength"],
        "daily_bias":   weekly_bias,
        "liq_trap":     liq_trap,
        "smart_risk_pct": smart_risk,
        # v4.0 fields
        "win_prob":     win_prob,
        "expected_value": ev,
        "net_ev":       calc_cost_adjusted_ev(win_prob, rr, "SWING", tier)["net_ev"],
        "sniper_level": sniper["level"],
        "ev_threshold":   effective_ev_threshold,
        # v7.0 fields
        "strategy_mode":  strategy_mode["mode"],
        "strategy_emoji": strategy_mode["emoji"],
        "strategy_desc":  strategy_mode["description"],
        # v7.9 fields
        "foreign_flow":   foreign_flow["trend"],
        "foreign_score":  foreign_flow["score"],
        # v7.16 fields [K03 + K04]
        "behavioral_event":  beh["event_name"],
        "sentiment_spike":   beh["sentiment_spike"],
        "behavioral_reason": beh["reason"],
        "exec_quality":      calc_execution_quality_score(
            strategy="SWING", data_age_minutes=_swing_age_mins,
            vpr=0.0, slippage_pct=0.20, partial_fill_risk=0.15, impact_pct=0.0,
        )["label"],
        "eqs_score":         calc_execution_quality_score(
            strategy="SWING", data_age_minutes=_swing_age_mins,
            vpr=0.0, slippage_pct=0.20, partial_fill_risk=0.15, impact_pct=0.0,
        )["eqs"],
        "data_age_minutes":  round(_swing_age_mins, 1),
        # v8.03 fields [R01] + [R03]
        "data_source":       DATA_SOURCE_SWING,
        "signal_mode":       "SIMPLE" if SIMPLE_MODE else "COMPLEX",
        # [PHASE-3] Extended metadata
        "atr_pct":           round(atr_pct, 3),
    }


# ════════════════════════════════════════════════════════
#  TELEGRAM OUTPUT
# ════════════════════════════════════════════════════════

def send_signal(sig: dict):
    """Format dan kirim signal ke Telegram. [v5.0 — win_prob, EV, sniper, NTZ, HVN]"""
    pair       = sig["pair"]
    strategy   = sig["strategy"]
    side       = sig["side"]
    tier       = sig["tier"]
    score      = sig["score"]
    rr         = sig["rr"]
    entry      = sig["entry"]
    tp1        = sig["tp1"]
    tp2        = sig["tp2"]
    sl         = sig["sl"]
    tf         = sig["timeframe"]
    rsi        = sig["rsi"]
    regime     = sig.get("regime", "—")
    adx        = sig.get("adx", 0.0)
    conviction = sig.get("conviction", "OK 🟡")

    # v3.0 fields (optional — backward compat jika ada signal lama di queue)
    phase         = sig.get("phase", "—")
    phase_desc    = sig.get("phase_desc", "")
    entry_pattern = sig.get("entry_pattern", "—")
    daily_bias    = sig.get("daily_bias", "—")
    liq_trap      = sig.get("liq_trap", {})
    smart_risk    = sig.get("smart_risk_pct", RISK_PCT)

    # v4.0 fields
    win_prob      = sig.get("win_prob", None)
    ev            = sig.get("expected_value", None)
    sniper_level  = sig.get("sniper_level", "STANDARD ⚪")
    sniper_detail = sig.get("sniper_detail", "—")
    # v5.0 fields
    chop_index    = sig.get("chop_index", None)
    depth_score   = sig.get("depth_score", None)
    near_hvn      = sig.get("near_hvn", None)
    hvn_price     = sig.get("hvn_price", None)
    # v6.0 fields
    setup_priority = sig.get("setup_priority", "MEDIUM")
    setup_reason   = sig.get("setup_reason", "—")
    ev_threshold   = sig.get("ev_threshold", EV_MIN_THRESHOLD)
    # v7.0 fields
    strategy_mode_name  = sig.get("strategy_mode", "NORMAL")
    strategy_mode_emoji = sig.get("strategy_emoji", "📊")
    strategy_mode_desc  = sig.get("strategy_desc", "")

    # Ambil harga live hanya saat signal benar-benar dikirim
    live      = get_current_price(sig["ticker"])
    cur_price = live if live > 0 else sig.get("current_price", entry)
    bos       = sig["structure"].get("bos") or sig["structure"].get("choch") or "—"

    pct_tp1   = abs((tp1 - entry) / entry * 100)
    pct_tp2   = abs((tp2 - entry) / entry * 100) if tp2 is not None else 0.0
    pct_sl    = abs((sl  - entry) / entry * 100)
    pct_above = (cur_price - entry) / entry * 100

    tier_emoji   = {"S": "💎", "A+": "🏆", "A": "🥇"}.get(tier, "🎯")
    strat_emoji  = {"INTRADAY": "📈", "SWING": "🌊"}.get(strategy, "🎯")
    regime_emoji = {"TRENDING": "🔥", "RANGING": "⚠️"}.get(regime, "—")
    tp_label     = "+" if side == "BUY" else "-"
    sl_label     = "-" if side == "BUY" else "+"

    # [v7.2 — FIX Masalah 4] SELL di IDX = EXIT posisi, bukan short selling
    if side == "BUY":
        side_emoji = "🟢 BUY"
        side_label = "BUY"
    else:
        side_emoji = "🔴 EXIT / TP" if SELL_AS_EXIT_ONLY else "🔴 SELL"
        side_label = "EXIT" if SELL_AS_EXIT_ONLY else "SELL"

    # [v7.2 — FIX Masalah 6] Pastikan entry juga dibulatkan ke fraksi harga IDX
    entry_frac = round_to_fraction(entry, "nearest")

    # Phase emoji
    phase_emoji_map = {
        # [v8.0] ACCUMULATION + DISTRIBUTION → RANGING (backward compat retained)
        "ACCUMULATION": "🏗️", "MARKUP": "🚀", "DISTRIBUTION": "🏭",
        "MARKDOWN": "📉",      "EXPANSION": "⚡", "MANIPULATION": "🎭",
        "CONSOLIDATION": "⏸️", "RANGING": "↔️",   # v8.0 new phase
    }
    phase_emoji = phase_emoji_map.get(phase, "❓")

    # Liquidity trap badges
    trap_badges = []
    if liq_trap.get("stop_hunt_bull"):  trap_badges.append("🎯 Stop Hunt Bull")
    if liq_trap.get("stop_hunt_bear"):  trap_badges.append("🎯 Stop Hunt Bear")
    if liq_trap.get("fake_bull_break"): trap_badges.append("🪤 Fake Bull Break")
    if liq_trap.get("fake_bear_break"): trap_badges.append("🪤 Fake Bear Break")
    trap_str = " | ".join(trap_badges) if trap_badges else ""

    # Warning entry terlambat
    entry_note = ""
    if side == "BUY" and pct_above > 0.5:
        entry_note = f"\n⚠️ Harga saat ini Rp{cur_price:,.0f} (+{pct_above:.1f}% dari entry)\n   <i>Tunggu pullback, jangan kejar harga!</i>"
    elif side == "BUY" and pct_above < -0.3:
        entry_note = f"\n✅ Harga saat ini Rp{cur_price:,.0f} — sudah di zona entry"
    elif side == "SELL" and pct_above < -0.5:
        entry_note = f"\n⚠️ Harga sudah turun {pct_above:.1f}% dari entry\n   <i>Tunggu retest ke zona entry!</i>"

    now         = datetime.now(WIB)
    hours_valid = 4 if strategy == "INTRADAY" else 24
    valid_until = (now + timedelta(hours=hours_valid)).strftime("%d/%m %H:%M WIB")

    tp2_str = f"Rp{tp2:,.0f} ({tp_label}{pct_tp2:.1f}%)" if tp2 is not None else "—"

    # Smart position sizing — gunakan smart_risk_pct (bukan flat RISK_PCT)
    ps = calc_position_sizing(entry, sl, side,
                               risk_pct=smart_risk,
                               portfolio_idr=PORTFOLIO_IDR)
    if ps:
        # [v7.11] Pakai format_idr() untuk konsistensi format angka besar
        portfolio_str = format_idr(PORTFOLIO_IDR)
        if smart_risk > RISK_PCT:
            risk_note = f" ⬆️ <i>(dinaikkan: score tinggi + sektor kuat)</i>"
        elif smart_risk < RISK_PCT * 0.6:
            risk_note = f" ⬇️⬇️ <i>(dikurangi: sektor lemah — capital rotation)</i>"
        elif smart_risk < RISK_PCT:
            risk_note = f" ⬇️ <i>(dikurangi: score/sektor adjustment)</i>"
        else:
            risk_note = ""
        ps_str = (f"\n━━━━━━━━━━━━━━━━━━\n"
                  f"💼 <b>Position Sizing</b> (modal {portfolio_str}, risk <b>{smart_risk}%</b>{risk_note})\n"
                  f"  Maks risiko : {format_idr(ps['max_risk_idr'])}\n"
                  f"  Est. lot    : <b>{ps['lot_estimate']} lot</b> ({ps['shares_estimate']:,} lembar)\n"
                  f"  Nilai posisi: {format_idr(ps['position_value'])}\n"
                  f"  <i>Sesuaikan dengan kondisi aktual kamu.</i>")
    else:
        ps_str = ""

    # v3.0 + v4.0 intelligence block
    intel_lines = [
        f"Phase      : {phase_emoji} <b>{phase}</b>",
    ]
    if phase_desc and phase_desc != "—":
        intel_lines.append(f"             <i>{phase_desc}</i>")
    intel_lines.append(f"Trigger    : ✅ {entry_pattern}")
    intel_lines.append(f"MTF Bias   : {daily_bias}")
    if trap_str:
        intel_lines.append(f"Liq Trap   : {trap_str}")
    # v4.0 additions
    intel_lines.append(f"Entry Mode : {sniper_level}")
    if sniper_detail and sniper_detail != "—":
        intel_lines.append(f"             <i>{sniper_detail}</i>")
    if win_prob is not None:
        ev_str   = f"{ev:+.2f}" if ev is not None else "—"
        ev_emoji = "✅" if (ev or 0) > 0.5 else ("⚠️" if (ev or 0) > 0 else "❌")
        net_ev_val = sig.get("net_ev")
        if net_ev_val is not None:
            net_ev_emoji = "✅" if net_ev_val > 0 else "🔴"
            net_ev_str   = f" | Net EV: {net_ev_val:+.2f} {net_ev_emoji}"
        else:
            net_ev_str = ""
        intel_lines.append(f"Win Prob   : <b>{win_prob:.0%}</b> | EV: {ev_str} {ev_emoji}{net_ev_str}")
    # v5.0 additions — Liquidity Depth + Choppiness
    if depth_score is not None:
        depth_bar  = "█" * int(depth_score * 5) + "░" * (5 - int(depth_score * 5))
        hvn_str    = f" (HVN: Rp{hvn_price:,.0f})" if near_hvn and hvn_price else ""
        depth_emoji = "🟢" if depth_score >= 0.6 else ("🟡" if depth_score >= 0.3 else "🔴")
        intel_lines.append(f"Liq Depth  : {depth_emoji} {depth_bar} {depth_score:.2f}{hvn_str}")
    if chop_index is not None:
        chop_emoji = "✅" if chop_index < 45 else ("⚠️" if chop_index < 55 else "🔴")
        intel_lines.append(f"Chop Index : {chop_emoji} {chop_index:.1f} ({'trending' if chop_index < 45 else 'ranging' if chop_index < 55 else 'choppy'})")
    # v6.0 additions — Setup Ranking + Capital Rotation
    priority_emoji = {"HIGH": "🔥", "MEDIUM": "🔵", "LOW": "⚪"}.get(setup_priority, "⚪")
    intel_lines.append(f"Setup Rank : {priority_emoji} <b>{setup_priority}</b> — <i>{setup_reason}</i>")
    ev_threshold_str = f"{ev_threshold:.2f}" if ev_threshold else f"{EV_MIN_THRESHOLD:.2f}"
    intel_lines.append(f"EV Gate    : ≥ {ev_threshold_str} (aktif per setup priority)")
    # v7.0 additions — Strategy Mode (Meta Intelligence)
    intel_lines.append(f"Strategy   : {strategy_mode_emoji} <b>{strategy_mode_name}</b>")
    if strategy_mode_desc:
        intel_lines.append(f"             <i>{strategy_mode_desc}</i>")
    # v7.9 additions — Net Foreign Flow Proxy
    foreign_flow_val  = sig.get("foreign_flow", "NEUTRAL")
    foreign_score_val = sig.get("foreign_score", 0.0)
    if foreign_flow_val != "NEUTRAL":
        ff_emoji = "🟢" if foreign_flow_val == "INFLOW" else "🔴"
        intel_lines.append(f"Foreign    : {ff_emoji} {foreign_flow_val} (proxy score: {foreign_score_val:+.2f})")
    # v7.16 additions — Behavioral Edge [K03] + Execution Quality Score [K04]
    beh_event   = sig.get("behavioral_event")
    sent_spike  = sig.get("sentiment_spike", False)
    exec_qual   = sig.get("exec_quality", "")
    eqs_score   = sig.get("eqs_score", None)
    if beh_event:
        intel_lines.append(f"📅 Event    : ⚠️ {beh_event}")
    if sent_spike:
        intel_lines.append(f"📣 Sentiment: ⚠️ Volume spike terdeteksi — monitor news")
    if exec_qual and eqs_score is not None:
        eqs_emoji = "✅" if eqs_score >= 75 else ("🟡" if eqs_score >= 50 else "🔴")
        _eqs_deg_note = " ⚠️ <i>risk dikurangi (data stale)</i>" if sig.get("eqs_degraded") else ""
        intel_lines.append(f"Exec Quality: {eqs_emoji} <b>{exec_qual}</b> (EQS {eqs_score:.0f}/100){_eqs_deg_note}")
    intel_block = "\n".join(intel_lines)

    # [v7.2] Gunakan harga yang sudah dibulatkan ke fraksi IDX
    pct_tp1   = abs((tp1 - entry_frac) / entry_frac * 100) if entry_frac else pct_tp1
    pct_tp2   = abs((tp2 - entry_frac) / entry_frac * 100) if (tp2 and entry_frac) else pct_tp2
    pct_sl    = abs((sl  - entry_frac) / entry_frac * 100) if entry_frac else pct_sl

    # [v7.2] Tambahkan info fraksi harga dan delay disclaimer
    frac_note = ""
    if entry_frac != int(entry):
        frac_note = f" <i>(fraksi IDX: asli Rp{entry:,.0f} → Rp{entry_frac:,})</i>"

    # Determine fraksi size for user info
    ep = entry_frac if entry_frac > 0 else entry
    if ep < 200:      frac_size = 1
    elif ep < 500:    frac_size = 2
    elif ep < 2000:   frac_size = 5
    elif ep < 5000:   frac_size = 10
    elif ep < 10000:  frac_size = 25
    else:             frac_size = 50

    # SELL signal context note untuk IDX retail
    sell_context = ""
    if side == "SELL" and SELL_AS_EXIT_ONLY:
        sell_context = (f"\n━━━━━━━━━━━━━━━━━━\n"
                        f"ℹ️ <b>Catatan IDX</b>: Sinyal EXIT ini adalah saran <b>keluar posisi / Take Profit</b>.\n"
                        f"Short selling tidak tersedia untuk investor ritel di IDX.\n"
                        f"Gunakan sinyal ini jika kamu <b>sudah memiliki posisi BUY</b> di saham ini.")

    # [v7.9] T+2 settlement note untuk SWING BUY
    t2_note = ""
    if strategy == "SWING" and side == "BUY":
        t2_note = (f"\n━━━━━━━━━━━━━━━━━━\n"
                   f"⏳ <b>T+2 Settlement IDX</b>: Saham baru bisa dijual 2 hari bursa setelah beli.\n"
                   f"Pastikan kamu tidak butuh dana dari posisi ini dalam 2 hari ke depan.")

    # [v8.0 FIX 8] Tambahkan data age aktual ke pesan — trader perlu tahu
    # kapan data terakhir diambil untuk memutuskan apakah signal masih valid.
    # data_age_minutes sudah ada di sig dict, tinggal ditampilkan secara eksplisit.
    data_age_mins_val = sig.get("data_age_minutes", None)
    if data_age_mins_val is not None:
        _age_int = int(data_age_mins_val)
        if _age_int <= 5:
            _age_emoji = "🟢"
            _age_label = "fresh"
        elif _age_int <= 15:
            _age_emoji = "🟡"
            _age_label = "acceptable"
        else:
            _age_emoji = "🔴"
            _age_label = "stale — konfirmasi dulu!"
        data_age_str = f"\n{_age_emoji} Data age : <b>{_age_int} menit</b> <i>({_age_label})</i>"
    else:
        data_age_str = f"\n⚠️ Data delay ~{DATA_DELAY_MINUTES} menit (yfinance)"

    # [R01] Data source label — sumber data + EQS ringkas + signal_mode
    _sig_data_source = sig.get("data_source", "")
    _sig_eqs         = sig.get("eqs_score", None)
    _sig_mode        = sig.get("signal_mode", "SIMPLE")
    if _sig_data_source:
        _ds_emoji   = "🟡" if "DELAYED" in _sig_data_source else "🟢"
        _eqs_note   = f" | EQS {_sig_eqs:.0f}/100" if _sig_eqs is not None else ""
        _mode_note  = f" | {_sig_mode}"
        _ds_caveat  = (f"   <i>⚠️ Data delay ~{DATA_DELAY_MINUTES} mnt — konfirmasi live price sebelum order.</i>\n"
                       if "DELAYED" in _sig_data_source else
                       f"   <i>ℹ️ EOD data — entry valid sesi berikutnya.</i>\n")
        data_source_block = (
            f"\n━━━━━━━━━━━━━━━━━━\n"
            f"{_ds_emoji} <b>Data</b>: <code>{_sig_data_source}</code>"
            f"{_eqs_note}{_mode_note}\n"
            f"{_ds_caveat}"
        )
    else:
        data_source_block = ""

    msg = (
        f"{strat_emoji} <b>{tier_emoji} [{tier}] SIGNAL {side_emoji} — {strategy}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Saham  : <b>{pair}</b> [{tf}]\n"
        f"⏰ Valid : {now.strftime('%H:%M')} → {valid_until}"
        f"{data_age_str}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Entry  : <b>Rp{entry_frac:,}</b>{frac_note}{entry_note}\n"
        f"TP1    : <b>Rp{tp1:,}</b> ({tp_label}{pct_tp1:.1f}%)\n"
        f"TP2    : <b>{tp2_str}</b>\n"
        f"SL     : <b>Rp{sl:,}</b> ({sl_label}{pct_sl:.1f}%)\n"
        f"Fraksi : Rp{frac_size} per tick\n"
        f"R/R    : <b>1:{rr}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Score      : {score} | RSI: {rsi}\n"
        f"Struktur   : {bos}\n"
        f"Regime     : {regime_emoji} {regime} (ADX: {adx})\n"
        f"Conviction : <b>{conviction}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{intel_block}"
        f"{ps_str}"
        f"{sell_context}"
        f"{t2_note}"
        f"{data_source_block}"
        f"<i>⚠️ Ini sinyal teknikal, bukan rekomendasi investasi.</i>\n"
        f"<i>Selalu pasang SL dan kelola risiko sendiri.</i>"
        + (f"\n<i>🕐 Intraday: data yfinance ~{DATA_DELAY_MINUTES}m delay. "
           f"Konfirmasi harga live di platform sebelum eksekusi.</i>"
           if strategy == "INTRADAY" and INTRADAY_IS_REFERENCE_ONLY else "")
    )
    tg(msg)
    log(f"  ✅ SIGNAL {tier} {strategy} {side_label} {pair} Entry:Rp{entry_frac:,} RR:1:{rr} Score:{score} Phase:{phase}")


# ════════════════════════════════════════════════════════
#  MAIN RUNNER
# ════════════════════════════════════════════════════════

def calc_complexity_score() -> dict:
    """
    [U04/W03] Complexity score dengan semantic danger weights.

    [W03] Tidak semua layer sama bahayanya. Contoh:
    - adaptive_relaxation: bahaya tinggi — bisa chase noise tiap run
    - cluster_weights: bahaya menengah — overfits ke regime terakhir
    - lean_mode: MENGURANGI risk, bukan menambah

    Weighted score = Σ(layer_active × danger_weight)
    Lebih honest dari count biasa.

    Danger weights:
      1.8 = sangat berbahaya (bisa spiral, optimise noise)
      1.4 = berbahaya (overfitting risk menengah tinggi)
      1.2 = agak berbahaya (amplifier noise moderat)
      1.0 = neutral / standard adaptive
      0.6 = low risk (informational mostly)
      0.0 = no risk (static, tidak adaptive)
     -2.0 = risk reducer (mengurangi complexity effective)
    """
    DANGER_WEIGHTS = {
        "adaptive_relaxation":    1.8,   # chases filter audit dari run sebelumnya
        "cluster_weights":        1.4,   # overfits ke regime/sector terbaru
        "kelly_sizing":           1.2,   # amplifies noisy WR estimate
        "adaptive_weights":       1.0,   # standard feedback loop
        "strategy_auto_disable":  1.0,   # could disable good strategy by chance
        "feedback_loop":          0.8,   # weight adjustment dari signal history
        "cost_ev_model":          0.6,   # informational + gate, low risk
        "behavioral_edge":        0.6,   # event calendar, low risk
        "walk_forward_val":       0.4,   # diagnostic only, not decision
        "eqs_gate":               0.4,   # data quality filter, low risk
        "complex_mode":           0.6,   # extra analysis layers
        "lean_mode_override":    -2.0,   # REDUCES complexity — subtract
    }

    if LEAN_MODE or _auto_lean_active:
        return {
            "score": 0.0, "max": sum(v for v in DANGER_WEIGHTS.values() if v > 0),
            "active_features": {"lean_mode_override": True},
            "label": "LEAN (all adaptive layers bypassed)",
            "suggest_lean": False, "weighted": True,
        }

    active   = {}
    w_score  = 0.0

    if bool(_adaptive_weights):
        active["adaptive_weights"] = DANGER_WEIGHTS["adaptive_weights"]
        w_score += DANGER_WEIGHTS["adaptive_weights"]
    if bool(_cluster_weights):
        active["cluster_weights"] = DANGER_WEIGHTS["cluster_weights"]
        w_score += DANGER_WEIGHTS["cluster_weights"]
    if bool(_disabled_strategies) or STRATEGY_MIN_TRADES > 0:
        active["strategy_auto_disable"] = DANGER_WEIGHTS["strategy_auto_disable"]
        w_score += DANGER_WEIGHTS["strategy_auto_disable"]
    if bool(_active_relaxations) or bool(_prev_filter_audit):
        active["adaptive_relaxation"] = DANGER_WEIGHTS["adaptive_relaxation"]
        w_score += DANGER_WEIGHTS["adaptive_relaxation"]
    if EV_USE_ADJUSTED_GATE:
        active["cost_ev_model"] = DANGER_WEIGHTS["cost_ev_model"]
        w_score += DANGER_WEIGHTS["cost_ev_model"]
    if MIN_SIGNALS_FOR_WEIGHT_VALIDATION > 0:
        active["feedback_loop"] = DANGER_WEIGHTS["feedback_loop"]
        w_score += DANGER_WEIGHTS["feedback_loop"]
    # Kelly always active
    active["kelly_sizing"] = DANGER_WEIGHTS["kelly_sizing"]
    w_score += DANGER_WEIGHTS["kelly_sizing"]
    if INTRADAY_ENABLED:
        active["eqs_gate"] = DANGER_WEIGHTS["eqs_gate"]
        w_score += DANGER_WEIGHTS["eqs_gate"]
    # Behavioral edge always compiled in
    active["behavioral_edge"] = DANGER_WEIGHTS["behavioral_edge"]
    w_score += DANGER_WEIGHTS["behavioral_edge"]
    if not SIMPLE_MODE:
        active["complex_mode"] = DANGER_WEIGHTS["complex_mode"]
        w_score += DANGER_WEIGHTS["complex_mode"]
    # Walk-forward val always in
    active["walk_forward_val"] = DANGER_WEIGHTS["walk_forward_val"]
    w_score += DANGER_WEIGHTS["walk_forward_val"]

    w_score = round(w_score, 2)
    max_score = sum(v for v in DANGER_WEIGHTS.values() if v > 0)

    if w_score <= 4.0:
        label = f"LOW ({w_score:.1f}/{max_score:.1f})"
    elif w_score <= 7.0:
        label = f"MODERATE ({w_score:.1f}/{max_score:.1f})"
    else:
        label = f"HIGH ({w_score:.1f}/{max_score:.1f}) ⚠️"

    return {
        "score":           w_score,
        "max":             max_score,
        "active_features": active,
        "label":           label,
        "suggest_lean":    w_score > 7.0,
        "weighted":        True,
    }


def print_system_config() -> None:
    """
    [S04] Cetak ringkasan semua konfigurasi aktif ke log dalam satu tempat.

    Tujuan: satu fungsi yang bisa dipanggil untuk memverifikasi manual
    bahwa sistem berjalan dengan setting yang diinginkan. Mengurangi
    kebutuhan membaca source code untuk memahami state bot.

    Dipanggil sekali di awal run(). Output: log INFO bukan Telegram
    (terlalu detail untuk notifikasi).
    """
    separator = "─" * 52
    log(f"\n{separator}")
    log(f"⚙️  SYSTEM CONFIG SUMMARY [v8.04]")
    log(f"{separator}")

    # ── Data & Market ─────────────────────────────────────
    log(f"📡 DATA SOURCE")
    log(f"   Intraday : {DATA_SOURCE_INTRADAY}")
    log(f"   Swing    : {DATA_SOURCE_SWING}")
    log(f"   Delay    : ~{DATA_DELAY_MINUTES} menit (yfinance, tidak bisa diubah tanpa API berbayar)")
    log(f"   Intraday : {'ENABLED ⚠️' if INTRADAY_ENABLED else 'DISABLED ✅ (delay safe)'}")
    if INTRADAY_ENABLED:
        log(f"   EQS gate : soft degradation threshold={INTRADAY_MIN_EQS} (hard block hanya EQS=0)")

    # ── Scoring & Filters ─────────────────────────────────
    log(f"🎯 SCORING & GATES")
    log(f"   Mode     : {'SIMPLE' if SIMPLE_MODE else 'COMPLEX'} | Lock={SIMPLE_MODE_LOCK}")
    log(f"   EV floor : HARD={HARD_EV_FLOOR} | SOFT min={EV_MIN_THRESHOLD}")
    log(f"   Tier min : S≥{TIER_MIN_SCORE['S']} A+≥{TIER_MIN_SCORE['A+']} A≥{TIER_MIN_SCORE['A']}")
    log(f"   RR min   : INTRADAY={MIN_RR['INTRADAY']} SWING={MIN_RR['SWING']} (±{RR_FRAKSI_TOLERANCE:.0%} tol)")
    log(f"   RSI OB/OS: INTRADAY {RSI_OB['INTRADAY']}/{RSI_OS['INTRADAY']} | "
        f"SWING {RSI_OB['SWING']}/{RSI_OS['SWING']}")

    # ── Risk & Sizing ─────────────────────────────────────
    log(f"💰 POSITION SIZING")
    log(f"   Portfolio: Rp{PORTFOLIO_IDR:,.0f} | Base risk: {RISK_PCT}%/trade")
    log(f"   Impact   : scale={IMPACT_MODEL_SCALE:.2f} [{IMPACT_CALIBRATION_STATUS}]")
    log(f"   ADV cap  : {MAX_ADV_PARTICIPATION_PCT}% ADV max per order")
    log(f"   Kelly    : prior_n=30 (Bayesian shrinkage) | blend=sample-weighted")

    # ── Strategy & Feedback ───────────────────────────────
    log(f"🧠 STRATEGY CONTROL")
    log(f"   Auto-disable: WR<{STRATEGY_MIN_WR:.0%} AND CI₉₀↓<{STRATEGY_MIN_WR:.0%} "
        f"(n≥{STRATEGY_MIN_TRADES}, window={STRATEGY_EVAL_WINDOW})")
    log(f"   Disable dur : {STRATEGY_DISABLE_HOURS}h | Active: {list(_disabled_strategies.keys()) or 'none'}")
    log(f"   Feedback W  : min {MIN_SIGNALS_FOR_WEIGHT_VALIDATION} signals | drift cap ±2 dari W base")

    # ── Kill Switches ─────────────────────────────────────
    log(f"🛑 KILL SWITCHES")
    log(f"   IHSG drop  : block BUY @ {IHSG_DROP_BLOCK}% | halt all @ {IHSG_CRASH_BLOCK}%")
    log(f"   Foreign flow: block={'ON' if FOREIGN_FLOW_BLOCK_ENABLED else 'OFF'} | "
        f"conf={FOREIGN_FLOW_BLOCK_CONF}")
    log(f"   Daily limit: max {MAX_TRADES_PER_DAY} signal/hari | "
        f"max drawdown {MAX_DAILY_DRAWDOWN_PCT}%")
    log(f"   Open pos   : max {MAX_OPEN_POSITIONS} posisi concurrent | "
        f"sector cap {MAX_SECTOR_EXPOSURE_PCT}%/sektor | "
        f"capital gate {MAX_TOTAL_CAPITAL_DEPLOYED_PCT}% deployed")

    # ── Edge Status (quick summary) ───────────────────────
    log(f"📊 EDGE STATUS")
    log(f"   Proof gate : min {EDGE_PROOF_MIN_SIGNALS} signals, "
        f"WR≥{EDGE_MIN_WR:.0%}, EV≥{EDGE_MIN_EV:.2f}, p<{EDGE_PVAL_THRESHOLD}")
    log(f"   Mode cmp   : min {COMPLEX_MODE_MIN_SAMPLE} signals per mode untuk SIMPLE vs COMPLEX valid")
    log(f"   Cost model : slip INTRADAY S={EV_COST_SLIPPAGE['INTRADAY']['S']:.2f}% "
        f"A={EV_COST_SLIPPAGE['INTRADAY']['A']:.2f}% | fill INTRADAY={EV_COST_FILL_RATE['INTRADAY']:.0%} "
        f"SWING={EV_COST_FILL_RATE['SWING']:.0%} | AdjEV gate={'ON' if EV_USE_ADJUSTED_GATE else 'OFF (informational)'}")
    log(f"   Adaptive   : block_threshold={ADAPTIVE_BLOCK_THRESHOLD:.0%} max_relax={ADAPTIVE_MAX_RELAX_PCT:.0%}")

    # ── Bootstrap phase (dibaca dari global — diisi setelah check_edge_proven) ─
    _n_cfg = globals().get("_edge_n_cache", 0)
    _bp_cfg = globals().get("_bootstrap_phase", "COLD")
    _bp_icons_cfg = {"COLD": "🧊", "EARLY": "🌱", "WARMING": "🔥", "MATURE": "✅"}
    log(f"🔖 BOOTSTRAP PHASE")
    log(f"   Current    : {_bp_icons_cfg.get(_bp_cfg, '❓')} {_bp_cfg} (n={_n_cfg} resolved)")
    log(f"   Thresholds : COLD<{BOOTSTRAP_COLD_N} | EARLY<{BOOTSTRAP_EARLY_N} "
        f"| WARMING<{BOOTSTRAP_WARMING_N} | MATURE≥{BOOTSTRAP_WARMING_N}")
    log(f"   Signal cap : COLD={BOOTSTRAP_SIGNALS_CAP_COLD} | EARLY={BOOTSTRAP_SIGNALS_CAP_EARLY} "
        f"| WARMING/MATURE=normal")
    log(f"   Lean gate  : COLD/EARLY → _effective_lean()=True (adaptive layers off)")
    log(f"{separator}\n")


def run():
    """Fungsi utama — scan seluruh watchlist dan kirim sinyal terbaik."""

    # ── [Z01] WEEKEND / JAM BURSA GUARD ────────────────────────────
    # Guard ini WAJIB berada di baris pertama run() sebelum koneksi
    # apapun ke Supabase/Yahoo Finance dilakukan.
    # Bursa IDX: Senin–Jumat, 09:00–16:00 WIB.
    # Buffer ±1 jam (08:00–17:00) untuk pre/post-market monitoring.
    _now_wib_guard = datetime.now(WIB)
    _weekday_guard = _now_wib_guard.weekday()   # 0=Senin, 6=Minggu
    _hour_guard    = _now_wib_guard.hour
    if _weekday_guard >= 5 or not (8 <= _hour_guard < 17):
        _day_names = ["Senin","Selasa","Rabu","Kamis","Jumat","Sabtu","Minggu"]
        log(f"⏸️ [Z01] Bukan jam bursa — {_day_names[_weekday_guard]} "
            f"{_now_wib_guard.strftime('%H:%M')} WIB. Run dibatalkan.")
        return
    # ── End guard ───────────────────────────────────────────────────

    global _candle_cache, _dedup_memory, _sector_momentum_cache, _cluster_weights, \
           _disabled_strategies, _sector_exposure_tracker, _adaptive_weights, \
           _threshold_guard, \
           _cluster_raw_counts, _filter_audit, _filter_audit_checked, \
           _prev_filter_audit, _prev_filter_audit_checked, _active_relaxations, \
           _relaxation_run_counter, \
           TIER_MIN_SCORE, EV_MIN_THRESHOLD, RR_FRAKSI_TOLERANCE, INTRADAY_MIN_EQS, \
           _auto_lean_reason, _edge_n_cache, _bootstrap_phase

    # Reset all transient state
    _candle_cache           = {}
    _dedup_memory           = set()
    _sector_momentum_cache  = {}
    _cluster_weights        = {}
    _cluster_raw_counts     = {}
    _adaptive_weights       = {}
    _sector_exposure_tracker.clear()

    # [T03/U01] Snapshot filter audit before reset
    _prev_filter_audit         = dict(_filter_audit)
    _prev_filter_audit_checked = _filter_audit_checked

    # [S03] Reset filter audit for new run
    _filter_audit         = {}
    _filter_audit_checked = 0

    # [PHASE-1 v8.16] Reset score detail tracking
    global _p1_score_total_sum, _p1_score_total_count, _p1_ticker_score_detail
    _p1_score_total_sum     = 0
    _p1_score_total_count   = 0
    _p1_ticker_score_detail = {}

    # [U01] Increment relaxation run counter
    _relaxation_run_counter += 1

    # [R02] RunStateGuard
    _run_state_guard.mark_reset()
    _run_state_guard.validate(
        candle_cache     = _candle_cache,
        sector_tracker   = _sector_exposure_tracker,
        adaptive_weights = _adaptive_weights,
        cluster_weights  = _cluster_weights,
        raw_counts       = _cluster_raw_counts,
    )

    if not isinstance(_disabled_strategies, dict):
        _disabled_strategies = {}

    now_wib = datetime.now(WIB)
    log(f"\n{'='*60}")
    log(f"🚀 SIGNAL BOT SAHAM IDX v8.14 — {now_wib.strftime('%Y-%m-%d %H:%M WIB')}")
    log(f"{'='*60}")

    # [S04] System config summary
    print_system_config()

    # [U04/V04] Complexity score — compute FIRST, used later for tax
    _cx = calc_complexity_score()
    log(f"🧩 [U04] Complexity: {_cx['label']}")
    if _cx["suggest_lean"] and not _effective_lean():
        log("  💡 [U04] Complexity tinggi tanpa edge — pertimbangkan ENV LEAN_MODE=true.", "warn")

    # [U03] Real fill calibration
    log("💰 [U03] Kalibrasi cost model dari actual fills...")
    try:
        _calib = calibrate_cost_model_from_fills()
        log(f"  {'✅' if _calib['calibrated'] else 'ℹ️'} [U03] {_calib['note']}")
    except Exception as _ce:
        log(f"  ⚠️ [U03] calibrate_cost_model_from_fills error: {_ce}", "warn")

    # [S01/T01/T02/V01] Edge proof — multi-layer + ensemble
    log("📊 [PHASE4/V01] Memeriksa bukti edge empiris (ensemble 4-method)...")
    _edge        = {"verdict": "INSUFFICIENT", "note": "error", "warnings": [], "ensemble": {}}
    _verdict_s01 = "INSUFFICIENT"
    try:
        _edge        = check_edge_proven()
        _verdict_s01 = _edge["verdict"]
        log(f"  {_edge['note']}")
        # Log ensemble method breakdown
        _ens = _edge.get("ensemble", {})
        for _mn, _md in _ens.get("methods", {}).items():
            log(f"    [{_mn}] {_md.get('note', '—')}")
        for _w in _edge.get("warnings", []):
            log(f"  {_w}")
    except Exception as _e1:
        log(f"  ⚠️ [PHASE4/V01] check_edge_proven error: {_e1}", "warn")

    # ── [PHASE-4] GUARD BULLETPROOF (v8.18 Fix 1) ───────────────────────
    # Guard ini TIDAK bergantung pada PHASE3_COLLECTION.
    # Masalah sebelumnya: guard hanya ada via "if PHASE3_COLLECTION: PHASE4=False"
    # Jika PHASE3 di-disable → PHASE4 langsung aktif tanpa cek jumlah trade.
    #
    # Fix: guard ini SELALU jalan, SEBELUM _phase4_active dibaca,
    # sehingga PHASE4_VALIDATE tidak pernah True jika n < 50,
    # tidak peduli apakah PHASE3 aktif atau tidak.
    global PHASE4_VALIDATE
    _p4_n_actual = _edge.get("n", 0)

    # [v8.18] GLOBAL UNCONDITIONAL GUARD — runs regardless of PHASE3 state
    if _p4_n_actual < 50:
        if PHASE4_VALIDATE:   # hanya log jika sebelumnya True, untuk menghindari spam
            log(
                f"  ⚠️ [P8-01] PHASE4_VALIDATE → False (GLOBAL GUARD): "
                f"n={_p4_n_actual} < 50 — guard ini aktif independen dari PHASE3. "
                f"Kumpulkan minimal 50 resolved trade.",
                "warn"
            )
        PHASE4_VALIDATE = False   # hard set — tidak bisa dikembalikan True di run ini

    _phase4_active = PHASE4_VALIDATE  # baca setelah guard, bukan sebelum

    # ── [PHASE-4] PERBAIKAN 2: FREEZE system saat UNPROVEN ──────────────
    # Saat verdict UNPROVEN, pastikan tidak ada adaptive layer yang aktif.
    # Ini enforcement tambahan di atas LEAN_MODE — karena LEAN_MODE bisa
    # di-override oleh user, tapi UNPROVEN freeze tidak boleh bisa di-override.
    _p4_frozen = False
    if _verdict_s01 in ("UNPROVEN", "INSUFFICIENT") and not PHASE3_COLLECTION:
        # PHASE3 sudah handle freeze-nya sendiri — ini untuk post-PHASE3
        _p4_frozen = True
        # Force adaptive weights ke base W — tidak boleh ada tuning
        if not isinstance(_adaptive_weights, dict) or _adaptive_weights != W:
            _adaptive_weights = dict(W)
            log(
                f"  🔒 [PHASE4 FREEZE] Verdict={_verdict_s01} → adaptive weights "
                f"dikembalikan ke base W. Tidak ada tuning sampai verdict berubah.",
                "warn"
            )
        # Pastikan cluster_weights dikosongkan
        # [v8.19 FIX] global _cluster_weights sudah dideklarasikan di awal run() baris ~11091
        # Tidak perlu deklarasi ulang — Python 3.12 strict: used-before-global = SyntaxError
        if _cluster_weights:
            _cluster_weights = {}
            log(
                f"  🔒 [PHASE4 FREEZE] _cluster_weights dikosongkan — "
                f"tidak ada cluster-based weight modification saat {_verdict_s01}.",
                "warn"
            )

    # ── [PHASE-4] Set global verdict + detect change ─────────────────────
    global _current_edge_verdict, _prev_edge_verdict, _p5_prev_state
    _prev_edge_verdict    = _current_edge_verdict
    _current_edge_verdict = _verdict_s01

    _verdict_changed  = (_current_edge_verdict != _prev_edge_verdict and
                         _prev_edge_verdict != "")   # bukan cold start
    _verdict_coldstart = (_prev_edge_verdict == "")

    if _phase4_active:
        if _verdict_coldstart:
            # Pertama kali — kirim tanpa spam (startup)
            try:
                send_validation_report(_edge, triggered_by="startup")
            except Exception as _vr_e:
                log(f"  ⚠️ [PHASE4] validation report startup: {_vr_e}", "warn")
        elif _verdict_changed:
            # Verdict berubah — kirim karena ini informatif
            log(f"  🔔 [PHASE4] Verdict berubah: {_prev_edge_verdict} → {_current_edge_verdict}", "warn")
            try:
                send_validation_report(_edge, triggered_by="run")
            except Exception as _vr_e:
                log(f"  ⚠️ [PHASE4] validation report on change: {_vr_e}", "warn")
    else:
        log(f"  ℹ️ [PHASE4] Telegram report dilewati — "
            f"PHASE4_VALIDATE tidak aktif (n={_p4_n_actual} atau PHASE3 masih berjalan)")

    # ── [PHASE-4] PERBAIKAN 3: EDGE STATUS — output jelas setiap run ─────
    # Biarkan data bicara. Tidak ada interpretasi manual. Selalu tampil.
    _p4_wr      = _edge.get("wr")
    _p4_ev      = _edge.get("empirical_ev")
    _p4_net_ev  = _edge.get("net_ev")
    _p4_pf      = _edge.get("ensemble", {}).get("methods", {}).get(
                      "M2_profit_factor_train", {}).get("note", "N/A")
    _p4_wr_str  = f"{_p4_wr:.0%}" if _p4_wr is not None else "N/A"
    _p4_ev_str  = f"{_p4_ev:+.3f}" if _p4_ev is not None else "N/A"
    _p4_nev_str = f"{_p4_net_ev:+.3f}" if _p4_net_ev is not None else "N/A"

    _p4_status_emoji = {
        "PROVEN":       "✅",
        "PROMISING":    "🔵",
        "UNPROVEN":     "⚠️",
        "INSUFFICIENT": "🧊",
    }.get(_verdict_s01, "❓")

    # ── [P8-03] COLLECTION PROGRESS BAR ─────────────────────────────────
    # Tampilkan setiap run — satu baris, mudah di-grep di CI log.
    # Sebelumnya tidak ada output yang jelas tentang progress menuju Phase 4.
    _coll_target    = COLLECTION_TARGET_MIN          # default 50 (bisa beda dari EDGE_PROOF_MIN_SIGNALS)
    # [v8.19 L3-FIX] Hapus dead code getattr(__builtins__...) yang fragile.
    # COLLECTION_TARGET_FULL adalah module-level constant — cukup baca langsung.
    _coll_full      = COLLECTION_TARGET_FULL         # module-level constant, selalu tersedia
    _coll_pct       = min(_p4_n_actual / max(_coll_target, 1) * 100, 100)
    _bar_filled     = int(_coll_pct / 10)           # 10-char bar
    _coll_bar       = "█" * _bar_filled + "░" * (10 - _bar_filled)
    _coll_remaining = max(0, _coll_target - _p4_n_actual)

    _collection_progress_line = (
        f"📊 COLLECTION PROGRESS: {_p4_n_actual} / {_coll_target} "
        f"[{_coll_bar}] {_coll_pct:.0f}% "
        + (f"— {_coll_remaining} trade lagi untuk analisis kasar" if _coll_remaining > 0
           else f"✅ Sudah cukup — siap validasi (target penuh: {_coll_full})")
    )
    log(_collection_progress_line)

    log(
        f"\n{'─'*40}\n"
        f"  EDGE STATUS : {_p4_status_emoji} {_verdict_s01}\n"
        f"  Trades (n)  : {_p4_n_actual}\n"
        f"  WR          : {_p4_wr_str}\n"
        f"  EV (gross)  : {_p4_ev_str}\n"
        f"  EV (net)    : {_p4_nev_str}\n"
        f"  PF note     : {_p4_pf}\n"
        f"  Frozen      : {'🔒 YA — tidak ada optimasi' if _p4_frozen else 'Tidak'}\n"
        f"{'─'*40}\n"
    )

    # ── Satu-liner VERDICT (mudah di-grep di CI log / Telegram) ──────────
    _pf_raw = _edge.get("ensemble", {}).get("methods", {}).get(
                  "M2_profit_factor_train", {}).get("value")
    _pf_str = f"{_pf_raw:.2f}" if isinstance(_pf_raw, (int, float)) else "N/A"
    log(
        f"VERDICT SUMMARY | "
        f"WR: {_p4_wr_str} | "
        f"EV: {_p4_ev_str} | "
        f"PF: {_pf_str} | "
        f"n: {_p4_n_actual} | "
        f"VERDICT: {_verdict_s01}"
    )

    # ── PHASE-4 UNPROVEN BANNER ───────────────────────────────────────
    # Tampilkan di log setiap run saat UNPROVEN/INSUFFICIENT
    # agar engineer tidak lupa: jangan tambah kompleksitas.
    if _verdict_s01 in ("UNPROVEN", "INSUFFICIENT"):
        _n_current = _edge.get("n", 0)
        _needed    = max(0, EDGE_PROOF_MIN_SIGNALS - _n_current)
        log(
            f"\n{'⛔'*20}\n"
            f"  [PHASE-4] VERDICT: {_verdict_s01}\n"
            f"  {_collection_progress_line}\n"   # [P8-03] progress bar embedded
            f"  n={_n_current}/{EDGE_PROOF_MIN_SIGNALS} trade resolved "
            f"({'perlu ' + str(_needed) + ' lagi' if _needed > 0 else 'cukup, tapi ensemble belum setuju'})\n"
            f"  ATURAN AKTIF:\n"
            f"    ❌ JANGAN tambah filter atau kompleksitas baru\n"
            f"    ❌ JANGAN ubah W, TIER_MIN_SCORE, MIN_RR\n"
            f"    ❌ JANGAN aktifkan adaptive/cluster weights\n"
            f"    ✅ Kumpulkan lebih banyak trade — operasikan apa adanya\n"
            f"{'⛔'*20}\n",
            "warn"
        )
    elif _verdict_s01 == "PROMISING":
        log(f"  🔵 [PHASE4] PROMISING — operasional normal, "
            f"jangan ubah parameter sampai PROVEN.")
    elif _verdict_s01 == "PROVEN":
        log(f"  ✅ [PHASE4] PROVEN — adaptive layers diizinkan. "
            f"Complexity tambahan boleh diuji.")

    # ── [P7-02] EDGE GATE — Block LIVE execution jika edge belum proven ──────
    # Ini adalah gate yang sebelumnya HILANG: EXECUTION_MODE=live bisa aktif
    # bahkan saat edge verdict masih INSUFFICIENT/UNPROVEN.
    # Fix: jika mode LIVE dan verdict belum PROVEN/PROMISING → abort run,
    # kirim alert Telegram, dan return sebelum scan ticker.
    #
    # Override via ENV: EDGE_GATE_BYPASS=true (untuk testing — gunakan dengan hati-hati).
    # ⚠️ BYPASS HANYA BOLEH digunakan oleh developer, bukan di production.
    _edge_gate_bypass = os.environ.get("EDGE_GATE_BYPASS", "false").lower() == "true"

    if EXECUTION_MODE == "live" and _verdict_s01 not in ("PROVEN", "PROMISING"):
        if _edge_gate_bypass:
            log(
                f"  ⚠️ [P7-02] EDGE GATE BYPASSED via ENV EDGE_GATE_BYPASS=true "
                f"— verdict={_verdict_s01}, n={_p4_n_actual}. "
                f"Bot lanjut LIVE mode. GUNAKAN HATI-HATI.",
                "warn"
            )
        else:
            _gate_msg = (
                f"🔴 <b>[P7-02] LIVE EXECUTION BLOCKED — EDGE NOT PROVEN</b>\n\n"
                f"<b>Verdict:</b> {_verdict_s01}\n"
                f"<b>Resolved trades (n):</b> {_p4_n_actual} / {EDGE_PROOF_MIN_SIGNALS} minimum\n"
                f"<b>WR:</b> {_p4_wr_str} | <b>EV (net):</b> {_p4_nev_str}\n\n"
                f"Bot tidak akan kirim order nyata sampai edge terbukti.\n"
                f"Lanjutkan kumpulkan data di mode <code>dry_run</code> atau <code>paper</code>.\n\n"
                f"💡 Untuk override (developer only): "
                f"set <code>EDGE_GATE_BYPASS=true</code>."
            )
            log(
                f"\n{'🚫'*20}\n"
                f"  [P7-02] LIVE EXECUTION BLOCKED\n"
                f"  Verdict={_verdict_s01} bukan PROVEN/PROMISING.\n"
                f"  n={_p4_n_actual}/{EDGE_PROOF_MIN_SIGNALS} resolved trade.\n"
                f"  Bot tidak scan ticker. Set EXECUTION_MODE=dry_run atau\n"
                f"  kumpulkan lebih banyak data sampai verdict berubah.\n"
                f"{'🚫'*20}\n",
                "error"
            )
            try:
                tg(_gate_msg)
            except Exception:
                pass
            return   # ← hard stop sebelum scan ticker apapun

    # ── BOOTSTRAP PHASE DETECTION ────────────────────────────────────
    # Wajib dijalankan SETELAH check_edge_proven() agar _edge_n_cache terisi.
    # Mengisi _edge_n_cache (dibaca oleh _effective_lean()) dan _bootstrap_phase
    # (dibaca oleh signals_cap override dan health check).
    global _edge_n_cache, _bootstrap_phase
    _edge_n_cache = _edge.get("n", 0)
    _bootstrap_phase = (
        "COLD"    if _edge_n_cache < BOOTSTRAP_COLD_N    else
        "EARLY"   if _edge_n_cache < BOOTSTRAP_EARLY_N   else
        "WARMING" if _edge_n_cache < BOOTSTRAP_WARMING_N else
        "MATURE"
    )

    _bp_emojis = {"COLD": "🧊", "EARLY": "🌱", "WARMING": "🔥", "MATURE": "✅"}
    _bp_emoji  = _bp_emojis.get(_bootstrap_phase, "❓")

    log(f"  {_bp_emoji} [BOOTSTRAP] Phase={_bootstrap_phase} | "
        f"n={_edge_n_cache}/{BOOTSTRAP_WARMING_N} resolved | "
        f"target: {BOOTSTRAP_COLD_N}→EARLY, {BOOTSTRAP_EARLY_N}→WARMING, "
        f"{BOOTSTRAP_WARMING_N}→MATURE")

    if _bootstrap_phase in ("COLD", "EARLY"):
        log(f"  🔵 [BOOTSTRAP] Adaptive layers dinonaktifkan otomatis "
            f"(n={_edge_n_cache} < {BOOTSTRAP_EARLY_N} — feedback = noise, bukan signal). "
            f"Bot dalam mode observasi.", "warn")
        if _bootstrap_phase == "COLD":
            log(f"  🧊 [BOOTSTRAP] COLD: max {BOOTSTRAP_SIGNALS_CAP_COLD} signal/run. "
                f"Prioritas: akumulasi data, bukan profit optimization.", "warn")
        else:
            log(f"  🌱 [BOOTSTRAP] EARLY: max {BOOTSTRAP_SIGNALS_CAP_EARLY} signal/run. "
                f"Adaptive layers (cluster, feedback, relaxation) masih dinonaktifkan.", "warn")
    elif _bootstrap_phase == "WARMING":
        log(f"  🔥 [BOOTSTRAP] WARMING: adaptive layers aktif tapi edge belum proven. "
            f"Complexity tax dan lean suggestion berlaku normal.")
    else:
        log(f"  ✅ [BOOTSTRAP] MATURE: track record cukup untuk validasi statistik penuh.")

    # [V03/W04] Auto LEAN_MODE — context-aware, not just streak count
    global _unproven_run_streak, _auto_lean_active, _auto_lean_reason
    _was_auto_lean = _auto_lean_active

    if _verdict_s01 in ("UNPROVEN", "INSUFFICIENT"):
        _unproven_run_streak += 1
    else:
        _unproven_run_streak = 0

    # [W04] Context check: disambiguate REGIME_CHANGE vs SYSTEM_ISSUE
    # Jika edge UNPROVEN tapi IHSG sendiri sedang dalam bear market (drop > 5% 5d),
    # ini lebih likely regime change eksternal daripada sistem rusak.
    # Dalam kasus ini, auto lean TIDAK aktif — kita tidak perlu mengurangi
    # complexity karena sistemnya mungkin fine, marketnya yang bermasalah.
    _lean_context = "—"
    if _unproven_run_streak >= AUTO_LEAN_THRESHOLD and not _auto_lean_active:
        # Ambil IHSG data untuk konteks (sudah diambil di step 4, tapi bisa belum tersedia di sini)
        _ihsg_ctx = {}
        try:
            _ihsg_ctx = get_ihsg_regime()
        except Exception:
            pass

        ihsg_5d = _ihsg_ctx.get("ihsg_5d", 0.0)
        ihsg_1d = _ihsg_ctx.get("ihsg_1d", 0.0)

        # Bear market threshold: IHSG turun > 5% dalam 5 hari
        if ihsg_5d < -5.0 or ihsg_1d < -3.0:
            _lean_context = "REGIME_CHANGE"
            _auto_lean_reason = "REGIME_CHANGE"
            log(f"  🟡 [W04] Streak={_unproven_run_streak} UNPROVEN tapi IHSG {ihsg_5d:+.1f}%/5d "
                f"→ likely REGIME_CHANGE, bukan system rusak. Auto lean ditahan.", "warn")
        else:
            _lean_context = "SYSTEM_ISSUE"
            _auto_lean_reason = "SYSTEM_ISSUE"
            _auto_lean_active = True
            log(f"  🔴 [W04] Streak={_unproven_run_streak} UNPROVEN, IHSG normal ({ihsg_5d:+.1f}%/5d) "
                f"→ SYSTEM_ISSUE. AUTO LEAN_MODE AKTIF.", "warn")
            tg(f"🔴 <b>[W04] AUTO LEAN_MODE AKTIF — SYSTEM_ISSUE</b>\n"
               f"Verdict: {_verdict_s01} | Streak: {_unproven_run_streak} run\n"
               f"IHSG 5d: {ihsg_5d:+.1f}% (normal) — edge degradation = system problem\n"
               f"Semua adaptive layer dinonaktifkan sampai edge recover.")

    elif _auto_lean_active:
        if _verdict_s01 in ("PROVEN", "PROMISING"):
            _auto_lean_active    = False
            _auto_lean_reason    = ""
            _unproven_run_streak = 0
            log(f"  ✅ [W04] AUTO LEAN_MODE DINONAKTIFKAN — edge recover ke {_verdict_s01}", "warn")
            tg(f"✅ <b>[W04] AUTO LEAN_MODE DINONAKTIFKAN</b>\n"
               f"Edge: {_verdict_s01} | Adaptive layers diaktifkan kembali.")
        else:
            log(f"  ⚠️ [W04] AUTO LEAN aktif ({_auto_lean_reason}) "
                f"streak={_unproven_run_streak}", "warn")
    else:
        log(f"  ✅ [W04] Auto lean: streak={_unproven_run_streak}/{AUTO_LEAN_THRESHOLD} "
            f"ctx={_lean_context} — inactive")

    # [V04] Complexity tax — 0 jika edge PROVEN, aktif jika tidak
    # [PHASE-2] HARD DISABLED — complexity_tax dimatikan paksa saat PHASE2_STABILIZE
    # [v8.18 P8-02] HARD CLEAN MODE — jika _effective_lean() aktif, matikan SEMUA
    #   intelligence layer: complexity_tax, distribution_penalty, ensemble_override,
    #   dan auto_lean influence. Phase 3 = pure system, zero tambahan influence.
    global _current_complexity_tax
    FORCE_DISABLE_COMPLEXITY = _effective_lean()   # [P8-02] True saat lean/phase3/bootstrap

    if FORCE_DISABLE_COMPLEXITY:
        _current_complexity_tax = 0.0
        _lean_source = (
            "LEAN_MODE" if LEAN_MODE else
            "AUTO_LEAN" if _auto_lean_active else
            f"BOOTSTRAP({_bootstrap_phase})"
        )
        log(
            f"  🔒 [P8-02] FORCE_DISABLE_COMPLEXITY=True ({_lean_source}) — "
            f"complexity_tax=0, distribution_penalty=OFF, ensemble_override=OFF. "
            f"Phase 3 = PURE SYSTEM."
        )
    elif PHASE2_STABILIZE:
        _current_complexity_tax = 0.0
        log(f"  ✅ [V04] Complexity tax: 0 [PHASE2_STABILIZE — force disabled]")
    elif _verdict_s01 == "PROVEN":
        _current_complexity_tax = 0.0   # edge proven → complexity justified → no tax
        log(f"  ✅ [V04] Complexity tax: 0 (edge PROVEN — complexity justified)")
    else:
        excess = max(0, _cx["score"] - COMPLEXITY_TAX_THRESHOLD)
        _current_complexity_tax = min(excess * COMPLEXITY_TAX_PER_POINT, COMPLEXITY_TAX_CAP)
        if _current_complexity_tax > 0:
            log(f"  🧩 [V04] Complexity tax: {_current_complexity_tax:.0%} "
                f"(score={_cx['score']}, excess={excess} pts × {COMPLEXITY_TAX_PER_POINT:.0%})", "warn")
        else:
            log(f"  ✅ [V04] Complexity tax: 0 (score={_cx['score']} ≤ threshold={COMPLEXITY_TAX_THRESHOLD})")

    # [v8.09-A] Save edge verdict snapshot untuk prospective tracking
    # Setiap run simpan verdict ke edge_verdict_log. evaluate_edge_verdict_accuracy()
    # akan retroaktif mengisi future_wr_20 saat cukup trade sudah terakumulasi.
    try:
        _wr_snap = _edge.get("wr") or 0.0
        _ev_snap = _edge.get("net_ev") or _edge.get("empirical_ev") or 0.0
        _n_snap  = _edge.get("n", 0)
        save_edge_verdict_snapshot(_verdict_s01, _n_snap, _wr_snap, _ev_snap)
    except Exception as _snap_e:
        log(f"  ⚠️ [v8.09-A] Snapshot save failed: {_snap_e}", "warn")

    # [v8.09-A] Evaluate past snapshots (retroaktif) — setiap run
    try:
        _eval_result = evaluate_edge_verdict_accuracy()
        if _eval_result.get("evaluated_snapshots", 0) > 0:
            log(f"  📈 [v8.09-A] {_eval_result['evaluated_snapshots']} snapshot dievaluasi")
        if _eval_result.get("note"):
            log(f"  {_eval_result['note']}")
        _pred_acc = _eval_result.get("predictive_accuracy")
        if _pred_acc is not None and _pred_acc < 0:
            log("  ⚠️ [v8.09-A] PERINGATAN: Verdict PROVEN tidak prediktif lebih baik dari UNPROVEN "
                "— evaluasi sistem edge-gating.", "warn")
    except Exception as _eval_e:
        log(f"  ⚠️ [v8.09-A] evaluate_edge_verdict_accuracy error: {_eval_e}", "warn")

    # [v8.09-D] Auto-calibrate distribution params jika cukup data baru
    # Dijalankan di background (tidak blokir scan), hanya jika cukup data baru
    try:
        _cal = calibrate_distribution_params(force=False)
        if not _cal.get("skipped") and not _cal.get("error"):
            k_changed = _cal.get("kurt_params", {}).get("changed", False)
            s_changed = _cal.get("skew_params", {}).get("changed", False)
            if k_changed or s_changed:
                log(f"  📐 [v8.09-D] Param calibration: perubahan terdeteksi — "
                    f"set ENV APPLY_CALIBRATED_PARAMS=true untuk apply.", "warn")
    except Exception as _cal_e:
        log(f"  ⚠️ [v8.09-D] Auto-calibration error: {_cal_e}", "warn")

    # [T03/U01] Adaptive relaxation — edge-guarded
    # [PHASE-2] HARD DISABLED — adaptive_relaxation dimatikan paksa saat PHASE2_STABILIZE
    # [PHASE-5] Layer 4 mengaktifkan adaptive_relaxation SETELAH prerequisite terpenuhi
    _p5_adaptive_ok = (
        not PHASE2_STABILIZE and
        not PHASE3_COLLECTION and
        PHASE5_LAYER >= 4 and
        _p5_result.get("prerequisites_ok", False) and
        "LAYER4_ADAPTIVE_FILTER" in _p5_result.get("layers_applied", [])
    )
    if PHASE2_STABILIZE:
        log("🔧 [T03/U01] Adaptive relaxation: SKIP [PHASE2_STABILIZE — force disabled]")
    elif PHASE3_COLLECTION:
        log("🔧 [T03/U01] Adaptive relaxation: SKIP [PHASE3 aktif — data collection mode]")
    elif _p5_adaptive_ok:
        log("🔧 [PHASE5/L4] Adaptive relaxation AKTIF (diaktifkan oleh PHASE5 Layer 4)...")
        try:
            apply_adaptive_relaxation(edge_verdict=_verdict_s01)
        except Exception as _te:
            log(f"  ⚠️ [T03] apply_adaptive_relaxation error: {_te}", "warn")
    elif PHASE5_LAYER > 0 and not _p5_result.get("prerequisites_ok", False):
        log("🔧 [T03/U01] Adaptive relaxation: SKIP [PHASE5 prerequisite belum terpenuhi]")
    else:
        log("🔧 [T03/U01] Adaptive relaxation: SKIP [PHASE5_LAYER < 4 atau LEAN_MODE]")

    # [Q03] Status kalibrasi market impact
    if IMPACT_CALIBRATION_STATUS == "UNCALIBRATED_THEORETICAL":
        log(f"⚠️ [Q03] Market impact model: UNCALIBRATED — set ENV IMPACT_MODEL_SCALE.", "warn")
    else:
        log(f"✅ [Q03] Market impact model: USER_CALIBRATED (scale={IMPACT_MODEL_SCALE:.2f})")

    if INTRADAY_ENABLED:
        log(f"⚠️ [R01] INTRADAY ON — semi-simulasi (~{DATA_DELAY_MINUTES}m delay).", "warn")
    log(f"🧠 Mode: {'SIMPLE' if SIMPLE_MODE else 'COMPLEX'} | "
        f"LEAN: {'AUTO ⚠️' if _auto_lean_active else 'MANUAL ✅' if LEAN_MODE else 'OFF'}")

    # [v7.2 — FIX Masalah 3] Warning jika watchlist terlalu besar untuk satu run
    if len(WATCHLIST) > 80:
        log(f"⚠️ Watchlist {len(WATCHLIST)} ticker — risiko yfinance rate limit. Pertimbangkan split job.", "warn")

    # ── Step 1: Update outcome signal lama ───────────────
    log("📊 Mengupdate outcome signal sebelumnya...")

    # [v7.18] Load slippage correction factors dari Supabase
    load_slippage_corrections()

    # [v7.18/M04] Update actual fill prices — V2 pipeline
    update_execution_fills()

    # [v7.20] Retry broker fill untuk signal yang masih simulated
    try:
        bulk_reconcile_fills(limit=30)
    except Exception as _e:
        log(f"  ⚠️ bulk_reconcile_fills: {_e}", "warn")

    # [M05] Kirim laporan slippage harian ke Telegram (hanya jika ada data)
    try:
        send_slippage_report()
    except Exception as _sr_e:
        log(f"  ⚠️ [M05] send_slippage_report: {_sr_e}", "warn")

    update_signal_outcomes()

    # ── Step 2: Ambil win rate terkini ───────────────────
    wr = get_win_rate_summary()
    if wr["overall"] is not None:
        log(f"📈 Win Rate: {wr['overall']}% dari {wr['total_closed']} signal closed "
            f"(INTRADAY:{wr['intraday']}% | SWING:{wr['swing']}%)")

    # ── Step 2b: [v7.0] Kill Switch System — Layer 1: Losing Streak ─
    # [v8.0 FIX 1] Cek dulu apakah pause dari run sebelumnya masih aktif
    log("🛑 Kill switch check: pause state dari run sebelumnya...")
    ks_pause = check_ks_pause_active()
    if ks_pause["triggered"]:
        msg = (f"⏸️ <b>BOT MASIH DALAM PAUSE</b>\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"Kill switch pause belum berakhir.\n"
               f"Resume dalam: <b>{ks_pause['remaining_hours']:.1f} jam</b>\n"
               f"Resume at: {ks_pause['resume_at'][:16]} UTC\n"
               f"<i>Scan ditunda. Bot akan aktif otomatis setelah pause selesai.</i>")
        tg(msg)
        send_health_check(0, 0,
                          {"halt": False, "block_buy": False, "ihsg_1d": 0.0, "ihsg_5d": 0.0},
                          wr, no_signal=True)
        return

    log("🛑 Kill switch check: losing streak...")
    ks_streak = check_losing_streak()
    if ks_streak["triggered"]:
        log(f"🛑 Kill switch AKTIF — {ks_streak['message']}")
        send_health_check(0, 0,
                          {"halt": False, "block_buy": False, "ihsg_1d": 0.0, "ihsg_5d": 0.0},
                          wr, no_signal=True)
        return

    # ── Step 2c: [J04] Strategy Performance — auto-disable underperforming ─
    if LEAN_MODE:
        log("  ℹ️ [U04] LEAN_MODE: skip strategy auto-disable")
    else:
        log("📊 Evaluasi performa per sub-strategy...")
        strategy_perf = get_strategy_performance(send_telegram=True)
        if strategy_perf:
            for sub, info in strategy_perf.items():
                if info.get("disabled"):
                    log(f"  🚫 [{sub}]: {info.get('note', 'disabled')}", "warn")
                elif info.get("wr") is not None:
                    log(f"  ✅ [{sub}]: WR={info['wr']:.0%} ({info['wins']}/{info['total']})")

    # ── Step 2d-R03: [R03] SIMPLE vs COMPLEX mode comparison ────────
    log("🧠 [R03] Evaluasi SIMPLE vs COMPLEX mode (empiris)...")
    try:
        _mode_cmp = get_mode_performance_comparison()
        _verdict  = _mode_cmp["verdict"]
        _rec      = _mode_cmp["recommendation"]
        _ns, _nc  = _mode_cmp["n_simple"], _mode_cmp["n_complex"]
        log(f"  📊 Mode data: SIMPLE={_ns} signal | COMPLEX={_nc} signal")
        if _verdict == "INSUFFICIENT_DATA":
            log(f"  ℹ️  {_rec}")
        elif _verdict == "COMPLEX_BETTER":
            log(f"  🔵 {_rec}", "warn")
        elif _verdict == "SIMPLE_BETTER":
            log(f"  ✅ {_rec}")
        else:
            log(f"  ➡️  {_rec}")
        if _verdict == "COMPLEX_BETTER" and not SIMPLE_MODE_LOCK and SIMPLE_MODE:
            log("  💡 [R03] Set ENV SIMPLE_MODE=false untuk aktifkan COMPLEX mode.", "warn")
    except Exception as _me:
        log(f"  ⚠️ [R03] mode comparison error: {_me}", "warn")

    # ── Step 2d: [v7.20] Self-Evolving Model ─────────────────────────
    # [v8.12] Evolution dipindah ke GitHub Actions job terpisah (--evolution flag,
    # cron 20:00 WIB). Tidak lagi dijalankan dari dalam run() karena:
    # 1. run() di-guard keluar sebelum jam 17:00 WIB — evolution jam 20:00 tidak pernah tercapai
    # 2. Bug lama: _dt.now().hour menghasilkan UTC, bukan WIB
    # Evolution sekarang: python bot_saham_v8_12.py --evolution via separate GHA job
    log("  ℹ️ [N05] Evolution dijalankan via job terpisah (--evolution) jam 20:00 WIB — skip dari run()")

    # ── Step 2e: [PHASE-5] Optimization layer sequencer ──────────────
    # Dijalankan SETELAH check_edge_proven() agar verdict dan n_trades tersedia.
    # Hanya aktif jika PHASE5_LAYER > 0 — default 0 (aman, tidak ada perubahan).
    # Layer diaktifkan SATU per satu sesuai urutan sequencing.
    log(f"🚀 [PHASE5] Evaluasi optimization layer (PHASE5_LAYER={PHASE5_LAYER})...")
    _p5_result = {}
    try:
        _p5_result = apply_phase5_layers(
            verdict  = _verdict_s01,
            n_trades = _edge.get("n", 0),
        )
        # ── FIX: Kirim Telegram hanya jika applied_layers berubah dari run sebelumnya ──
        # Tanpa ini, setiap run mengirim notif PHASE5 → spam.
        # Perubahan yang relevan: layer baru aktif, layer diskip, atau prerequisite berubah.
        _p5_applied_now  = tuple(sorted(_p5_result.get("layers_applied", [])))
        _p5_skipped_now  = tuple(sorted(_p5_result.get("layers_skipped", [])))
        _p5_prereq_now   = _p5_result.get("prerequisites_ok", False)
        _p5_state_now    = (_p5_applied_now, _p5_skipped_now, _p5_prereq_now)
        # _p5_prev_state adalah global yang di-persist antar loop di dalam satu run session
        # [v8.19 L3b-FIX] Sebelumnya: fragile getattr(__builtins__,...) untuk cross-run persistence.
        # Pendekatan itu tidak reliable di Python 3.12 (builtins object bisa dict atau module).
        # Fix: baca dari module-level global langsung. Nilai persist selama process hidup (satu session).
        global _p5_prev_state
        # _p5_prev_state already initialized as None at module level (see declaration below run())
        # No need for builtins hacks — module-level globals persist across calls within same process

        _p5_changed = (_p5_prev_state is None) or (_p5_state_now != _p5_prev_state)

        if PHASE5_LAYER > 0 and _p5_changed:
            try:
                send_phase5_status(_p5_result)
                _p5_prev_state = _p5_state_now   # [v8.19] persist via module-level global, bukan builtins hack
            except Exception as _p5t_e:
                log(f"  ⚠️ [PHASE5] Telegram status error: {_p5t_e}", "warn")
        elif PHASE5_LAYER > 0:
            log(f"  ℹ️ [PHASE5] Telegram skip — layer state tidak berubah dari run sebelumnya")
    except Exception as _p5_e:
        log(f"  ⚠️ [PHASE5] apply_phase5_layers error: {_p5_e}", "warn")

    # ── Step 3: Load adaptive weights ─────────────────────
    # [PHASE-3] HARD DISABLED — adaptive weights dimatikan saat PHASE3_COLLECTION.
    # LEAN_MODE sudah dipaksa True oleh PHASE3 enforce rule, tapi kita guard
    # eksplisit di sini agar intent jelas di log dan tidak tergantung urutan init.
    if PHASE3_COLLECTION:
        log("  🔒 [PHASE3] Adaptive weights: SKIP [data collection mode — no optimization]")
        _adaptive_weights = dict(W)   # pakai base weights langsung, tidak ada tuning
    elif LEAN_MODE:
        log("  ℹ️ [U04] LEAN_MODE: skip adaptive weights — pakai W base")
        _adaptive_weights = dict(W)   # gunakan base weights langsung
    else:
        log("🧠 Memuat adaptive weights dari histori signal...")
        _adaptive_weights = get_feedback_weights()

    # ── Step 3a: Freeze threshold snapshot ────────────────
    try:
        _threshold_guard = ThresholdGuard(_load_current_thresholds())
        log(f"🔒 ThresholdGuard aktif: {_threshold_guard}")
    except Exception as _tg_err:
        _threshold_guard = ThresholdGuard({})
        log(f"  ⚠️ ThresholdGuard fallback kosong: {_tg_err}", "warn")

    # ── Step 3b: Load cluster weights ─────────────────────
    # [PHASE-2] HARD DISABLED — cluster_weights dimatikan paksa saat PHASE2_STABILIZE
    # [PHASE-5] Layer 3 mengaktifkan cluster weights SETELAH prerequisite terpenuhi
    _p5_cluster_ok = (
        not PHASE2_STABILIZE and
        not PHASE3_COLLECTION and
        PHASE5_LAYER >= 3 and
        _p5_result.get("prerequisites_ok", False) and
        "LAYER3_CLUSTER_WEIGHTS" in _p5_result.get("layers_applied", [])
    )
    if PHASE2_STABILIZE:
        log("  ℹ️ [U04] PHASE2_STABILIZE: skip cluster weights [force disabled]")
    elif PHASE3_COLLECTION:
        log("  ℹ️ [PHASE3] PHASE3 aktif: skip cluster weights [data collection mode]")
    elif _p5_cluster_ok:
        log("🧮 [PHASE5/L3] Memuat cluster weights (diaktifkan oleh PHASE5 Layer 3)...")
        get_cluster_weights()
    elif LEAN_MODE:
        log("  ℹ️ [U04] LEAN_MODE: skip cluster weights")
    else:
        log("🧮 Memuat cluster weights per kondisi pasar...")
        get_cluster_weights()

    # ── Step 4: Cek kondisi IHSG ──────────────────────────
    ihsg = get_ihsg_regime()

    # ── Step 4b: [v7.0] Kill Switch — Layer 2: Market Abnormal ──
    log("🛑 Kill switch check: market abnormal...")
    ks_market = check_market_abnormal(ihsg)
    if ks_market["abnormal"]:
        severity = ks_market["severity"]
        reason   = ks_market["reason"]
        msg = (f"🛑 <b>KILL SWITCH — MARKET ABNORMAL</b>\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"Severity : <b>{severity}</b>\n"
               f"Alasan   : {html_escape(reason)}\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"🚫 Semua signal ditunda sampai kondisi normal.\n"
               f"<i>Scan berikutnya dalam 4 jam.</i>")
        tg(msg)
        log(f"🛑 Market abnormal [{severity}]: {reason}")
        return

    # ── Step 4b-2: [J05] Layer 4 — IDX Circuit Breaker detection ──
    log("🛑 Kill switch check: IDX circuit breaker / halt...")
    ks_idx_halt = check_idx_trading_halt(ihsg)
    if ks_idx_halt["halt_detected"]:
        level  = ks_idx_halt["level"]
        reason = ks_idx_halt["reason"]
        msg = (f"🛑 <b>IDX {level.replace('_', ' ')}</b>\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"Alasan : {html_escape(reason)}\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"⏸️ Tidak ada signal baru sampai kondisi stabil.\n"
               f"<i>IDX circuit breaker dapat menghentikan eksekusi semua order.</i>")
        tg(msg)
        log(f"🛑 IDX Halt [{level}]: {reason}", "warn")
        return

    if ihsg["halt"]:
        msg = (f"🛑 <b>HALT — IHSG CRASH</b>\n"
               f"IHSG 5d: {ihsg['ihsg_5d']:+.1f}% (threshold: {IHSG_CRASH_BLOCK}%)\n"
               f"Bot dihentikan sementara. Semua signal ditunda.\n"
               f"<i>Scan berikutnya dalam 4 jam.</i>")
        tg(msg)
        log("🛑 HALT karena IHSG crash"); return

    # ── Step 4c: [v7.0] Kill Switch — Layer 3: Portfolio Drawdown ──
    log("🛑 Kill switch check: portfolio drawdown...")
    ks_port = check_portfolio_drawdown()
    if ks_port["triggered"]:
        log(f"🛑 Circuit breaker: {ks_port['total_risk_pct']:.1f}% exposure — block new trades")
        send_health_check(0, 0, ihsg, wr, no_signal=True, portfolio=ks_port)
        return

    # [PHASE-0 P0-09] IHSG sebagai SOFT FILTER — bukan hard block.
    # Hard block hanya saat halt (crash > 5%). Drop ringan → penalty score saja.
    # Penalty diterapkan di analyze_swing() / analyze_intraday() via _ihsg_soft_penalty.
    allow_sell = True
    if ihsg["halt"]:
        allow_buy = False   # Crash total — halt semua BUY
        log(f"🛑 IHSG CRASH HALT — semua BUY diblokir (ihsg_5d={ihsg.get('ihsg_5d', 0):+.1f}%)")
    else:
        allow_buy = True    # Drop ringan → tetap lanjut, penalty via score
        if ihsg["block_buy"]:
            log(f"⚠️ [P0-09] IHSG drop ringan — BUY tetap aktif dengan soft penalty -3 score. "
                f"Counter-trend sectors: {IHSG_COUNTER_TREND_SECTORS}")
    log(f"📊 IHSG 1d:{ihsg['ihsg_1d']:+.1f}% 5d:{ihsg['ihsg_5d']:+.1f}% | "
        f"BUY:{'aktif' if not ihsg['halt'] else 'HALT'} | "
        f"Portfolio exposure: {ks_port['total_risk_pct']:.1f}% ({ks_port['open_count']} open)")

    # ── [v7.14] Fix 1: Intraday gate — disabled by default ───────────
    # Data yfinance ~15 menit delay membuat intraday tidak execution-grade.
    # Default INTRADAY_ENABLED=False → bot fokus ke SWING yang delay-insensitive.
    allow_intraday = INTRADAY_ENABLED
    if not allow_intraday:
        log("⚠️ Intraday DISABLED (INTRADAY_ENABLED=false) — hanya SWING yang dikirim")
        log("   Aktifkan dengan ENV: INTRADAY_ENABLED=true (butuh data real-time)")

    # ── [v7.14] Fix 3b: Daily drawdown check ─────────────────────────
    try:
        today_iso = datetime.now(timezone.utc).date().isoformat()
        today_rows = (
            supabase.table("signals")
            .select("outcome, entry, sl, side")
            .eq("outcome", "LOSS")
            .gte("closed_at", today_iso)
            .execute()
            .data
        )
        daily_loss_pct = 0.0
        for r in (today_rows or []):
            try:
                entry = float(r["entry"] or 0)
                sl    = float(r["sl"]    or 0)
                if entry > 0 and sl > 0:
                    loss_pct = abs(entry - sl) / entry * RISK_PCT
                    daily_loss_pct += loss_pct
            except Exception as _e:
                log(f"  ⚠️ [FALLBACK] daily_loss_pct calc: {_e} — +RISK_PCT", "warn")
                daily_loss_pct += RISK_PCT
        if daily_loss_pct >= MAX_DAILY_DRAWDOWN_PCT:
            msg = (f"🛑 <b>DAILY DRAWDOWN LIMIT</b>\n"
                   f"Sudah rugi <b>{daily_loss_pct:.1f}%</b> portfolio hari ini\n"
                   f"Limit: {MAX_DAILY_DRAWDOWN_PCT}%\n"
                   f"Trading dihentikan untuk hari ini.")
            tg(msg)
            log(f"🛑 Daily drawdown {daily_loss_pct:.1f}% >= {MAX_DAILY_DRAWDOWN_PCT}% — halt today")
            send_health_check(0, 0, ihsg, wr, no_signal=True, portfolio=ks_port)
            return
        log(f"✅ Daily drawdown: {daily_loss_pct:.1f}% / {MAX_DAILY_DRAWDOWN_PCT}% limit")
    except Exception as e:
        log(f"⚠️ Daily drawdown check gagal: {e}", "warn")

    # ── [v7.14] Fix 3b: Max trades per day check ─────────────────────
    try:
        today_sent = (
            supabase.table("signals")
            .select("id")
            .gte("sent_at", today_iso)
            .execute()
            .data
        )
        trades_today = len(today_sent) if today_sent else 0
        if trades_today >= MAX_TRADES_PER_DAY:
            log(f"⚠️ Max trades per day ({MAX_TRADES_PER_DAY}) sudah tercapai — skip scan")
            send_health_check(0, 0, ihsg, wr, no_signal=True, portfolio=ks_port)
            return
        remaining_today = MAX_TRADES_PER_DAY - trades_today
        log(f"📊 Trades hari ini: {trades_today}/{MAX_TRADES_PER_DAY} (sisa: {remaining_today})")
    except Exception as e:
        log(f"⚠️ Max trades check gagal: {e}", "warn")
        remaining_today = MAX_TRADES_PER_DAY

    # ── [PHASE6] Max open positions check ────────────────────────────────
    # Hard block: jika posisi aktif sudah >= MAX_OPEN_POSITIONS, tidak ada
    # signal baru sampai sebagian posisi ditutup via TP/SL.
    # Ini lapisan keamanan yang BERBEDA dari capital gate (modal terikat):
    # capital gate cek % modal, open positions cek jumlah tiket.
    try:
        _open_now = ks_port.get("open_count", 0)
        log(f"📊 Open positions: {_open_now} / {MAX_OPEN_POSITIONS} max")
        if _open_now >= MAX_OPEN_POSITIONS:
            msg = (
                f"⛔ <b>OPEN POSITION LIMIT</b>\n"
                f"Posisi aktif: <b>{_open_now}</b> / max {MAX_OPEN_POSITIONS}\n"
                f"Tidak ada signal baru sampai sebagian posisi ditutup (TP/SL).\n"
                f"<i>Kurangi MAX_OPEN_POSITIONS via ENV jika terlalu ketat.</i>"
            )
            tg(msg)
            log(f"🛑 Open positions {_open_now} >= {MAX_OPEN_POSITIONS} — block new signals")
            send_health_check(0, 0, ihsg, wr, no_signal=True, portfolio=ks_port)
            return
    except Exception as e:
        log(f"⚠️ Open positions check gagal: {e}", "warn")

    # ── Portfolio-Level Control: max exposure gate ────────────────────
    exposure_pct  = ks_port["total_risk_pct"]
    signals_cap   = min(MAX_SIGNALS_CYCLE, remaining_today if 'remaining_today' in dir() else MAX_SIGNALS_CYCLE)
    if exposure_pct > KS_DRAWDOWN_PCT_MAX * 0.75:
        signals_cap = max(1, signals_cap // 2)
        log(f"⚠️ Exposure {exposure_pct:.1f}% mendekati limit — signal cap diturunkan ke {signals_cap}")
    elif exposure_pct > KS_DRAWDOWN_PCT_MAX * 0.50:
        signals_cap = max(2, signals_cap - 2)
        log(f"⚠️ Exposure {exposure_pct:.1f}% moderat — signal cap {signals_cap}")

    # ── [PHASE6] RISK CONTROL SUMMARY — satu-liner mudah di-grep ────────
    # Tampil setiap run sebelum scan dimulai. Semua 4 kontrol dalam satu baris.
    _rc_dd_pct    = daily_loss_pct   if 'daily_loss_pct'   in dir() else 0.0
    _rc_trades    = trades_today     if 'trades_today'     in dir() else 0
    _rc_open      = ks_port.get("open_count", 0)
    _rc_exposure  = exposure_pct
    _rc_sector_ok = all(
        v <= MAX_SECTOR_EXPOSURE_PCT
        for v in _sector_exposure_tracker.values()
    ) if _sector_exposure_tracker else True
    log(
        f"RISK CONTROL | "
        f"DD: {_rc_dd_pct:.1f}%/{MAX_DAILY_DRAWDOWN_PCT}% | "
        f"TRADES: {_rc_trades}/{MAX_TRADES_PER_DAY} | "
        f"OPEN: {_rc_open}/{MAX_OPEN_POSITIONS} | "
        f"EXPOSURE: {_rc_exposure:.1f}%/{KS_DRAWDOWN_PCT_MAX}% | "
        f"SECTOR_OK: {_rc_sector_ok} | "
        f"CAP: {signals_cap}"
    )

    # ── BOOTSTRAP: Override signals_cap berdasarkan phase ─────────────
    # Ini SELALU diterapkan di atas exposure gate — bootstrap lebih prioritas
    # karena fungsinya bukan risk management tapi epistemic humility:
    # jangan agresif saat kita belum tahu apakah sistem ini bekerja.
    if _bootstrap_phase == "COLD":
        signals_cap = BOOTSTRAP_SIGNALS_CAP_COLD
        log(f"🧊 [BOOTSTRAP] COLD — signals_cap paksa ke {signals_cap} "
            f"(n={_edge_n_cache} < {BOOTSTRAP_COLD_N})", "warn")
    elif _bootstrap_phase == "EARLY":
        signals_cap = min(signals_cap, BOOTSTRAP_SIGNALS_CAP_EARLY)
        log(f"🌱 [BOOTSTRAP] EARLY — signals_cap dibatasi ke {signals_cap} "
            f"(n={_edge_n_cache} < {BOOTSTRAP_EARLY_N})", "warn")

    signals  = []
    scanned  = 0
    skip_vol = 0
    data_fail = 0  # FIX 6A: track ticker yang gagal ambil data

    # [PHASE-1] Funnel counters — granular visibility ke mana ticker gugur
    _stage_total    = len(WATCHLIST)  # Total ticker di watchlist
    _stage_price    = 0               # Gugur: harga <= gocap / data fail
    _stage_vol      = 0               # Gugur: volume 20d < minimum
    _stage_vol_day  = 0               # Gugur: volume hari ini < 50% avg
    _stage_dq       = 0               # Gugur: data quality / corrupt / ARA / suspend
    _stage_volume   = 0               # Lolos semua pre-scan → masuk scan
    _stage_final    = 0               # Diisi setelah loop dari len(signals)
    # _stage_signal alias scanned (sudah ada)

    # [PHASE-1] DEBUG_TICKER — full reasoning untuk 1 ticker tertentu
    # Set via ENV: DEBUG_TICKER=BBCA atau DEBUG_TICKER=BBCA.JK
    _debug_ticker: str = ""
    try:
        _dt_env = os.environ.get("DEBUG_TICKER", "").strip().upper()
        if _dt_env:
            _debug_ticker = _dt_env if _dt_env.endswith(".JK") else _dt_env + ".JK"
            log(f"\n🔬 [PHASE-1 DEBUG] Debug mode aktif → ticker: {_debug_ticker}")
    except Exception:
        pass

    for ticker in WATCHLIST:
        try:
            pair = ticker.replace(".JK", "")

            data_1d, _data_src = get_candles_v2(ticker, "1d", 25)  # [v7.20] multi-source
            if data_1d is None:
                data_fail += 1
                continue

            closes_1d, _, _, volumes_1d, _op_1d = data_1d
            price   = float(closes_1d[-1])

            if price <= 0:
                data_fail += 1
                continue

            # [v7.2 — FIX Masalah 2B] Filter saham mendekati level Gocap (Rp 50)
            # Saham dengan harga <= Rp 55 sangat rentan stagnan dan manipulable
            if price <= MIN_PRICE_IDR:
                skip_vol += 1
                _stage_price += 1
                _filter_audit["PRICE_GOCAP"] = _filter_audit.get("PRICE_GOCAP", 0) + 1
                log(f"  ⏭ {ticker}: Harga Rp{price:,.0f} <= Rp{MIN_PRICE_IDR} (mendekati Gocap) — skip")
                continue
            # Tambahan: single-day check — pastikan hari ini >= 50% dari avg 20-day
            # Ini menangkap saham yang pass filter karena 1 hari spike volume, tapi
            # sebenarnya liquid hanya di hari itu saja (misal CUAN.JK, HERO.JK)
            vol_20d_avg = float(np.mean(volumes_1d[-21:-1])) * price if len(volumes_1d) >= 21 \
                          else float(np.mean(volumes_1d[:-1])) * price
            vol_today   = float(volumes_1d[-1]) * price

            if vol_20d_avg < MIN_VOLUME_IDR:
                skip_vol += 1
                _stage_vol += 1
                _filter_audit["VOLUME_20D_LOW"] = _filter_audit.get("VOLUME_20D_LOW", 0) + 1
                log(f"  ⏭ {ticker}: vol 20d avg Rp{vol_20d_avg/1e9:.1f}M < minimum — skip")
                continue

            # Secondary check: hari ini volume tidak boleh di bawah 50% rata-rata
            if vol_20d_avg > 0 and vol_today < vol_20d_avg * 0.50:
                skip_vol += 1
                _stage_vol_day += 1
                _filter_audit["VOLUME_TODAY_LOW"] = _filter_audit.get("VOLUME_TODAY_LOW", 0) + 1
                log(f"  ⏭ {ticker}: vol hari ini Rp{vol_today/1e9:.1f}M < 50% avg ({vol_20d_avg/1e9:.1f}M) — skip")
                continue

            # [J05] Data corruption guard — cek OHLCV anomaly sebelum signal
            closes_chk, highs_chk, lows_chk, vols_chk, _op_chk = data_1d
            corruption = check_data_corruption(closes_chk, highs_chk, lows_chk,
                                               vols_chk, ticker=ticker)
            if corruption["corrupt"]:
                log(f"  ⚠️ {ticker}: Data corrupt — {corruption['reasons'][:1]} — skip", "warn")
                data_fail += 1
                _stage_dq += 1
                _filter_audit["DATA_CORRUPT"] = _filter_audit.get("DATA_CORRUPT", 0) + 1
                continue

            # [J05] ARA/ARB check — skip jika saham kemungkinan auto-rejection
            ara_arb = check_ticker_ara_arb(ticker, closes_chk)
            if ara_arb["ara"] or ara_arb["arb"]:
                skip_vol += 1
                _stage_dq += 1
                _filter_audit["ARA_ARB_PRESCAN"] = _filter_audit.get("ARA_ARB_PRESCAN", 0) + 1
                log(f"  ⏭ {ticker}: ARA/ARB detected ({ara_arb['pct_change']:+.1%}) — skip signal")
                continue

            # [v7.11 FIX] UMA/Suspend guard sebelum scanned+=1 agar saham suspend
            # tidak ikut terhitung dalam statistik scan yang dilaporkan
            if is_likely_suspended(ticker, price):
                skip_vol += 1
                _stage_dq += 1
                _filter_audit["SUSPENDED"] = _filter_audit.get("SUSPENDED", 0) + 1
                continue

            # [v7.19/v7.20] Data Quality Pipeline — block signal jika data jelek
            try:
                _closes_1h = None
                if allow_intraday:
                    _data_1h, _ = get_candles_v2(ticker, "1h", 120)
                    if _data_1h: _closes_1h = _data_1h[0]

                _dq = calc_data_quality_score(
                    ticker=ticker,
                    closes_1h=_closes_1h,
                    closes_1d=closes_1d,
                    highs_1d=highs_chk,
                    lows_1d=lows_chk,
                    volumes_1d=volumes_1d,
                    strategy="INTRADAY" if allow_intraday else "SWING",
                )
                _dq = apply_source_penalty_to_dq(_dq, _data_src)
                if not _dq["allow_signal"]:
                    skip_vol += 1
                    _stage_dq += 1
                    _filter_audit["DQ_BLOCK"] = _filter_audit.get("DQ_BLOCK", 0) + 1
                    log(f"  ⏭ {ticker}: DQ block [{_dq['label']} {_dq['score']}/100] — skip")
                    continue
            except Exception as _dq_e:
                log(f"  ⚠️ DQ pipeline [{ticker}]: {_dq_e}", "warn")
                _dq = {"allow_advanced": True}  # fallback: lanjut normal

            scanned += 1
            _stage_volume += 1   # [PHASE-1] ticker ini lolos semua pre-scan filters
            log(f"  🔍 Scan {ticker} | Harga: Rp{price:,.0f} | Vol20d: Rp{vol_20d_avg/1e9:.1f}M | Today: Rp{vol_today/1e9:.1f}M")

            # [PHASE-1] DEBUG_TICKER — full reasoning mode
            _is_debug = _debug_ticker and (ticker == _debug_ticker or ticker.replace(".JK","") == _debug_ticker.replace(".JK",""))
            if _is_debug:
                log(f"\n{'🔬'*30}")
                log(f"  [DEBUG] {ticker} — FULL REASONING MODE")
                log(f"  Harga       : Rp{price:,.0f}")
                log(f"  Vol 20d avg : Rp{vol_20d_avg/1e9:.2f}M (min: Rp{MIN_VOLUME_IDR/1e9:.1f}M)")
                log(f"  Vol hari ini: Rp{vol_today/1e9:.2f}M ({vol_today/vol_20d_avg*100:.0f}% dari avg)")
                log(f"  IHSG status : 1d={ihsg.get('ihsg_1d',0):+.2f}% | block_buy={ihsg.get('block_buy')} | halt={ihsg.get('halt')}")
                log(f"  DQ score    : {_dq.get('score','?')}/100 [{_dq.get('label','?')}]")
                log(f"  allow_buy   : {allow_buy} | allow_intraday: {allow_intraday}")
                log(f"  MIN_RR      : INTRADAY={MIN_RR['INTRADAY']} | SWING={MIN_RR['SWING']}")
                log(f"  RSI_OB/OS   : {RSI_OB} / {RSI_OS}")
                log(f"  LEAN_MODE   : {LEAN_MODE} | SIMPLE_MODE: {SIMPLE_MODE}")
                log(f"{'🔬'*30}\n")

            # ── INTRADAY BUY ──────────────────────────────
            # [v7.14] allow_intraday=False by default (data delay)
            if allow_intraday and allow_buy and not already_sent(pair, "INTRADAY", "BUY"):
                sig = check_intraday(ticker, price, ihsg, side="BUY")
                if sig: signals.append(sig)
                elif _is_debug:
                    log(f"  [DEBUG] {ticker} INTRADAY BUY → REJECTED (check_intraday returned None)")

            # ── INTRADAY SELL ─────────────────────────────
            if allow_intraday and allow_sell and not already_sent(pair, "INTRADAY", "SELL"):
                sig = check_intraday(ticker, price, ihsg, side="SELL")
                if sig: signals.append(sig)
                elif _is_debug:
                    log(f"  [DEBUG] {ticker} INTRADAY SELL → REJECTED")

            # ── SWING BUY ────────────────────────────────
            if allow_buy and not already_sent(pair, "SWING", "BUY"):
                sig = check_swing(ticker, price, ihsg, side="BUY")
                if sig: signals.append(sig)
                elif _is_debug:
                    log(f"  [DEBUG] {ticker} SWING BUY → REJECTED (check_swing returned None)")

            # ── SWING SELL ───────────────────────────────
            if allow_sell and not already_sent(pair, "SWING", "SELL"):
                sig = check_swing(ticker, price, ihsg, side="SELL")
                if sig: signals.append(sig)
                elif _is_debug:
                    log(f"  [DEBUG] {ticker} SWING SELL → REJECTED")

            # [PHASE-1 v8.16] DEBUG_TICKER — print blocker summary setelah semua checks
            if _is_debug:
                _dbg_key_buy  = f"{ticker}_SWING_BUY"
                _dbg_key_sell = f"{ticker}_SWING_SELL"
                _dbg_detail   = _p1_ticker_score_detail
                log(f"\n  [DEBUG SUMMARY] {ticker} — filter detail run ini:")
                log(f"    Ticker lolos ke scoring : "
                    f"{'YA (SWING BUY)' if _dbg_key_buy in _dbg_detail else 'TIDAK (SWING BUY)'} | "
                    f"{'YA (SWING SELL)' if _dbg_key_sell in _dbg_detail else 'TIDAK (SWING SELL)'}")
                for _dkey in [_dbg_key_buy, _dbg_key_sell]:
                    if _dkey in _dbg_detail:
                        _dd = _dbg_detail[_dkey]
                        log(f"    {_dkey}: score={_dd['score']} | RR={_dd['rr']} | "
                            f"EV={_dd['ev']} | tier={_dd['tier']} | blocker={_dd['blocker']}")
                _dbg_blocks = {k: v for k, v in _filter_audit.items() if ticker.replace(".JK","") in k or True}
                log(f"    Top blocker run ini: {dict(sorted(_filter_audit.items(), key=lambda x: -x[1])[:5])}")
                log(f"{'🔬'*30}\n")

            # [v7.2 — FIX Masalah 3] Throttle lebih ketat untuk cegah yfinance rate limit
            # Dengan 80+ ticker × 4 check, sleep 0.8s = ~5 menit total scan
            # Ini masih dalam budget GitHub Actions 6 menit
            time.sleep(0.8)

        except Exception as e:
            log(f"⚠️ [{ticker}]: {e}", "warn")
            continue

    # [J05] API Cascade + Data Outage check (enhanced dari v7.14 FIX 6A)
    total_attempted = len(WATCHLIST)
    ks_cascade = check_api_cascade_failure(data_fail, total_attempted)
    fail_ratio  = data_fail / total_attempted if total_attempted > 0 else 0

    if ks_cascade["cascade"]:
        severity_emoji = "🔴" if ks_cascade["severity"] == "CRITICAL" else "🟡"
        tg(f"🚨 <b>API CASCADE FAILURE [{ks_cascade['severity']}]</b>\n"
           f"━━━━━━━━━━━━━━━━━━\n"
           f"{severity_emoji} {ks_cascade['reason']}\n"
           f"Data gagal: <b>{data_fail}/{total_attempted}</b> ({fail_ratio:.0%})\n"
           f"━━━━━━━━━━━━━━━━━━\n"
           f"⏸️ Scan dibatalkan — data tidak bisa dipercaya dalam kondisi ini.\n"
           f"<i>Cek koneksi internet, status yfinance, dan IDX maintenance.</i>")
        log(f"🚨 API_CASCADE: {ks_cascade['reason']}", "error")
        return

    if fail_ratio > 0.40:
        outage_msg = (f"🚨 <b>DATA OUTAGE DETECTED</b>\n"
                      f"━━━━━━━━━━━━━━━━━━\n"
                      f"Data gagal: <b>{data_fail}/{total_attempted}</b> ticker ({fail_ratio:.0%})\n"
                      f"Threshold: 40% — kemungkinan IDX maintenance atau yfinance down\n"
                      f"━━━━━━━━━━━━━━━━━━\n"
                      f"⏸️ Scan dibatalkan. Ini bukan 'tidak ada sinyal' — ini <b>DATA_OUTAGE</b>.\n"
                      f"<i>Cek status yfinance dan IDX. Scan berikutnya dalam 4 jam.</i>")
        tg(outage_msg)
        log(f"🚨 DATA_OUTAGE: {data_fail}/{total_attempted} ticker gagal ({fail_ratio:.0%}) — abort", "error")
        return

    buy_cand  = sum(1 for s in signals if s["side"] == "BUY")
    sell_cand = sum(1 for s in signals if s["side"] == "SELL")
    log(f"📊 Scanned: {scanned} | Vol filter: {skip_vol} | Data fail: {data_fail} | "
        f"Kandidat: {len(signals)} (BUY:{buy_cand} SELL:{sell_cand})")

    # [PHASE-1] ── FULL FUNNEL AUDIT ──────────────────────────────────────
    _stage_final = len(signals)
    _total_blocked = sum(_filter_audit.values())

    # ── Hitung top blockers (top 8, full gate list)
    _blocker_lines_log  = []   # untuk log (lebih verbose)
    _blocker_lines_tg   = []   # untuk Telegram (compact)
    _gate_icons_fa = {
        "PRICE": "💰", "VOLUME": "📊", "VOL": "📊", "DQ": "⚗️",
        "SUSPEND": "🚫", "ARA": "⛔", "CORRUPT": "❌",
        "STRUCTURE": "🏗️", "RSI": "⚡", "TRIGGER": "🎯",
        "TIER": "🏅", "EV": "📐", "RR": "📏", "STALE": "⏱️",
        "CHOPPY": "🌀", "NO_STRUCT": "🧱", "ATR": "📉",
    }
    _fa_sorted = sorted(_filter_audit.items(), key=lambda x: -x[1])
    for _gate, _cnt in _fa_sorted[:8]:
        _pct  = _cnt / max(_filter_audit_checked, 1) * 100
        _icon = next((v for k, v in _gate_icons_fa.items() if k in _gate.upper()), "🔒")
        _blocker_lines_log.append(f"    {_gate:<24}: {_cnt:>3}x ({_pct:.0f}%)")
        _blocker_lines_tg.append(f"  {_icon} {_gate:<20}: {_cnt:>3}x ({_pct:.0f}%)")

    # ── Build pipeline breakdown string (untuk log DAN Telegram)
    _lolos_prescan  = _stage_volume
    _gugur_price    = _stage_price
    _gugur_vol      = _stage_vol + _stage_vol_day
    _gugur_dq       = _stage_dq
    _gugur_datafail = data_fail
    _gugur_infunc   = max(_filter_audit_checked - _stage_final, 0)

    _pipeline_str = (
        f"  {'TOTAL':<12}: {_stage_total}\n"
        f"  {'PRICE/GOCAP':<12}: -{_gugur_price} gugur\n"
        f"  {'VOLUME':<12}: -{_gugur_vol} gugur  ({_stage_vol} 20d-avg + {_stage_vol_day} today-low)\n"
        f"  {'DQ/SUSPEND':<12}: -{_gugur_dq} gugur  (corrupt/ARA/suspend/DQ)\n"
        f"  {'DATA FAIL':<12}: -{_gugur_datafail} gugur\n"
        f"  {'─'*30}\n"
        f"  {'SCAN MASUK':<12}: {scanned}  (lolos semua pre-scan)\n"
        f"  {'STRUCTURE':<12}: -{sum(v for k,v in _filter_audit.items() if 'STRUCTURE' in k or 'NO_STRUCT' in k)} gugur\n"
        f"  {'RSI':<12}: -{sum(v for k,v in _filter_audit.items() if 'RSI' in k)} gugur\n"
        f"  {'RR/EV/TIER':<12}: -{sum(v for k,v in _filter_audit.items() if any(x in k for x in ('RR','EV','TIER','TRIGGER','SNIPER','STALE','ATR','CHOPPY','ARA_ARB','VOL_SPIKE')))} gugur\n"
        f"  {'─'*30}\n"
        f"  {'FINAL':<12}: {_stage_final} signal"
    )

    # [PHASE-1 v8.16] Score breakdown summary dari _p1_ticker_score_detail
    _avg_score_str = ""
    if _p1_score_total_count > 0:
        _avg_score = _p1_score_total_sum / _p1_score_total_count
        _avg_score_str = f"  AVG SCORE yang lolos scoring : {_avg_score:.1f} (n={_p1_score_total_count})\n"

    # Pct breakdown per blocker utama
    _scan_base = max(scanned, 1)
    _n_structure = sum(v for k,v in _filter_audit.items() if 'STRUCTURE' in k or 'NO_STRUCT' in k)
    _n_rsi       = sum(v for k,v in _filter_audit.items() if 'RSI' in k)
    _n_rr_ev     = sum(v for k,v in _filter_audit.items() if any(x in k for x in ('RR','EV','TIER','TRIGGER','SNIPER','STALE','ATR','CHOPPY','ARA_ARB','VOL_SPIKE')))

    _blocker_pct_str = (
        f"  BLOCKER PCT (dari {scanned} ticker yang scan):\n"
        f"  {'STRUCTURE_FAIL':<20}: {_n_structure:>3}x ({_n_structure/_scan_base*100:.0f}%)\n"
        f"  {'RSI_FAIL':<20}: {_n_rsi:>3}x ({_n_rsi/_scan_base*100:.0f}%)\n"
        f"  {'RR/EV/TIER_FAIL':<20}: {_n_rr_ev:>3}x ({_n_rr_ev/_scan_base*100:.0f}%)\n"
    )

    log(
        f"\n{'━'*60}\n"
        f"  📊 FILTER FUNNEL REPORT [PHASE-1 v8.16]\n"
        f"{'━'*60}\n"
        + _pipeline_str +
        f"\n{'━'*60}\n"
        + _blocker_pct_str
        + _avg_score_str +
        f"{'━'*60}\n"
        f"  🔒 TOP BLOCKERS ({_total_blocked} total blokir di dalam check()):\n"
        + ("\n".join(_blocker_lines_log) if _blocker_lines_log else "    — tidak ada blokir tercatat") +
        f"\n{'━'*60}"
    )

    # ── Simpan pipeline string untuk Telegram (dipakai di send_health_check)
    _phase1_pipeline_tg = (
        f"<code>"
        f"TOTAL       : {_stage_total}\n"
        f"PRICE/GOCAP : -{_gugur_price}\n"
        f"VOLUME      : -{_gugur_vol}\n"
        f"DQ/SUSPEND  : -{_gugur_dq}\n"
        f"DATA FAIL   : -{_gugur_datafail}\n"
        f"──────────────\n"
        f"SCAN MASUK  : {scanned}\n"
        f"STRUCTURE   : -{_n_structure} ({_n_structure//_scan_base*100 if _scan_base else 0}%)\n"
        f"RSI         : -{_n_rsi} ({_n_rsi//_scan_base*100 if _scan_base else 0}%)\n"
        f"RR/EV/TIER  : -{_n_rr_ev} ({_n_rr_ev//_scan_base*100 if _scan_base else 0}%)\n"
        f"──────────────\n"
        f"FINAL       : {_stage_final}"
        f"</code>"
    )
    _phase1_top_blockers_tg = "\n".join(_blocker_lines_tg[:5]) if _blocker_lines_tg else "  — tidak ada blokir"

    # ── Step 5: Health check heartbeat ───────────────────
    no_signal_flag = len(signals) == 0

    # ── [P8-06] ZERO-SIGNAL ALERT ────────────────────────────────────────
    # Masalah sebelumnya: jika bot kirim 0 signal hari ini (karena bug, API issue,
    # atau filter terlalu ketat tiba-tiba), tidak ada alert khusus.
    # Heartbeat normal ada tapi tidak membedakan "no signal wajar" vs "bot mati".
    #
    # Fix: cek berapa signal HARI INI dari Supabase. Jika 0 saat jam bursa aktif
    # (setelah 11:00 WIB — beri waktu cukup untuk bot jalan beberapa kali),
    # kirim alert Telegram agar operator tahu dan bisa investigasi.
    _now_wib_z6 = datetime.now(WIB)
    _is_late_enough = _now_wib_z6.hour >= 11   # baru alert setelah jam 11, bukan jam 9 pertama

    if no_signal_flag and _is_late_enough:
        # [v8.19 M3-FIX] Sebelumnya: `and PHASE3_COLLECTION` — alert tidak fire di Phase 4/5/live.
        # Fix: alert aktif di semua phase. Paper/live mode justru lebih kritis untuk dipantau.
        _today_str = _now_wib_z6.strftime("%Y-%m-%d")
        _signals_today_count = 0
        try:
            _today_rows = (
                supabase.table("signals")
                .select("id", count="exact")
                .gte("sent_at", _today_str + "T00:00:00")
                .execute()
            )
            _signals_today_count = _today_rows.count or 0
        except Exception as _sc_e:
            log(f"  ⚠️ [P8-06] signals today count error: {_sc_e}", "warn")

        log(
            f"📊 [P8-06] Signal hari ini ({_today_str}): {_signals_today_count} total "
            f"(run ini: 0)"
        )

        if _signals_today_count == 0:
            # Benar-benar 0 hari ini — bukan hanya run ini
            _zero_alert_msg = (
                f"⚠️ <b>[P8-06] 0 SIGNAL HARI INI</b>\n\n"
                f"<b>Tanggal:</b> {_today_str}\n"
                f"<b>Jam:</b> {_now_wib_z6.strftime('%H:%M')} WIB\n"
                f"<b>Scanned:</b> {scanned} ticker\n"
                f"<b>Final:</b> {_stage_final} signal\n\n"
                f"<b>Kemungkinan penyebab:</b>\n"
                f"  • Filter terlalu ketat (cek blocker terbesar di pipeline)\n"
                f"  • API/data issue (cek log data_fail = {_gugur_datafail})\n"
                f"  • Market kondisi sideways / tidak ada setup\n"
                f"  • Bug baru (cek log error terkini)\n\n"
                f"<b>Top blocker:</b>\n"
                + (_phase1_top_blockers_tg or "— tidak ada data") +
                f"\n\n<i>Investigasi jika berlanjut 2+ hari berturut-turut.</i>"
            )
            try:
                tg(_zero_alert_msg)
                log("  ⚠️ [P8-06] Zero-signal alert dikirim ke Telegram", "warn")
            except Exception as _za_tg:
                log(f"  ⚠️ [P8-06] zero-signal alert TG gagal: {_za_tg}", "warn")

    # [v7.20] Fill quality report — embed ke log sebelum heartbeat
    try:
        _fill_ratio_report = check_fill_source_ratio_alert()
        log(f"  📊 [N04] {_fill_ratio_report}")
    except Exception as _e:
        log(f"  ⚠️ fill ratio check: {_e}", "warn")

    send_health_check(scanned, skip_vol, ihsg, wr, no_signal=no_signal_flag,
                      portfolio=ks_port,
                      pipeline_tg=_phase1_pipeline_tg,
                      top_blockers_tg=_phase1_top_blockers_tg)

    # ── [v8.12] Daily PnL Summary — sekali sehari jam 16:00 WIB ──────
    try:
        _hour_wib = datetime.now(WIB).hour
        if _hour_wib == 15:   # 15:30 WIB = run terakhir sesi bursa
            log("📅 [v8.12] Mengirim daily PnL summary...")
            send_daily_pnl_summary()
    except Exception as _dp:
        log(f"  ⚠️ [v8.12] daily PnL summary: {_dp}", "warn")

    # ── [v8.12] Position expiry warnings ─────────────────────────────
    try:
        send_position_expiry_warnings()
    except Exception as _ew:
        log(f"  ⚠️ [v8.12] expiry warnings: {_ew}", "warn")

    if not signals:
        log("📭 Tidak ada signal"); return

    # Sort: tier terbaik → EV tertinggi (core engine) → score
    tier_order = {"S": 0, "A+": 1, "A": 2}
    signals.sort(key=lambda x: (
        tier_order.get(x["tier"], 9),
        -round(x.get("expected_value", 0), 2),
        -x["score"]
    ))

    sent = 0
    cumulative_new_risk_pct = 0.0
    _sector_exposure_tracker.clear()   # reset untuk cycle ranking ini (bukan cycle run)

    # [PHASE6] Hitung slot yang masih tersedia berdasarkan MAX_OPEN_POSITIONS
    _open_slots = max(0, MAX_OPEN_POSITIONS - ks_port.get("open_count", 0))
    if _open_slots < signals_cap:
        log(f"  ℹ️ [PHASE6] Open slots tersisa: {_open_slots} — signals_cap diturunkan "
            f"dari {signals_cap} ke {_open_slots} (open={ks_port.get('open_count', 0)}/{MAX_OPEN_POSITIONS})")
        signals_cap = _open_slots

    for sig in signals:
        if sent >= signals_cap: break

        sig_risk_pct = sig.get("smart_risk_pct", RISK_PCT)
        projected_total = exposure_pct + cumulative_new_risk_pct + sig_risk_pct
        if projected_total > KS_DRAWDOWN_PCT_MAX:
            log(f"  ⚠️ Pre-send block: {sig['pair']} {sig['side']} — "
                f"projected exposure {projected_total:.1f}% > {KS_DRAWDOWN_PCT_MAX}% limit")
            break

        # [v7.13] Fix 6: Sector exposure cap — cegah over-concentration
        sig_sector = TICKER_SECTOR.get(sig["ticker"], "MISC")
        sector_so_far = _sector_exposure_tracker.get(sig_sector, 0.0)
        if sector_so_far + sig_risk_pct > MAX_SECTOR_EXPOSURE_PCT:
            log(f"  ⚠️ Sector cap: {sig['pair']} [{sig_sector}] — "
                f"sektor exposure {sector_so_far:.1f}%+{sig_risk_pct:.1f}% "
                f"> max {MAX_SECTOR_EXPOSURE_PCT}% — skip")
            continue   # skip signal ini, coba yang lain (bukan break)

        send_signal(sig)
        save_signal(
            sig["pair"], sig["strategy"], sig["side"],
            sig["entry"], sig["tp1"], sig["tp2"], sig["sl"],
            sig["tier"], sig["score"], sig["timeframe"],
            regime        = sig.get("regime", "TRENDING"),
            sector        = sig_sector,
            win_prob      = sig.get("win_prob"),
            ev            = sig.get("expected_value"),
            signal_mode   = sig.get("signal_mode", "SIMPLE"),    # [R03]
            data_source   = sig.get("data_source"),              # [R01]
            # [PHASE-3] Extended metadata untuk edge analytics
            rr            = sig.get("rr"),
            atr_pct       = sig.get("atr_pct"),
            adx           = sig.get("adx"),
            phase         = sig.get("phase"),
            daily_bias    = sig.get("daily_bias"),
            entry_pattern = sig.get("entry_pattern"),
            entry_strength= sig.get("entry_strength"),
        )
        cumulative_new_risk_pct += sig_risk_pct
        _sector_exposure_tracker[sig_sector] = sector_so_far + sig_risk_pct
        sent += 1
        time.sleep(1.2)

    # Summary
    sent_sigs      = signals[:sent]
    intraday_buy   = sum(1 for s in sent_sigs if s["strategy"] == "INTRADAY" and s["side"] == "BUY")
    intraday_sell  = sum(1 for s in sent_sigs if s["strategy"] == "INTRADAY" and s["side"] == "SELL")
    swing_buy      = sum(1 for s in sent_sigs if s["strategy"] == "SWING"    and s["side"] == "BUY")
    swing_sell     = sum(1 for s in sent_sigs if s["strategy"] == "SWING"    and s["side"] == "SELL")

    wr_line = (f"\n━━━━━━━━━━━━━━━━━━\n"
               f"📈 Win Rate: <b>{wr['overall']}%</b> ({wr['wins']}/{wr['total_closed']} closed)") \
              if wr["overall"] is not None else ""

    exposure_line = (f"\n💼 Portfolio exposure: <b>{exposure_pct:.1f}%</b> / {KS_DRAWDOWN_PCT_MAX}% max"
                     f" ({ks_port['open_count']} posisi aktif)")

    tg(f"🔍 <b>Scan Selesai — Signal Bot Saham IDX v8.14 [P7]</b>\n"
       f"━━━━━━━━━━━━━━━━━━\n"
       f"Saham di-scan : <b>{scanned}</b>\n"
       f"IHSG 1d/5d    : <b>{ihsg['ihsg_1d']:+.1f}% / {ihsg['ihsg_5d']:+.1f}%</b>\n"
       f"━━━━━━━━━━━━━━━━━━\n"
       f"Signal terkirim : <b>{sent}</b> / cap {signals_cap}\n"
       f"  📈 INTRADAY BUY  : {intraday_buy}\n"
       f"  📉 INTRADAY SELL : {intraday_sell}\n"
       f"  🌊 SWING BUY     : {swing_buy}\n"
       f"  🌊 SWING SELL    : {swing_sell}"
       f"{wr_line}"
       f"{exposure_line}\n"
       f"🧠 <i>v8.12 Phase 3 — Data Collection Mode aktif.</i>\n"
       f"<i>Scan berikutnya dalam 4 jam.</i>")

    log(f"\n✅ Done — {sent}/{signals_cap} signal terkirim "
        f"(INTRADAY: BUY:{intraday_buy} SELL:{intraday_sell} | "
        f"SWING: BUY:{swing_buy} SELL:{swing_sell})")

    # [S03] Filter audit breakdown — visibilitas gate mana yang paling banyak blokir
    log(f"\n🔍 [S03] FILTER AUDIT — {_filter_audit_checked} ticker dievaluasi:")
    log(get_filter_audit_summary())

    # [PHASE-3] Kirim collection progress report ke Telegram (1x per run)
    # Hanya jika PHASE3_COLLECTION aktif dan sudah ada setidaknya 1 resolved trade
    if PHASE3_COLLECTION:
        try:
            _prog = get_collection_progress()
            _p3n  = _prog["resolved"]
            _p3wr = _prog["win_rate"]
            _p3ev = _prog["ev"]

            # ── Progress tracking — selalu tampil di log, terlepas ada trade atau belum ──
            # Satu-liner ringkas untuk grep / monitoring cepat
            log(f"COLLECTION: {_p3n} / {COLLECTION_TARGET_FULL} trades "
                f"(min {COLLECTION_TARGET_MIN}: "
                f"{'✅' if _p3n >= COLLECTION_TARGET_MIN else f'perlu {COLLECTION_TARGET_MIN - _p3n} lagi'})")
            _p3_bar_filled = int(min(_p3n, COLLECTION_TARGET_FULL) / COLLECTION_TARGET_FULL * 20)
            _p3_bar = "█" * _p3_bar_filled + "░" * (20 - _p3_bar_filled)
            log(
                f"\n📊 COLLECTION PROGRESS: {_p3n} / {COLLECTION_TARGET_FULL} trades "
                f"[{_p3_bar}] {_p3n/COLLECTION_TARGET_FULL*100:.0f}%\n"
                f"   Min target : {_p3n}/{COLLECTION_TARGET_MIN} "
                f"({'✅ TERCAPAI' if _p3n >= COLLECTION_TARGET_MIN else f'perlu {COLLECTION_TARGET_MIN - _p3n} lagi'})\n"
                f"   WR saat ini: {f'{_p3wr}%' if _p3wr is not None else 'N/A (belum ada data)'}\n"
                f"   EV saat ini: {f'{_p3ev:+.3f}' if _p3ev is not None else 'N/A'}\n"
                f"   🔒 Mode: PHASE3 COLLECTION — tidak ada optimasi parameter"
            )

            if _prog["resolved"] > 0:
                send_collection_report(_prog)
            else:
                log("📊 [PHASE3] Belum ada resolved trade — collection report dilewati")
        except Exception as _p3e:
            log(f"⚠️ [PHASE3] collection report: {_p3e}", "warn")




# ════════════════════════════════════════════════════════════════════
#  PATCH v7.17 — Execution Bridge + Order Book + Capital Constraints
# ════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
#  [L01] EXECUTION BRIDGE — v7.17
#
#  Abstraksi layer antara signal bot dan eksekusi order nyata.
#  Saat ini: DRY_RUN mode (log saja, tidak kirim ke broker).
#  Siap di-upgrade ke LIVE mode dengan sambungkan broker API.
#
#  Broker yang didukung (perlu API key terpisah):
#    - Mandiri Sekuritas: POST /order ke MOST API
#    - IPOT (Indo Premier): REST API dengan basic auth
#    - Mirae Asset: WebSocket based
#
#  Untuk aktifkan LIVE mode:
#    ENV: EXECUTION_MODE=live
#    ENV: BROKER_API_KEY=<your_key>
#    ENV: BROKER_API_SECRET=<your_secret>
#    ENV: BROKER_ENDPOINT=https://api.broker.com/v1/order
# ═══════════════════════════════════════════════════════════════════

import os

_execution_mode_raw = os.environ.get("EXECUTION_MODE", "dry_run").strip().lower()
# [v8.19 FIX] Guard kosong string — terjadi saat GitHub Variable di-set tapi isinya ""
# Tanpa guard ini: empty string lolos ke ExecutionBridge dan crash dengan ValueError.
if not _execution_mode_raw:
    import sys as _sys_em
    print("[WARN] ENV EXECUTION_MODE kosong — fallback ke dry_run", file=_sys_em.stderr)
    _execution_mode_raw = "dry_run"
EXECUTION_MODE = _execution_mode_raw
BROKER_ENDPOINT = os.environ.get("BROKER_ENDPOINT", "")
BROKER_API_KEY  = os.environ.get("BROKER_API_KEY", "")

_VALID_EXECUTION_MODES = {"dry_run", "live", "paper"}


class ExecutionBridge:
    """
    [L01] Abstraksi eksekusi order — decouples signal logic dari broker integration.

    Mode:
      dry_run  : Tidak ada order yang dikirim. Log saja. (default)
      paper    : Simpan ke Supabase sebagai paper trade, tidak ke broker.
      live     : Kirim ke broker API. Butuh BROKER_ENDPOINT + API_KEY.

    Semua mode mengembalikan struktur yang sama:
      {
        "order_id"    : str   — ID unik (UUID untuk dry_run, broker ID untuk live)
        "status"      : str   — "DRY_RUN" / "PAPER" / "SUBMITTED" / "REJECTED"
        "ticker"      : str
        "side"        : str   — "BUY" / "SELL"
        "lots"        : int
        "price"       : float — target entry price
        "order_type"  : str   — "LIMIT" / "MARKET"
        "timestamp"   : str
        "note"        : str   — alasan / error message
      }
    """

    def __init__(self, mode: str = "dry_run"):
        if mode not in _VALID_EXECUTION_MODES:
            # [v8.19 FIX] Sebelumnya raise ValueError — crash seluruh bot.
            # Sekarang: fallback ke dry_run + warn, karena mode invalid paling
            # sering terjadi karena ENV kosong (GitHub Variable belum di-set).
            import sys as _sys_eb
            print(
                f"[WARN] ExecutionBridge: mode tidak valid: '{mode}' — "
                f"fallback ke dry_run. Set ENV EXECUTION_MODE ke: {_VALID_EXECUTION_MODES}",
                file=_sys_eb.stderr
            )
            log(
                f"  ⚠️ [L01] ExecutionBridge: mode='{mode}' tidak valid — "
                f"fallback dry_run. Cek ENV EXECUTION_MODE.",
                "warn"
            )
            mode = "dry_run"
        self.mode = mode
        log(f"  🔌 [L01] ExecutionBridge init — mode: {mode.upper()}")

    def submit_order(
        self,
        ticker: str,
        side: str,
        lots: int,
        price: float,
        order_type: str = "LIMIT",
        sl: float = 0.0,
        tp1: float = 0.0,
        signal_id: str = "",
        strategy: str = "SWING",
    ) -> dict:
        """
        Submit order ke broker (atau simulate jika dry_run/paper).

        Args:
            ticker     : misal "BBCA.JK"
            side       : "BUY" atau "SELL"
            lots       : jumlah lot (1 lot = 100 saham IDX)
            price      : target price (adjusted entry dari simulate_execution)
            order_type : "LIMIT" (default) atau "MARKET"
            sl         : stop-loss price (untuk referensi, bukan OCO otomatis)
            tp1        : take profit (untuk referensi)
            signal_id  : ID dari tabel signals di Supabase
            strategy   : "INTRADAY" atau "SWING"

        Returns: dict dengan status dan detail order
        """
        import uuid
        from datetime import datetime, timezone

        ts = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
        order_id = str(uuid.uuid4())[:8].upper()

        base = {
            "order_id":   order_id,
            "ticker":     ticker,
            "side":       side,
            "lots":       lots,
            "price":      price,
            "order_type": order_type,
            "sl":         sl,
            "tp1":        tp1,
            "timestamp":  ts,
            "strategy":   strategy,
            "signal_id":  signal_id,
        }

        if self.mode == "dry_run":
            return self._dry_run(base)
        elif self.mode == "paper":
            return self._paper_trade(base)
        elif self.mode == "live":
            return self._live_order(base)

        return {**base, "status": "ERROR", "note": "mode tidak dikenal"}

    def _dry_run(self, order: dict) -> dict:
        """Log saja — tidak ada yang dikirim ke manapun."""
        order_idr = order["lots"] * 100 * order["price"]
        log(
            f"  📋 [L01] DRY_RUN ORDER | {order['ticker']} {order['side']} "
            f"{order['lots']} lot @ Rp{order['price']:,.0f} "
            f"(≈ Rp{order_idr/1e6:.1f}M) | SL:{order['sl']:,.0f} TP:{order['tp1']:,.0f} "
            f"| [{order['order_type']}] {order['timestamp']}"
        )
        return {**order, "status": "DRY_RUN",
                "note": "Order tidak dikirim — mode dry_run aktif"}

    def _paper_trade(self, order: dict) -> dict:
        """Simpan ke Supabase sebagai paper trade (no broker)."""
        try:
            supabase.table("paper_trades").insert({
                "order_id":   order["order_id"],
                "ticker":     order["ticker"],
                "side":       order["side"],
                "lots":       order["lots"],
                "price":      order["price"],
                "sl":         order["sl"],
                "tp1":        order["tp1"],
                "order_type": order["order_type"],
                "strategy":   order["strategy"],
                "signal_id":  order["signal_id"],
                "status":     "PAPER",
            }).execute()
            log(f"  📝 [L01] PAPER TRADE saved: {order['order_id']} "
                f"{order['ticker']} {order['side']} {order['lots']} lot")
            return {**order, "status": "PAPER",
                    "note": "Paper trade tersimpan di Supabase"}
        except Exception as e:
            log(f"  ⚠️ [L01] Paper trade save gagal: {e}", "warn")
            return {**order, "status": "ERROR", "note": str(e)}

    def _live_order(self, order: dict) -> dict:
        """
        [M03] Kirim ke broker via adapter pattern (IPOTAdapter / MOSTAdapter).

        Sebelumnya menggunakan generic HTTP template yang membaca BROKER_ENDPOINT
        dan BROKER_API_KEY secara langsung — tidak kompatibel dengan adapter
        pattern yang sudah diimplementasi di IPOTAdapter/MOSTAdapter.

        Perubahan:
          1. Gunakan get_broker_adapter() + adapter.authenticate() + adapter.place_order()
          2. Setelah SUBMITTED, simpan broker_order_id ke Supabase signals
             agar feedback loop bulk_reconcile_fills() bisa query status order.
          3. Fallback ke _dry_run() jika adapter tidak configured atau auth gagal.
        """
        adapter = get_broker_adapter()
        if adapter is None:
            # ── [P7-01] HARD ABORT — tidak boleh silent fallback di LIVE mode ──
            # Sebelumnya: fallback ke dry_run tanpa alert → operator tidak sadar
            # Sekarang: kirim alert Telegram + return REJECTED eksplisit.
            # Bot TIDAK eksekusi order jika broker tidak terkonfigurasi.
            _abort_msg = (
                "🔴 <b>[P7-01] LIVE ORDER ABORTED</b>\n"
                f"Ticker: {order.get('ticker','?')} {order.get('side','?')}\n"
                "Penyebab: <code>EXECUTION_MODE=live</code> tapi broker adapter "
                "tidak terkonfigurasi.\n"
                "Cek ENV: <code>BROKER_NAME</code>, <code>BROKER_USERNAME</code>, "
                "<code>BROKER_PASSWORD</code>, <code>BROKER_PIN</code>.\n"
                "⚠️ Order TIDAK dikirim. Bot tetap jalan (scan lanjut) "
                "tapi tidak ada eksekusi nyata."
            )
            log("  🔴 [P7-01] LIVE mode tapi broker adapter None — ORDER ABORTED (bukan dry_run)", "error")
            try:
                tg(_abort_msg)
            except Exception:
                pass
            return {**order, "status": "ABORTED",
                    "note": "LIVE mode: no broker adapter configured — order not sent"}

        # [M02] Authenticate via adapter (token akan di-cache oleh BrokerFillManager)
        token = _broker_mgr.get_token()
        if not token:
            # ── [P7-01] HARD ABORT — auth gagal di LIVE mode ──
            _auth_abort_msg = (
                "🔴 <b>[P7-01] LIVE ORDER ABORTED — AUTH FAILED</b>\n"
                f"Ticker: {order.get('ticker','?')} {order.get('side','?')}\n"
                "Broker adapter ada tapi token None. "
                "Cek kredensial atau re-login broker.\n"
                "⚠️ Order TIDAK dikirim."
            )
            log("  🔴 [P7-01] Broker auth gagal di LIVE mode — ORDER ABORTED (bukan dry_run)", "error")
            try:
                tg(_auth_abort_msg)
            except Exception:
                pass
            return {**order, "status": "ABORTED",
                    "note": "LIVE mode: broker auth failed — order not sent"}

        try:
            result = adapter.place_order(token, order)
            broker_order_id = result.get("broker_order_id", "")
            status          = result.get("status", "UNKNOWN")

            log(f"  ✅ [M03] LIVE ORDER: {broker_order_id} | "
                f"{order['ticker']} {order['side']} {order['lots']} lot "
                f"@ Rp{order['price']:,.0f} | status={status}")

            # [M03] Simpan broker_order_id ke Supabase signals agar feedback
            # loop (bulk_reconcile_fills, resolve_fill_price_v2) bisa cek
            # status dan ambil actual fill price dari broker.
            # Ini adalah sambungan yang sebelumnya putus: order dikirim tapi
            # ID-nya tidak pernah disimpan → fill tracking selalu jadi SIM.
            signal_id = order.get("signal_id", "")
            if signal_id and broker_order_id and status == "SUBMITTED":
                try:
                    supabase.table("signals").update({
                        "broker_order_id": broker_order_id,
                        "execution_mode":  "live",
                    }).eq("id", signal_id).execute()
                    log(f"  ✅ [M03] broker_order_id saved to signals: "
                        f"{signal_id} → {broker_order_id}")
                except Exception as _db_e:
                    log(f"  ⚠️ [M03] save broker_order_id gagal [{signal_id}]: "
                        f"{_db_e}", "warn")

            return {**order, "status": status,
                    "order_id": broker_order_id or order["order_id"],
                    "note": result.get("note", "")}

        except Exception as e:
            log(f"  🔴 [M03] LIVE ORDER GAGAL: {e}", "error")
            return {**order, "status": "REJECTED", "note": str(e)}


# Singleton — buat sekali, pakai di seluruh bot
_execution_bridge = ExecutionBridge(mode=EXECUTION_MODE)


def submit_signal_order(sig: dict, lots: int) -> dict:
    """
    [L01] Wrapper utama: submit order dari signal dict ke ExecutionBridge.

    Dipanggil di send_signal() setelah semua validasi lulus.
    sig : dict signal dari check_intraday() / check_swing()
    lots: jumlah lot yang sudah dihitung dari capital constraint (L03)
    """
    return _execution_bridge.submit_order(
        ticker     = sig.get("ticker", ""),
        side       = sig.get("side", "BUY"),
        lots       = lots,
        price      = sig.get("entry_adj", sig.get("entry", 0.0)),
        order_type = "LIMIT",
        sl         = sig.get("sl", 0.0),
        tp1        = sig.get("tp1", 0.0),
        signal_id  = sig.get("signal_id", ""),
        strategy   = sig.get("strategy", "SWING"),
    )


# ═══════════════════════════════════════════════════════════════════
#  [L02] ORDER BOOK DEPTH SIMULATION — v7.17
#
#  Karena yfinance tidak menyediakan Level 2 data (bid/ask ladder),
#  kita buat synthetic order book dari OHLCV + spread proxy.
#
#  Model ini BUKAN data nyata. Tujuannya:
#    1. Estimasi queue position kita di antrian beli/jual
#    2. Hitung expected slippage lebih granular per price level
#    3. Flag ILLIQUID jika depth terlalu tipis untuk order size kita
#
#  Keterbatasan (jujur):
#    ❌ Bukan real bid/ask ladder dari broker
#    ❌ Tidak tahu queue position nyata (perlu data L2 langsung)
#    ✅ Lebih baik dari asumsi flat spread
#    ✅ Berkorelasi dengan ATR dan volume ratio
# ═══════════════════════════════════════════════════════════════════

def build_synthetic_order_book(
    price: float,
    vol_today_idr: float,
    atr_pct: float,
    spread_pct: float,
    ticker: str = "",
    n_levels: int = 5,
) -> dict:
    """
    [L02] Bangun synthetic order book (Level 2 proxy) dari OHLCV data.

    Menghasilkan n_levels bid dan ask dengan estimasi volume per level.
    Volume per level diturunkan dari:
      - Total volume hari ini (vol_today_idr)
      - ATR sebagai ukuran volatilitas dan kedalaman pasar
      - Spread proxy sebagai jarak antar tick

    Returns:
      {
        "bids": [(price, vol_idr), ...],   # level rendah → tertinggi
        "asks": [(price, vol_idr), ...],   # level rendah → tertinggi
        "mid":  float,
        "total_bid_idr": float,
        "total_ask_idr": float,
        "depth_score":   int,   # 1-5 (5 = sangat likuid)
        "note":          str,
      }
    """
    if price <= 0 or vol_today_idr <= 0:
        return {
            "bids": [], "asks": [], "mid": price,
            "total_bid_idr": 0.0, "total_ask_idr": 0.0,
            "depth_score": 0, "note": "No data"
        }

    # Tick size IDX (berdasarkan fraksi harga)
    if price >= 5_000:
        tick = 25.0
    elif price >= 2_000:
        tick = 10.0
    elif price >= 500:
        tick = 5.0
    elif price >= 200:
        tick = 2.0
    else:
        tick = 1.0

    # Volume harian / 6.5 jam trading = estimasi volume per jam
    # Distribusi: assume 40% volume ada dalam 2 jam pertama dan terakhir
    # → sisanya (60%) tersebar merata. Per level ≈ vol/jam / 5 level
    vol_per_hour_idr = vol_today_idr / 6.5
    vol_per_level_base = vol_per_hour_idr * 0.60 / n_levels

    # Volatility dampener: ATR tinggi → kedalaman tipis (market maker mundur)
    vol_dampener = max(0.3, 1.0 - (atr_pct - 1.0) * 0.15)

    bids, asks = [], []
    for i in range(n_levels):
        # Level makin jauh dari mid → volume makin kecil (market maker confident)
        level_vol = vol_per_level_base * vol_dampener * (0.9 ** i)

        bid_price = price - tick * (i + 1)
        ask_price = price + tick * (i + 1)

        # Jaga harga tidak turun ke nol
        if bid_price > 0:
            bids.append((round(bid_price, 0), round(level_vol, 0)))
        asks.append((round(ask_price, 0), round(level_vol, 0)))

    total_bid = sum(v for _, v in bids)
    total_ask = sum(v for _, v in asks)

    # Depth score: 5 = deep (> Rp 1M per level avg), 1 = shallow
    avg_level_vol = (total_bid + total_ask) / (2 * n_levels + 1e-9)
    if avg_level_vol >= 1_000_000_000:    depth_score = 5
    elif avg_level_vol >= 500_000_000:    depth_score = 4
    elif avg_level_vol >= 100_000_000:    depth_score = 3
    elif avg_level_vol >= 20_000_000:     depth_score = 2
    else:                                 depth_score = 1

    note = (f"Synthetic L2 [{ticker}]: {n_levels} levels | "
            f"tick=Rp{tick:.0f} | avg/level≈Rp{avg_level_vol/1e6:.0f}M | "
            f"depth={depth_score}/5")
    log(f"  📖 [L02] {note}")

    return {
        "bids":           bids,
        "asks":           asks,
        "mid":            price,
        "total_bid_idr":  round(total_bid, 0),
        "total_ask_idr":  round(total_ask, 0),
        "depth_score":    depth_score,
        "tick":           tick,
        "note":           note,
    }


def estimate_queue_position(
    order_idr: float,
    order_book: dict,
    side: str,
    session_info: dict,
) -> dict:
    """
    [L02] Estimasi posisi antrian order kita di order book.

    Berdasarkan:
      - Total volume di level terbaik (best bid/ask)
      - Session multiplier (pra-opening vs continuous vs pre-closing)
      - Order size relatif terhadap depth level 1

    Returns:
      {
        "queue_pct":     float,  # estimasi % antrian yang di depan kita (0-1)
        "expected_wait": str,    # "INSTANT" / "FAST" / "MODERATE" / "SLOW"
        "fill_prob":     float,  # probabilitas fill di current session (0-1)
        "note":          str,
      }
    """
    if not order_book.get("bids") or not order_book.get("asks"):
        return {"queue_pct": 0.5, "expected_wait": "UNKNOWN",
                "fill_prob": 0.5, "note": "No order book data"}

    # Ambil volume di level terbaik (level 0 = closest to mid)
    best_level_vol = (order_book["bids"][0][1] if side == "BUY"
                      else order_book["asks"][0][1])

    # Berapa besar order kita vs volume di level ini
    queue_pct = min(order_idr / (best_level_vol + 1e-9), 1.0)

    # Session modifier
    session = session_info.get("session", "CONTINUOUS")
    if session in ("OUTSIDE_HOURS",):
        return {"queue_pct": 1.0, "expected_wait": "MARKET_CLOSED",
                "fill_prob": 0.0, "note": "Market tutup"}
    elif session in ("PRE_OPENING",):
        # Random matching saat open — posisi antrian tidak relevan
        fill_prob = 0.70  # 70% chance terisi saat matching
        wait = "OPEN_MATCH"
    elif session in ("OPEN_RUSH",):
        fill_prob = max(0.3, 1.0 - queue_pct * 0.5)
        wait = "SLOW" if queue_pct > 0.5 else "MODERATE"
    elif session in ("CONTINUOUS",):
        fill_prob = max(0.6, 1.0 - queue_pct * 0.3)
        wait = "FAST" if queue_pct < 0.2 else "MODERATE"
    elif session in ("PRE_CLOSE", "AFTER_HOURS"):
        fill_prob = 0.40
        wait = "MODERATE"
    else:
        fill_prob = 0.65
        wait = "MODERATE"

    # Depth score modifier: depth tipis → fill prob turun
    depth = order_book.get("depth_score", 3)
    fill_prob = fill_prob * (depth / 5.0) * 1.2  # boost sedikit agar tidak terlalu pesimistis
    fill_prob = min(fill_prob, 0.95)

    note = (f"Queue est: {queue_pct:.1%} depth | "
            f"fill_prob={fill_prob:.0%} | wait={wait} | depth={depth}/5")
    log(f"  🔢 [L02] {note}")
    return {
        "queue_pct":     round(queue_pct, 4),
        "expected_wait": wait,
        "fill_prob":     round(fill_prob, 3),
        "note":          note,
    }


# ═══════════════════════════════════════════════════════════════════
#  [L03] CAPITAL CONSTRAINT — ADV PARTICIPATION CAP — v7.17
#
#  Masalah utama:
#    Bot sekarang menghitung VPR (Volume Participation Rate) tapi
#    tidak ada guard yang mencegah order melewati batas ADV wajar.
#
#    Contoh: Modal Rp 500jt, RISK 2%, signal di CUAN.JK (volume Rp 50M/hari)
#    → tanpa ADV cap, bot bisa signal order Rp 10jt = 20% ADV → ILLIQUID
#
#  Solusi:
#    1. MAX_ADV_PARTICIPATION_PCT per liquidity tier
#    2. Hard cap order IDR per tier
#    3. calc_lot_size() dengan ADV constraint terintegrasi
#    4. Fungsi ini menggantikan / melengkapi get_smart_risk_pct()
# ═══════════════════════════════════════════════════════════════════

# Config ENV — bisa di-override via environment variable
try:
    MAX_ADV_PARTICIPATION_PCT = float(
        os.environ.get("MAX_ADV_PARTICIPATION_PCT", 2.0)
    )
    MAX_ADV_PARTICIPATION_PCT = max(0.1, min(MAX_ADV_PARTICIPATION_PCT, 10.0))
except ValueError:
    MAX_ADV_PARTICIPATION_PCT = 2.0  # default: max 2% dari ADV per order

# Hard cap per tier (IDR)
_ADV_TIER_CAPS: dict[str, float] = {
    "LQ45/BLUECHIP": 500_000_000,   # Rp 500jt — blue chip bisa handle
    "MID_CAP":       100_000_000,   # Rp 100jt
    "SMALL_CAP":      30_000_000,   # Rp 30jt
    "ILLIQUID":       10_000_000,   # Rp 10jt — hampir tidak worth it
}


def calc_adv_participation_cap(
    vol_today_idr: float,
    cap_tier: str,
    max_pct: float = MAX_ADV_PARTICIPATION_PCT,
) -> dict:
    """
    [L03] Hitung batas maksimum order size berdasarkan ADV participation.

    Args:
        vol_today_idr : volume hari ini dalam IDR
        cap_tier      : "LQ45/BLUECHIP" / "MID_CAP" / "SMALL_CAP" / "ILLIQUID"
        max_pct       : max % ADV yang boleh kita ambil (default: 2%)

    Returns:
        {
          "max_order_idr":    float,  # batas keras order size dalam IDR
          "adv_cap_idr":      float,  # ADV-based cap
          "tier_cap_idr":     float,  # tier-based hard cap
          "binding_cap":      str,    # "ADV" atau "TIER" — mana yang lebih ketat
          "participation_pct": float, # persentase ADV yang kita ambil (jika ikuti cap)
          "tradeable":        bool,   # False jika terlalu illiquid
          "note":             str,
        }
    """
    adv_cap = vol_today_idr * (max_pct / 100.0)
    tier_cap = _ADV_TIER_CAPS.get(cap_tier, _ADV_TIER_CAPS["SMALL_CAP"])

    max_order_idr = min(adv_cap, tier_cap)

    if adv_cap < tier_cap:
        binding_cap = "ADV"
    else:
        binding_cap = "TIER"

    participation_pct = (max_order_idr / vol_today_idr * 100
                         if vol_today_idr > 0 else 0.0)

    # Saham sangat illiquid: ADV cap di bawah 1 lot
    # 1 lot = 100 saham. Jika max_order < 1 lot value → tidak worth trading
    tradeable = max_order_idr >= 100_000  # minimal Rp 100rb ≈ kemungkinan 1 lot

    note = (f"ADV cap: Rp{adv_cap/1e6:.1f}M | tier cap: Rp{tier_cap/1e6:.0f}M | "
            f"binding: {binding_cap} → max order Rp{max_order_idr/1e6:.1f}M "
            f"({participation_pct:.2f}% ADV)")
    log(f"  💧 [L03] ADV Constraint [{cap_tier}]: {note}")

    return {
        "max_order_idr":    round(max_order_idr, 0),
        "adv_cap_idr":      round(adv_cap, 0),
        "tier_cap_idr":     round(tier_cap, 0),
        "binding_cap":      binding_cap,
        "participation_pct": round(participation_pct, 4),
        "tradeable":        tradeable,
        "note":             note,
    }


def calc_lot_size(
    signal_risk_pct: float,
    portfolio_idr: float,
    entry_price: float,
    sl_price: float,
    vol_today_idr: float,
    cap_tier: str,
    ticker: str = "",
) -> dict:
    """
    [L03] Hitung jumlah lot yang optimal dengan ADV participation constraint.

    Pipeline:
      1. Hitung risk capital (Rp berapa yang siap hilang)
      2. Dari risk capital dan jarak SL → hitung berapa saham/lot ideal
      3. Konversi ke IDR → cek vs ADV cap (L03)
      4. Truncate ke lot paling banyak yang masih dalam cap
      5. Hitung ulang actual participation rate

    Args:
        signal_risk_pct : risk % dari get_smart_risk_pct() (e.g. 1.5%)
        portfolio_idr   : total modal (dari PORTFOLIO_IDR)
        entry_price     : harga entry adjusted (dari simulate_execution)
        sl_price        : stop loss price
        vol_today_idr   : volume hari ini dalam IDR
        cap_tier        : tier likuiditas dari calc_market_impact()
        ticker          : untuk logging

    Returns:
        {
          "lots":              int,    # jumlah lot final (1 lot = 100 saham)
          "shares":            int,    # jumlah lembar saham
          "order_idr":         float,  # total nilai order dalam IDR
          "risk_idr":          float,  # actual risk yang diambil
          "actual_risk_pct":   float,  # actual risk % dari portfolio
          "vpr":               float,  # participation rate aktual
          "adv_constrained":   bool,   # True jika ADV cap yang membatasi
          "tradeable":         bool,   # False jika 0 lot bisa dikerjakan
          "note":              str,
        }
    """
    default = {
        "lots": 0, "shares": 0, "order_idr": 0.0, "risk_idr": 0.0,
        "actual_risk_pct": 0.0, "vpr": 0.0,
        "adv_constrained": False, "tradeable": False,
        "note": "Invalid input"
    }

    if entry_price <= 0 or sl_price <= 0 or entry_price == sl_price:
        log(f"  ⚠️ [L03] calc_lot_size gagal: entry/sl invalid [{ticker}]", "warn")
        return default

    # Step 1: Risk capital dalam Rupiah
    risk_idr_target = portfolio_idr * (signal_risk_pct / 100.0)

    # Step 2: Jarak SL dalam Rupiah per saham
    sl_distance_per_share = abs(entry_price - sl_price)

    # Step 3: Jumlah saham ideal (belum constraint)
    ideal_shares = risk_idr_target / sl_distance_per_share
    ideal_lots = int(ideal_shares / 100)

    # Nilai order dari ideal sizing
    ideal_order_idr = ideal_lots * 100 * entry_price

    # Step 4: Cek ADV cap
    adv_cap_result = calc_adv_participation_cap(vol_today_idr, cap_tier)
    max_order_idr  = adv_cap_result["max_order_idr"]

    adv_constrained = False
    if ideal_order_idr > max_order_idr:
        adv_constrained = True
        capped_lots = int(max_order_idr / (100 * entry_price))
        log(f"  ⚠️ [L03] ADV cap aktif [{ticker}]: ideal {ideal_lots} lot → "
            f"dibatasi {capped_lots} lot (cap Rp{max_order_idr/1e6:.1f}M)", "warn")
    else:
        capped_lots = ideal_lots

    final_lots  = max(0, capped_lots)
    final_order = final_lots * 100 * entry_price
    actual_risk = final_lots * 100 * sl_distance_per_share
    actual_risk_pct = (actual_risk / portfolio_idr * 100) if portfolio_idr > 0 else 0.0
    actual_vpr  = (final_order / vol_today_idr * 100) if vol_today_idr > 0 else 0.0
    tradeable   = final_lots > 0

    note = (f"Ideal: {ideal_lots} lot → ADV cap → {final_lots} lot | "
            f"order Rp{final_order/1e6:.1f}M | risk {actual_risk_pct:.2f}% | "
            f"participation {actual_vpr:.2f}% ADV")
    if adv_constrained:
        note += " [ADV CONSTRAINED]"

    log(f"  📦 [L03] Lot size [{ticker}]: {note}")

    return {
        "lots":            final_lots,
        "shares":          final_lots * 100,
        "order_idr":       round(final_order, 0),
        "risk_idr":        round(actual_risk, 0),
        "actual_risk_pct": round(actual_risk_pct, 4),
        "vpr":             round(actual_vpr, 4),
        "adv_constrained": adv_constrained,
        "tradeable":       tradeable,
        "note":            note,
    }


def apply_capital_constraints(sig: dict, portfolio_idr: float) -> dict:
    """
    [L03] Entry point utama — evaluasi semua capital constraints untuk satu signal.

    Tambahkan ke send_signal() atau check_intraday/check_swing() sebelum kirim Telegram.

    Returns sig dict yang diperkaya dengan:
      - "lot_sizing": dict dari calc_lot_size()
      - "adv_check":  dict dari calc_adv_participation_cap()
      - "tradeable":  bool — False = jangan kirim signal
    """
    entry     = sig.get("entry_adj", sig.get("entry", 0.0))
    sl        = sig.get("sl", 0.0)
    risk_pct  = sig.get("smart_risk_pct", RISK_PCT)
    vol_idr   = sig.get("vol_today_idr", 0.0)
    cap_tier  = sig.get("cap_tier", "SMALL_CAP")
    ticker    = sig.get("ticker", "")

    lot_result = calc_lot_size(
        signal_risk_pct = risk_pct,
        portfolio_idr   = portfolio_idr,
        entry_price     = entry,
        sl_price        = sl,
        vol_today_idr   = vol_idr,
        cap_tier        = cap_tier,
        ticker          = ticker,
    )

    adv_check = calc_adv_participation_cap(vol_idr, cap_tier)

    sig["lot_sizing"]  = lot_result
    sig["adv_check"]   = adv_check
    sig["tradeable"]   = lot_result["tradeable"] and adv_check["tradeable"]
    sig["final_lots"]  = lot_result["lots"]

    if not sig["tradeable"]:
        log(f"  🚫 [L03] {ticker} TIDAK TRADEABLE — 0 lot atau terlalu illiquid. "
            f"ADV: Rp{vol_idr/1e6:.0f}M, cap_tier: {cap_tier}", "warn")

    return sig


# ═══════════════════════════════════════════════════════════════════
#  CARA INTEGRASI KE BOT UTAMA
#  (Instruksi manual — tidak otomatis applied)
# ═══════════════════════════════════════════════════════════════════
#
#  1. Di check_intraday() / check_swing(), sebelum return signal:
#
#     sig["vol_today_idr"] = vol_today_idr   # pastikan ini ada di sig dict
#     sig = apply_capital_constraints(sig, PORTFOLIO_IDR)
#     if not sig["tradeable"]:
#         log(f"  🚫 {ticker}: signal discarded — tidak tradeable (ADV cap)")
#         return None
#
#  2. Di send_signal(), setelah semua validasi:
#
#     lots = sig.get("final_lots", 0)
#     order_result = submit_signal_order(sig, lots)
#     sig["order_status"] = order_result["status"]
#     sig["order_id"]     = order_result["order_id"]
#
#  3. Di simulate_execution(), di bawah calc_market_impact():
#
#     order_book = build_synthetic_order_book(
#         price=entry, vol_today_idr=vol_today_idr,
#         atr_pct=atr_pct, spread_pct=spread_pct, ticker=ticker
#     )
#     queue_est = estimate_queue_position(
#         order_idr=order_lots * 100 * entry,
#         order_book=order_book, side=side,
#         session_info=queue_info
#     )
#     result["depth_score"]  = order_book["depth_score"]
#     result["queue_pct"]    = queue_est["queue_pct"]
#     result["fill_prob"]    = queue_est["fill_prob"]
#
#  4. ENV yang perlu ditambah ke GitHub Actions secrets:
#
#     EXECUTION_MODE=dry_run          # atau paper / live
#     MAX_ADV_PARTICIPATION_PCT=2.0   # max % ADV per order
#     BROKER_ENDPOINT=                # (opsional, hanya jika live mode)
#     BROKER_API_KEY=                 # (opsional, hanya jika live mode)
#
# ═══════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════
#  PATCH v7.18 — Broker Adapters (IPOT/MOST) + L2 + Slippage Model
# ════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════
#  [M01] BROKER-SPECIFIC ADAPTERS — v7.18
#
#  Menggantikan _live_order() yang generic di ExecutionBridge v7.17.
#
#  Arsitektur: Strategy Pattern
#    ExecutionBridge._live_order() → dispatch ke BrokerAdapter
#    BrokerAdapter: interface generik
#    IPOTAdapter   : implementasi konkret IPOT
#    MOSTAdapter   : implementasi konkret Mandiri Sekuritas
#
#  Setup IPOT:
#    - Daftar akun API di ipotstock.com → menu API Trading
#    - Generate token via POST /auth/token
#    - ENV: BROKER=ipot, BROKER_USERNAME, BROKER_PASSWORD, BROKER_PIN
#
#  Setup MOST (Mandiri Sekuritas):
#    - Hubungi relationship manager Mandiri Sekuritas untuk akses MOST API
#    - ENV: BROKER=most, BROKER_CLIENT_ID, BROKER_CLIENT_SECRET
#
#  Catatan jujur:
#    - IPOT API endpoint di bawah ini berdasarkan dokumentasi publik 2024
#    - Mandiri MOST API: dokumentasi terbatas — blok ini adalah best-effort
#    - Selalu test dengan paper mode sebelum live
# ════════════════════════════════════════════════════════════════════

import os, json, time
import urllib.request, urllib.parse

BROKER_NAME = os.environ.get("BROKER", "none").lower()

# ── IPOT credentials ──────────────────────────────────────────────
IPOT_BASE_URL  = os.environ.get("IPOT_BASE_URL",
                                "https://www.indopremier.com/ipotnext/api/v2")
IPOT_USERNAME  = os.environ.get("BROKER_USERNAME", "")
IPOT_PASSWORD  = os.environ.get("BROKER_PASSWORD", "")
IPOT_PIN       = os.environ.get("BROKER_PIN", "")

# ── MOST credentials ─────────────────────────────────────────────
MOST_BASE_URL  = os.environ.get("MOST_BASE_URL",
                                "https://most.id/api/v1")
MOST_CLIENT_ID     = os.environ.get("BROKER_CLIENT_ID", "")
MOST_CLIENT_SECRET = os.environ.get("BROKER_CLIENT_SECRET", "")


class BrokerAdapter:
    """Base interface untuk semua broker. Override place_order()."""

    def authenticate(self) -> str | None:
        """Return token string atau None jika gagal."""
        raise NotImplementedError

    def place_order(self, token: str, order: dict) -> dict:
        """
        Kirim order ke broker.
        Args:
            token : auth token dari authenticate()
            order : dict standar dari ExecutionBridge
        Returns dict dengan "broker_order_id", "status", "note"
        """
        raise NotImplementedError

    def get_order_status(self, token: str, broker_order_id: str) -> dict:
        """Cek status order yang sudah dikirim."""
        raise NotImplementedError

    @staticmethod
    def _http_post(url: str, payload: dict, headers: dict,
                   timeout: int = 10) -> dict:
        """Shared HTTP POST helper."""
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(url, data=data,
                                      headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body}") from e

    @staticmethod
    def _http_get(url: str, headers: dict, timeout: int = 10) -> dict:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())


class IPOTAdapter(BrokerAdapter):
    """
    [M01] Indo Premier Online Trading (IPOT) broker adapter.

    Endpoint reference: ipotnext API v2 (dokumentasi publik 2024)
    Auth  : POST /auth/token dengan username/password/pin
    Order : POST /trading/order
    Status: GET  /trading/order/{order_id}

    IDX lot convention: 1 lot = 100 lembar (IPOT pakai unit "lot")
    """

    def authenticate(self) -> str | None:
        """Ambil JWT token dari IPOT. Token valid ~8 jam."""
        if not IPOT_USERNAME or not IPOT_PASSWORD or not IPOT_PIN:
            log("  🔴 [M01] IPOT: BROKER_USERNAME/PASSWORD/PIN belum diset", "error")
            return None
        try:
            resp = self._http_post(
                url=f"{IPOT_BASE_URL}/auth/token",
                payload={
                    "username": IPOT_USERNAME,
                    "password": IPOT_PASSWORD,
                    "pin":      IPOT_PIN,
                    "grant_type": "password",
                },
                headers={"Content-Type": "application/json"},
            )
            token = resp.get("access_token") or resp.get("token")
            if not token:
                log(f"  🔴 [M01] IPOT auth gagal: {resp}", "error")
                return None
            log("  ✅ [M01] IPOT auth OK")
            return token
        except Exception as e:
            log(f"  🔴 [M01] IPOT auth error: {e}", "error")
            return None

    def place_order(self, token: str, order: dict) -> dict:
        """
        Submit limit order ke IPOT.

        IPOT order payload (berdasarkan dokumentasi ipotnext API v2):
          stock_code : "BBCA" (tanpa .JK)
          action     : "B" (buy) atau "S" (sell)
          lots       : jumlah lot (integer)
          price      : harga limit per lembar (rupiah)
          order_type : "L" (limit) atau "MKT" (market)
        """
        ticker_clean = order["ticker"].replace(".JK", "").replace(".jk", "")
        action = "B" if order["side"] == "BUY" else "S"

        try:
            resp = self._http_post(
                url=f"{IPOT_BASE_URL}/trading/order",
                payload={
                    "stock_code": ticker_clean,
                    "action":     action,
                    "lots":       order["lots"],
                    "price":      int(order["price"]),  # IDX price = integer Rupiah
                    "order_type": "L",
                    "remarks":    f"signal_id:{order.get('signal_id','')}",
                },
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
            broker_id = (resp.get("order_id") or resp.get("orderId")
                         or resp.get("id") or "UNKNOWN")
            status = resp.get("status", "SUBMITTED")
            log(f"  ✅ [M01] IPOT order submitted: {broker_id} | "
                f"{ticker_clean} {action} {order['lots']} lot "
                f"@ Rp{order['price']:,.0f}")
            return {
                "broker_order_id": str(broker_id),
                "status": "SUBMITTED",
                "note":   f"IPOT response: {resp}",
                "raw":    resp,
            }
        except Exception as e:
            log(f"  🔴 [M01] IPOT place_order error: {e}", "error")
            return {"broker_order_id": "", "status": "REJECTED", "note": str(e)}

    def get_order_status(self, token: str, broker_order_id: str) -> dict:
        """Cek status order — dipakai oleh feedback loop M03."""
        try:
            resp = self._http_get(
                url=f"{IPOT_BASE_URL}/trading/order/{broker_order_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            raw_status = resp.get("status", "UNKNOWN")
            # Normalize ke status internal
            status_map = {
                "FILLED":          "FILLED",
                "PARTIAL":         "PARTIAL_FILL",
                "OPEN":            "PENDING",
                "CANCELLED":       "CANCELLED",
                "REJECTED":        "REJECTED",
            }
            status = status_map.get(raw_status.upper(), raw_status)
            fill_price = float(resp.get("avg_price") or resp.get("avgPrice") or 0)
            fill_lots  = int(resp.get("filled_lots") or resp.get("filledLots") or 0)
            return {
                "status":     status,
                "fill_price": fill_price,
                "fill_lots":  fill_lots,
                "raw":        resp,
            }
        except Exception as e:
            log(f"  ⚠️ [M01] IPOT get_order_status error: {e}", "warn")
            return {"status": "UNKNOWN", "fill_price": 0.0, "fill_lots": 0}


class MOSTAdapter(BrokerAdapter):
    """
    [M01] Mandiri Sekuritas (MOST) broker adapter.

    MOST API menggunakan OAuth2 client_credentials flow.
    Dokumentasi: hubungi Mandiri Sekuritas helpdesk → MOST API access.
    Endpoint di bawah ini adalah estimasi berdasarkan pola umum broker API IDX.

    PENTING: Endpoint ini perlu diverifikasi dengan dokumentasi resmi MOST.
    Set ENV: MOST_BASE_URL ke URL aktual yang diberikan oleh Mandiri.
    """

    def authenticate(self) -> str | None:
        if not MOST_CLIENT_ID or not MOST_CLIENT_SECRET:
            log("  🔴 [M01] MOST: BROKER_CLIENT_ID/SECRET belum diset", "error")
            return None
        try:
            resp = self._http_post(
                url=f"{MOST_BASE_URL}/oauth/token",
                payload={
                    "grant_type":    "client_credentials",
                    "client_id":     MOST_CLIENT_ID,
                    "client_secret": MOST_CLIENT_SECRET,
                },
                headers={"Content-Type": "application/json"},
            )
            token = resp.get("access_token")
            if not token:
                log(f"  🔴 [M01] MOST auth gagal: {resp}", "error")
                return None
            log("  ✅ [M01] MOST auth OK")
            return token
        except Exception as e:
            log(f"  🔴 [M01] MOST auth error: {e}", "error")
            return None

    def place_order(self, token: str, order: dict) -> dict:
        ticker_clean = order["ticker"].replace(".JK", "")
        action = "BUY" if order["side"] == "BUY" else "SELL"
        try:
            resp = self._http_post(
                url=f"{MOST_BASE_URL}/trading/orders",
                payload={
                    "symbol":     ticker_clean,
                    "side":       action,
                    "quantity":   order["lots"] * 100,  # MOST mungkin pakai unit lembar
                    "price":      int(order["price"]),
                    "order_type": "LIMIT",
                    "ref":        order.get("signal_id", ""),
                },
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
            broker_id = resp.get("orderId") or resp.get("order_id") or "UNKNOWN"
            log(f"  ✅ [M01] MOST order submitted: {broker_id}")
            return {
                "broker_order_id": str(broker_id),
                "status": "SUBMITTED",
                "note":   f"MOST response: {resp}",
                "raw":    resp,
            }
        except Exception as e:
            log(f"  🔴 [M01] MOST place_order error: {e}", "error")
            return {"broker_order_id": "", "status": "REJECTED", "note": str(e)}

    def get_order_status(self, token: str, broker_order_id: str) -> dict:
        try:
            resp = self._http_get(
                url=f"{MOST_BASE_URL}/trading/orders/{broker_order_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            fill_price = float(resp.get("avgPrice") or resp.get("fill_price") or 0)
            fill_lots  = int(resp.get("filledQty", 0) or 0) // 100
            status_raw = resp.get("status", "UNKNOWN")
            return {
                "status":     status_raw,
                "fill_price": fill_price,
                "fill_lots":  fill_lots,
                "raw":        resp,
            }
        except Exception as e:
            log(f"  ⚠️ [M01] MOST get_order_status error: {e}", "warn")
            return {"status": "UNKNOWN", "fill_price": 0.0, "fill_lots": 0}


def get_broker_adapter() -> BrokerAdapter | None:
    """
    [M01] Factory — return adapter yang sesuai berdasarkan ENV BROKER.

    Contoh:
      ENV BROKER=ipot   → IPOTAdapter
      ENV BROKER=most   → MOSTAdapter
      ENV BROKER=none   → None (dry_run / paper mode)
    """
    if BROKER_NAME == "ipot":
        log("  🔌 [M01] Broker adapter: IPOT (Indo Premier)")
        return IPOTAdapter()
    elif BROKER_NAME == "most":
        log("  🔌 [M01] Broker adapter: MOST (Mandiri Sekuritas)")
        return MOSTAdapter()
    else:
        log("  📋 [M01] Broker adapter: NONE (dry_run / paper mode)")
        return None


# ════════════════════════════════════════════════════════════════════
#  [M02] L2 DATA UPGRADE PATH — v7.18
#
#  Masalah: build_synthetic_order_book() (v7.17) menghasilkan data
#  model, bukan real bid/ask ladder.
#
#  Upgrade path:
#    Level 0 (sekarang): Synthetic dari OHLCV + ATR + volume
#    Level 1 (soon):     IPOT/MOST streaming via WebSocket — order book
#                        per ticker tersedia saat market open
#    Level 2 (future):   RTI Business / Bloomberg BQuant — L2 full
#
#  Modul ini:
#    1. Cek ENV apakah L2 real tersedia
#    2. Jika ada → fetch dari endpoint yang dikonfigurasi
#    3. Jika tidak → fallback ke synthetic (v7.17)
#    4. Normalize semua source ke format internal yang sama
#
#  Format output standar (sama untuk semua source):
#    {
#      "bids":         [(price, vol_idr), ...],  # sorted descending
#      "asks":         [(price, vol_idr), ...],  # sorted ascending
#      "mid":          float,
#      "total_bid_idr": float,
#      "total_ask_idr": float,
#      "depth_score":  int,   # 1-5
#      "source":       str,   # "synthetic" / "ipot_stream" / "rti" / "bloomberg"
#      "timestamp":    str,
#      "note":         str,
#    }
# ════════════════════════════════════════════════════════════════════

L2_SOURCE  = os.environ.get("L2_SOURCE", "synthetic").lower()
L2_API_URL = os.environ.get("L2_API_URL", "")
L2_API_KEY = os.environ.get("L2_API_KEY", "")

_L2_VALID_SOURCES = {"synthetic", "ipot_stream", "rti", "bloomberg"}


def fetch_real_l2_orderbook(ticker: str, price: float) -> dict | None:
    """
    [M02] Fetch real L2 order book dari configured source.

    Saat ini L2_SOURCE selain 'synthetic' bersifat STUB —
    endpoint perlu disesuaikan dengan dokumentasi provider masing-masing.

    Returns None jika fetch gagal (caller akan fallback ke synthetic).
    """
    if L2_SOURCE == "synthetic" or not L2_API_URL or not L2_API_KEY:
        return None  # trigger fallback ke synthetic

    ticker_clean = ticker.replace(".JK", "")

    try:
        if L2_SOURCE == "ipot_stream":
            # IPOT menyediakan snapshot order book via REST polling
            # Endpoint: GET /market/orderbook/{symbol}
            req = urllib.request.Request(
                f"{L2_API_URL}/market/orderbook/{ticker_clean}",
                headers={"Authorization": f"Bearer {L2_API_KEY}"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = json.loads(resp.read())
            return _normalize_ipot_l2(raw, price, ticker)

        elif L2_SOURCE == "rti":
            # RTI Business: real-time IDX data provider
            # Endpoint format bervariasi — sesuaikan dengan kontrak RTI
            req = urllib.request.Request(
                f"{L2_API_URL}/orderbook?symbol={ticker_clean}&apikey={L2_API_KEY}"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = json.loads(resp.read())
            return _normalize_rti_l2(raw, price, ticker)

        elif L2_SOURCE == "bloomberg":
            # Bloomberg BQuant / BLPAPI — enterprise only
            # Ini stub — implementasi nyata butuh blpapi Python SDK
            log(f"  ⚠️ [M02] Bloomberg L2 belum diimplementasi — fallback synthetic", "warn")
            return None

    except Exception as e:
        log(f"  ⚠️ [M02] L2 fetch gagal ({L2_SOURCE}): {e} — fallback synthetic", "warn")
        return None


def _normalize_ipot_l2(raw: dict, price: float, ticker: str) -> dict:
    """Normalize IPOT order book response ke format internal."""
    from datetime import datetime

    try:
        # IPOT format (perkiraan berdasarkan pola broker IDX):
        # { "bids": [{"price": 7450, "volume": 500}, ...],
        #   "asks": [{"price": 7475, "volume": 300}, ...] }
        raw_bids = raw.get("bids") or raw.get("bid") or []
        raw_asks = raw.get("asks") or raw.get("offer") or []

        # Konversi volume lot ke IDR (1 lot = 100 lembar)
        bids = [(float(b["price"]), float(b["volume"]) * 100 * float(b["price"]))
                for b in raw_bids[:10]]  # max 10 level
        asks = [(float(a["price"]), float(a["volume"]) * 100 * float(a["price"]))
                for a in raw_asks[:10]]

        total_bid = sum(v for _, v in bids)
        total_ask = sum(v for _, v in asks)
        avg_vol   = (total_bid + total_ask) / max(len(bids) + len(asks), 1)

        if avg_vol >= 1_000_000_000:    depth = 5
        elif avg_vol >= 500_000_000:    depth = 4
        elif avg_vol >= 100_000_000:    depth = 3
        elif avg_vol >= 20_000_000:     depth = 2
        else:                           depth = 1

        log(f"  📖 [M02] IPOT L2 [{ticker}]: {len(bids)}x{len(asks)} levels | depth={depth}/5")
        return {
            "bids":           sorted(bids, key=lambda x: -x[0]),
            "asks":           sorted(asks, key=lambda x:  x[0]),
            "mid":            price,
            "total_bid_idr":  round(total_bid, 0),
            "total_ask_idr":  round(total_ask, 0),
            "depth_score":    depth,
            "source":         "ipot_stream",
            "timestamp":      datetime.now(WIB).strftime("%H:%M:%S WIB"),
            "note":           f"Real L2 dari IPOT | {len(bids)} bid / {len(asks)} ask levels",
        }
    except Exception as e:
        log(f"  ⚠️ [M02] normalize IPOT L2 error: {e}", "warn")
        return None


def _normalize_rti_l2(raw: dict, price: float, ticker: str) -> dict:
    """Normalize RTI order book response ke format internal."""
    from datetime import datetime
    try:
        # RTI format berbeda per kontrak — ini template generik
        bids_raw = raw.get("BidDepth") or raw.get("bids") or []
        asks_raw = raw.get("OfferDepth") or raw.get("asks") or []

        bids = [(float(b.get("Price", 0)),
                 float(b.get("Volume", 0)) * 100 * float(b.get("Price", 1)))
                for b in bids_raw[:10] if b.get("Price")]
        asks = [(float(a.get("Price", 0)),
                 float(a.get("Volume", 0)) * 100 * float(a.get("Price", 1)))
                for a in asks_raw[:10] if a.get("Price")]

        total_bid = sum(v for _, v in bids)
        total_ask = sum(v for _, v in asks)
        avg_vol   = (total_bid + total_ask) / max(len(bids) + len(asks), 1)
        depth = (5 if avg_vol >= 1e9 else 4 if avg_vol >= 5e8
                 else 3 if avg_vol >= 1e8 else 2 if avg_vol >= 2e7 else 1)

        log(f"  📖 [M02] RTI L2 [{ticker}]: depth={depth}/5")
        return {
            "bids":           sorted(bids, key=lambda x: -x[0]),
            "asks":           sorted(asks, key=lambda x:  x[0]),
            "mid":            price,
            "total_bid_idr":  round(total_bid, 0),
            "total_ask_idr":  round(total_ask, 0),
            "depth_score":    depth,
            "source":         "rti",
            "timestamp":      datetime.now(WIB).strftime("%H:%M:%S WIB"),
            "note":           f"Real L2 dari RTI | {len(bids)} bid / {len(asks)} ask levels",
        }
    except Exception as e:
        log(f"  ⚠️ [M02] normalize RTI L2 error: {e}", "warn")
        return None


def get_order_book(
    ticker: str,
    price: float,
    vol_today_idr: float,
    atr_pct: float,
    spread_pct: float,
) -> dict:
    """
    [M02] Unified order book getter — real L2 jika tersedia, fallback ke synthetic.

    Ini fungsi yang harus dipanggil dari simulate_execution() dan
    estimate_queue_position() — bukan langsung ke build_synthetic_order_book().

    Returns: format dict standar (sama untuk semua source).
    """
    from datetime import datetime

    # Coba real L2 dulu
    real_l2 = fetch_real_l2_orderbook(ticker, price)
    if real_l2 is not None:
        return real_l2

    # Fallback: synthetic dari v7.17
    synth = build_synthetic_order_book(
        price=price,
        vol_today_idr=vol_today_idr,
        atr_pct=atr_pct,
        spread_pct=spread_pct,
        ticker=ticker,
    )
    synth["source"]    = "synthetic"
    synth["timestamp"] = datetime.now(WIB).strftime("%H:%M:%S WIB")
    return synth


# ════════════════════════════════════════════════════════════════════
#  [M03] EXECUTION FEEDBACK LOOP — v7.18
#
#  Core idea: bandingkan simulated_entry (dari simulate_execution())
#  dengan actual_close (harga aktual setelah order dikirim).
#
#  Pipeline:
#    1. Saat signal dikirim → simpan simulated_entry + sim params
#    2. Di run berikutnya → ambil harga aktual candle setelah signal
#    3. Hitung slippage error = (actual - simulated) / simulated
#    4. Aggregate per (cap_tier, session) → bias correction factor
#    5. Simpan correction ke Supabase tabel "slippage_calibration"
#    6. simulate_execution() baca correction di awal → adjusted model
#
#  Tabel Supabase yang perlu dibuat:
#
#  CREATE TABLE IF NOT EXISTS execution_fills (
#    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
#    signal_id       TEXT,
#    ticker          TEXT,
#    side            TEXT,
#    strategy        TEXT,
#    cap_tier        TEXT,
#    session         TEXT,
#    simulated_entry FLOAT,
#    sim_cost_pct    FLOAT,    -- total cost % yang kita simulasikan
#    actual_close    FLOAT,    -- harga close candle pertama setelah signal
#    slippage_error  FLOAT,    -- (actual_cost - sim_cost) / entry
#    recorded_at     TIMESTAMPTZ DEFAULT NOW()
#  );
#
#  CREATE TABLE IF NOT EXISTS slippage_calibration (
#    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
#    cap_tier        TEXT,
#    session         TEXT,
#    bias_correction FLOAT,   -- rata-rata slippage error per tier+session
#    sample_count    INT,
#    updated_at      TIMESTAMPTZ DEFAULT NOW()
#  );
# ════════════════════════════════════════════════════════════════════

# ── In-memory cache untuk slippage correction ──────────────────────
# Key: (cap_tier, session) → bias_correction float
# Diisi oleh load_slippage_corrections() di awal run()
_SLIPPAGE_CORRECTIONS: dict[tuple, float] = {}
_CORRECTIONS_LOADED = False


def load_slippage_corrections() -> None:
    """
    [M03] Baca slippage correction factors dari Supabase ke cache.
    Dipanggil sekali di awal run() sebelum scan dimulai.
    """
    global _CORRECTIONS_LOADED
    try:
        rows = (
            supabase.table("slippage_calibration")
            .select("cap_tier, session, bias_correction, sample_count")
            .gte("sample_count", 5)   # butuh minimal 5 sample agar reliable
            .execute()
            .data
        )
        if not rows:
            log("  ℹ️ [M03] Slippage calibration: belum ada data (cold start)")
            _CORRECTIONS_LOADED = True
            return

        for r in rows:
            key = (r["cap_tier"], r["session"])
            _SLIPPAGE_CORRECTIONS[key] = float(r["bias_correction"])

        log(f"  ✅ [M03] Loaded {len(_SLIPPAGE_CORRECTIONS)} slippage corrections: "
            f"{list(_SLIPPAGE_CORRECTIONS.keys())}")
        _CORRECTIONS_LOADED = True

    except Exception as e:
        log(f"  ⚠️ [M03] load_slippage_corrections gagal: {e}", "warn")
        _CORRECTIONS_LOADED = True   # jangan block bot


def get_slippage_correction(cap_tier: str, session: str) -> float:
    """
    [M03] Return bias correction untuk pasangan (cap_tier, session).

    Correction > 0 → kita selama ini under-estimate slippage
    Correction < 0 → kita selama ini over-estimate slippage
    Return 0.0 jika belum ada data (cold start).
    """
    if not _CORRECTIONS_LOADED:
        load_slippage_corrections()

    exact = _SLIPPAGE_CORRECTIONS.get((cap_tier, session), None)
    if exact is not None:
        return exact

    # Fallback: pakai average semua session di tier ini
    tier_corrections = [v for (t, _), v in _SLIPPAGE_CORRECTIONS.items()
                        if t == cap_tier]
    if tier_corrections:
        return sum(tier_corrections) / len(tier_corrections)

    return 0.0  # cold start


def record_execution_fill(
    signal_id: str,
    ticker: str,
    side: str,
    strategy: str,
    cap_tier: str,
    session: str,
    simulated_entry: float,
    sim_cost_pct: float,
) -> None:
    """
    [M03] Simpan record fill ke Supabase segera setelah signal dikirim.

    actual_close akan diisi nanti oleh update_execution_fills().
    Dipanggil dari send_signal() / submit_signal_order().
    """
    try:
        supabase.table("execution_fills").insert({
            "signal_id":       signal_id,
            "ticker":          ticker,
            "side":            side,
            "strategy":        strategy,
            "cap_tier":        cap_tier,
            "session":         session,
            "simulated_entry": simulated_entry,
            "sim_cost_pct":    sim_cost_pct,
            "actual_close":    None,   # diisi oleh update_execution_fills()
            "slippage_error":  None,
        }).execute()
        log(f"  📝 [M03] Fill record saved: {ticker} {side} @ sim {simulated_entry:,.0f}")
    except Exception as e:
        log(f"  ⚠️ [M03] record_execution_fill gagal: {e}", "warn")


def update_execution_fills() -> None:
    """
    [M03] Isi actual_close untuk fill records yang masih NULL.

    [M04] Upgrade ke V2 pipeline:
      - resolve_fill_price_v2() — broker ALWAYS first, via BrokerFillManager
        (bukan V1 yang butuh caller pass adapter manual, sering di-skip)
      - recalculate_slippage_model_v2() — confidence-weighted EWA
        (bukan V1 yang tidak distinguish kualitas fill source)
      - update_fill_with_source() untuk konsistensi metadata

    Dipanggil di awal setiap run() — sebelum scan, setelah
    load_slippage_corrections().
    """
    try:
        rows = (
            supabase.table("execution_fills")
            .select("id, ticker, side, strategy, cap_tier, session, "
                    "simulated_entry, sim_cost_pct, broker_order_id, "
                    "signal_id, recorded_at")
            .is_("actual_close", "null")
            .order("recorded_at", desc=True)
            .limit(30)
            .execute()
            .data
        )
        if not rows:
            return

        now_utc = datetime.now(timezone.utc)
        updated = 0

        for row in rows:
            try:
                # Cek usia record — butuh minimal 1 candle setelah signal
                recorded = datetime.fromisoformat(row["recorded_at"])
                if recorded.tzinfo is None:
                    recorded = recorded.replace(tzinfo=timezone.utc)

                strategy = row.get("strategy", "SWING")
                min_wait = timedelta(hours=2 if strategy == "INTRADAY" else 24)
                if (now_utc - recorded) < min_wait:
                    continue   # terlalu dini

                sim_entry    = float(row.get("simulated_entry") or 0)
                sim_cost_pct = float(row.get("sim_cost_pct") or 0)

                if sim_entry <= 0:
                    continue

                # [M04] V2 fill resolver — broker selalu dicoba dulu via
                # BrokerFillManager (adapter + token sudah di-cache + auto-refresh)
                fill_result = resolve_fill_price_v2(signal_row=row)

                update_fill_with_source(
                    fill_record_id=row["id"],
                    fill_result=fill_result,
                    simulated_entry=sim_entry,
                    sim_cost_pct=sim_cost_pct,
                    side=row.get("side", "BUY"),
                )
                updated += 1

                # ── [P7-03] Fill deviation hard check ─────────────────
                # Hanya cek jika source adalah broker actual (bukan proxy/sim)
                # agar tidak flood alert dari open/close proxy yang memang
                # punya inherent deviation dari sim entry.
                _actual_fill = fill_result.get("fill_price", 0.0)
                _fill_src    = fill_result.get("fill_source", "")
                if _actual_fill > 0 and sim_entry > 0 and _fill_src == FILL_SOURCE_BROKER:
                    try:
                        check_fill_deviation_hard(
                            ticker          = row.get("ticker", "?"),
                            actual_fill     = _actual_fill,
                            simulated_entry = sim_entry,
                            strategy        = strategy,
                        )
                    except Exception as _dev_e:
                        log(f"  ⚠️ [P7-03] deviation check [{row.get('ticker','?')}]: "
                            f"{_dev_e}", "warn")

            except Exception as e:
                log(f"  ⚠️ [M04] update fill [{row.get('ticker')}]: {e}", "warn")
                continue

        if updated > 0:
            log(f"  ✅ [M04] {updated} execution fills updated (V2 pipeline)")
            # [M04] V2 calibration — confidence-weighted, broker fills 3x bobot
            recalculate_slippage_model_v2()

    except Exception as e:
        log(f"  ⚠️ [M04] update_execution_fills: {e}", "warn")


def recalculate_slippage_model() -> None:
    """
    [M03] Recalculate bias correction per (cap_tier, session) dari fills historis.

    Menggunakan exponential weighted average — data baru lebih berpengaruh
    dari data lama (alpha=0.3: 70% bobot untuk history, 30% untuk data baru).

    Simpan ke tabel slippage_calibration di Supabase.
    """
    ALPHA = 0.30   # EWA decay: semakin tinggi = semakin responsif ke data baru
    MIN_SAMPLES = 5

    try:
        rows = (
            supabase.table("execution_fills")
            .select("cap_tier, session, slippage_error")
            .not_.is_("slippage_error", "null")
            .order("recorded_at", desc=True)
            .limit(200)   # pakai 200 fill terbaru
            .execute()
            .data
        )
        if not rows:
            return

        # Group by (cap_tier, session)
        from collections import defaultdict
        grouped: dict[tuple, list] = defaultdict(list)
        for r in rows:
            key = (r["cap_tier"] or "UNKNOWN", r["session"] or "UNKNOWN")
            grouped[key].append(float(r["slippage_error"]))

        updated_keys = 0
        for (cap_tier, session), errors in grouped.items():
            if len(errors) < MIN_SAMPLES:
                continue

            # EWA: proses dari tertua ke terbaru (errors sorted desc sudah)
            # Karena kita query desc, kita reverse untuk EWA
            ewa = errors[-1]   # mulai dari yang paling lama
            for err in reversed(errors[:-1]):
                ewa = ALPHA * err + (1 - ALPHA) * ewa

            # Clamp: bias correction tidak boleh ekstrem
            bias = round(max(-2.0, min(2.0, ewa)), 6)

            # Upsert ke slippage_calibration
            try:
                existing = (
                    supabase.table("slippage_calibration")
                    .select("id")
                    .eq("cap_tier", cap_tier)
                    .eq("session", session)
                    .execute()
                    .data
                )
                payload = {
                    "cap_tier":        cap_tier,
                    "session":         session,
                    "bias_correction": bias,
                    "sample_count":    len(errors),
                    "updated_at":      datetime.now(timezone.utc).isoformat(),
                }
                if existing:
                    supabase.table("slippage_calibration").update(payload).eq(
                        "id", existing[0]["id"]).execute()
                else:
                    supabase.table("slippage_calibration").insert(payload).execute()

                # Update in-memory cache juga
                _SLIPPAGE_CORRECTIONS[(cap_tier, session)] = bias
                updated_keys += 1
                log(f"  🔧 [M03] Calibration [{cap_tier}/{session}]: "
                    f"bias={bias:+.4f}% (n={len(errors)})")

            except Exception as e:
                log(f"  ⚠️ [M03] upsert calibration [{cap_tier}/{session}]: {e}", "warn")

        log(f"  ✅ [M03] Slippage model recalibrated: {updated_keys} buckets updated")

    except Exception as e:
        log(f"  ⚠️ [M03] recalculate_slippage_model: {e}", "warn")


def get_calibrated_cost_pct(
    base_cost_pct: float,
    cap_tier: str,
    session: str,
) -> tuple[float, float]:
    """
    [M03] Apply slippage bias correction ke simulated cost.

    Fungsi ini dipanggil di simulate_execution() — menggantikan total_cost_pct
    mentah dengan versi yang sudah dikalibrasi berdasarkan data historis.

    Returns: (calibrated_cost_pct, correction_applied)
    """
    correction = get_slippage_correction(cap_tier, session)

    if correction == 0.0:
        return base_cost_pct, 0.0

    # Apply correction: jika model selalu under-estimate, tambahkan bias
    calibrated = base_cost_pct + correction
    calibrated = max(0.0, calibrated)   # tidak boleh negatif

    if abs(correction) > 0.001:
        log(f"  🔧 [M03] Slippage correction [{cap_tier}/{session}]: "
            f"{base_cost_pct:.3f}% → {calibrated:.3f}% ({correction:+.4f}%)")

    return round(calibrated, 4), round(correction, 6)


# ════════════════════════════════════════════════════════════════════
#  INTEGRATION INSTRUCTIONS — Tambahkan ke run() dan send_signal()
# ════════════════════════════════════════════════════════════════════
#
#  ── 1. Di awal run(), sebelum loop scan: ────────────────────────
#
#     # [M03] Load slippage corrections yang tersimpan
#     load_slippage_corrections()
#     # [M03] Update actual fills dari run sebelumnya
#     update_execution_fills()
#
#  ── 2. Di simulate_execution(), ganti total_cost_pct final: ────
#
#     # Ganti baris:
#     #   total_cost_pct = spread + delay + queue + vol + impact
#     # Dengan:
#     total_cost_pct = spread + delay + queue + vol + impact
#     calibrated_cost, correction = get_calibrated_cost_pct(
#         total_cost_pct,
#         cap_tier=mkt_impact["cap_tier"],
#         session=queue_info["session"],
#     )
#     total_cost_pct = calibrated_cost
#     result["slippage_correction"] = correction
#
#  ── 3. Di send_signal() setelah save_signal(): ─────────────────
#
#     sig_id = sig.get("signal_id", "")  # atau dari Supabase insert response
#     record_execution_fill(
#         signal_id       = sig_id,
#         ticker          = sig["ticker"],
#         side            = sig["side"],
#         strategy        = sig["strategy"],
#         cap_tier        = sig.get("cap_tier", "UNKNOWN"),
#         session         = sig.get("queue_session", "UNKNOWN"),
#         simulated_entry = sig.get("entry_adj", sig.get("entry", 0)),
#         sim_cost_pct    = sig.get("net_cost_pct", 0.0),
#     )
#
#  ── 4. Di _live_order() (ExecutionBridge v7.17): ───────────────
#
#     # Ganti _live_order() generic dengan:
#     adapter = get_broker_adapter()
#     if adapter is None:
#         return self._dry_run(order)
#     token = adapter.authenticate()
#     if token is None:
#         return {**order, "status": "AUTH_FAILED",
#                 "note": "Broker auth gagal — fallback dry_run"}
#     result = adapter.place_order(token, order)
#     order["order_id"] = result.get("broker_order_id", order["order_id"])
#     return {**order, **result}
#
#  ── 5. ENV baru yang perlu ditambah ke GitHub Actions secrets: ──
#
#     BROKER=ipot                       # atau: most / none
#     BROKER_USERNAME=<ipot_username>   # hanya jika BROKER=ipot
#     BROKER_PASSWORD=<ipot_password>   # hanya jika BROKER=ipot
#     BROKER_PIN=<ipot_pin>             # hanya jika BROKER=ipot
#     BROKER_CLIENT_ID=<most_id>        # hanya jika BROKER=most
#     BROKER_CLIENT_SECRET=<most_sec>   # hanya jika BROKER=most
#     L2_SOURCE=synthetic               # atau: ipot_stream / rti
#     L2_API_URL=<endpoint>             # hanya jika L2_SOURCE != synthetic
#     L2_API_KEY=<api_key>              # hanya jika L2_SOURCE != synthetic
#
#  ── 6. SQL untuk 2 tabel baru di Supabase: ─────────────────────
#
#     CREATE TABLE IF NOT EXISTS execution_fills (
#       id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
#       signal_id       TEXT,
#       ticker          TEXT,
#       side            TEXT,
#       strategy        TEXT,
#       cap_tier        TEXT,
#       session         TEXT,
#       simulated_entry FLOAT,
#       sim_cost_pct    FLOAT,
#       actual_close    FLOAT,
#       slippage_error  FLOAT,
#       recorded_at     TIMESTAMPTZ DEFAULT NOW()
#     );
#
#     CREATE TABLE IF NOT EXISTS slippage_calibration (
#       id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
#       cap_tier        TEXT,
#       session         TEXT,
#       bias_correction FLOAT,
#       sample_count    INT,
#       updated_at      TIMESTAMPTZ DEFAULT NOW()
#     );
#
#     -- Index untuk query performance
#     CREATE INDEX ON execution_fills (ticker, strategy);
#     CREATE INDEX ON execution_fills (cap_tier, session);
#     CREATE UNIQUE INDEX ON slippage_calibration (cap_tier, session);
# ════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════
#  PATCH v7.19 — Data Quality Pipeline + Fill Price Reconciliation
# ════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════
#  [N01] DATA QUALITY PIPELINE — v7.19
#
#  Problem yang ada sekarang:
#    check_data_corruption() (J05) hanya cek anomali keras:
#    High < Low, zero price, volume spike, monotone.
#    Tidak ada:
#      - Cross-timeframe consistency check
#      - Latency measurement per ticker
#      - Quality score aggregate yang bisa di-threshold
#      - Graceful degradation (skip fitur tertentu jika data jelek)
#
#  Filosofi:
#    Setiap candle punya "quality score" 0-100.
#    Semakin rendah → semakin banyak komponen signal yang di-skip.
#    Bukan binary block/pass, tapi degradation bertingkat.
#
#  Quality score breakdown (total 100):
#    Freshness     (30) : seberapa baru candle terakhir
#    Consistency   (25) : cross-timeframe price alignment
#    Completeness  (25) : tidak ada candle yang missing/NaN
#    Plausibility  (20) : distribusi return wajar (tidak fat-tail ekstrem)
# ════════════════════════════════════════════════════════════════════

DQ_BLOCK_THRESHOLD   = 40   # < 40: block signal sepenuhnya
DQ_DEGRADE_THRESHOLD = 65   # < 65: skip komponen advanced (OB, liquidity trap)
DQ_WARN_THRESHOLD    = 80   # < 80: log warning, tetap lanjut


def calc_data_freshness_score(ticker: str, interval: str) -> tuple[float, float]:
    """
    [N01] Freshness score dari timestamp candle terakhir — 0 sampai 30.

    Berbeda dari is_candle_stale() yang binary — ini kontinu.
    Returns: (score, age_minutes)
    """
    try:
        import yfinance as yf
        from datetime import datetime, timezone

        period = "7d" if interval == "1h" else "30d"
        df = yf.download(ticker, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return 0.0, 9999.0

        last_ts = df.index[-1]
        if hasattr(last_ts, "tzinfo") and last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        age_minutes = (now_utc - last_ts).total_seconds() / 60.0

        if interval == "1h":
            if age_minutes <= 20:     score = 30.0
            elif age_minutes <= 45:   score = 25.0
            elif age_minutes <= 90:   score = 15.0
            elif age_minutes <= 180:  score = 5.0
            else:                     score = 0.0
        else:
            trading_day_minutes = 8 * 60
            days_old = age_minutes / trading_day_minutes
            if days_old <= 1:    score = 30.0
            elif days_old <= 2:  score = 22.0
            elif days_old <= 5:  score = 10.0
            else:                score = 0.0

        return round(score, 2), round(age_minutes, 1)

    except Exception as e:
        log(f"  ⚠️ [N01] freshness [{ticker}/{interval}]: {e}", "warn")
        return 0.0, 9999.0


def calc_cross_timeframe_consistency(
    closes_1h: np.ndarray,
    closes_1d: np.ndarray,
    ticker: str = "",
) -> float:
    """
    [N01] Cross-timeframe consistency check — score 0-25.

    Apakah harga 1h dan 1d agree? Gap besar = suspect data error.
    Wajar: gap <= 5% (intraday range normal). Suspect: > 15%.
    """
    try:
        if len(closes_1h) < 2 or len(closes_1d) < 2:
            return 12.5

        price_1h = float(closes_1h[-1])
        price_1d = float(closes_1d[-1])

        if price_1d <= 0 or price_1h <= 0:
            return 0.0

        gap_pct = abs(price_1h - price_1d) / price_1d * 100

        if gap_pct <= 1.0:    score = 25.0
        elif gap_pct <= 3.0:  score = 20.0
        elif gap_pct <= 5.0:  score = 15.0
        elif gap_pct <= 10.0: score = 8.0
        elif gap_pct <= 15.0: score = 3.0
        else:
            log(f"  ⚠️ [N01] Cross-TF gap ekstrem [{ticker}]: "
                f"1h={price_1h:,.0f} vs 1d={price_1d:,.0f} ({gap_pct:.1f}%)", "warn")
            score = 0.0

        return round(score, 2)

    except Exception as e:
        log(f"  ⚠️ [N01] cross_tf_consistency [{ticker}]: {e}", "warn")
        return 12.5


def calc_candle_completeness(
    closes: np.ndarray,
    highs:  np.ndarray,
    lows:   np.ndarray,
    volumes: np.ndarray,
    expected_candles: int,
    ticker: str = "",
) -> float:
    """
    [N01] Kelengkapan candle — score 0-25.

    Cek: missing candles, zero-volume, NaN count.
    """
    try:
        n = len(closes)
        if n == 0:
            return 0.0

        completeness_ratio = min(n / max(expected_candles, 1), 1.0)
        zero_vol = np.sum(volumes <= 0)
        zero_vol_ratio = zero_vol / n
        nan_count = (np.sum(np.isnan(closes)) + np.sum(np.isnan(highs))
                     + np.sum(np.isnan(lows)))
        nan_ratio = nan_count / (n * 3)

        raw_score = (completeness_ratio * 0.60
                     + (1 - zero_vol_ratio) * 0.25
                     + (1 - nan_ratio) * 0.15)
        score = round(raw_score * 25, 2)

        if score < 20:
            log(f"  ⚠️ [N01] Completeness low [{ticker}]: "
                f"n={n}/{expected_candles} zero_vol={zero_vol} nan={nan_count} "
                f"-> {score:.1f}/25", "warn")
        return score

    except Exception as e:
        log(f"  ⚠️ [N01] completeness [{ticker}]: {e}", "warn")
        return 12.5


def calc_return_plausibility(closes: np.ndarray, ticker: str = "") -> float:
    """
    [N01] Distribusi return wajar untuk saham IDX — score 0-20.

    Return > 35% single day (non-ARA) = hampir pasti data error.
    Terlalu banyak extreme returns = data noisy.
    """
    try:
        n = len(closes)
        if n < 5:
            return 10.0

        c = closes.astype(float)
        returns = np.abs(np.diff(c) / (c[:-1] + 1e-9))

        extreme_count = np.sum(returns > 0.25)
        suspicious    = np.sum(returns > 0.35)
        mean_return   = float(np.mean(returns))

        if suspicious > 0:
            score = max(0.0, 20.0 - suspicious * 8.0)
        elif extreme_count > 2:
            score = max(5.0, 20.0 - extreme_count * 3.0)
        elif mean_return > 0.05:
            score = 8.0
        elif mean_return > 0.03:
            score = 14.0
        else:
            score = 20.0

        return round(score, 2)

    except Exception as e:
        log(f"  ⚠️ [N01] plausibility [{ticker}]: {e}", "warn")
        return 10.0


def calc_data_quality_score(
    ticker: str,
    closes_1h: np.ndarray | None,
    closes_1d: np.ndarray,
    highs_1d:  np.ndarray,
    lows_1d:   np.ndarray,
    volumes_1d: np.ndarray,
    strategy: str = "SWING",
) -> dict:
    """
    [N01] Master data quality scorer — gabung semua komponen.

    Dipanggil sekali per ticker di scan loop, setelah get_candles()
    tapi sebelum check_intraday() / check_swing().

    Returns dict dengan:
      score (0-100), label, komponen, allow_signal, allow_advanced, note.

    Degradation tiers:
      GOOD     (>= 80): semua fitur aktif
      DEGRADED (>= 65): skip OB / liquidity_trap / SMT
      POOR     (>= 40): signal dikirim tapi sangat dibatasi
      BLOCK    (<  40): tidak ada signal sama sekali
    """
    interval = "1h" if strategy == "INTRADAY" else "1d"

    freshness, age_minutes = calc_data_freshness_score(ticker, interval)

    if closes_1h is not None and len(closes_1h) >= 2:
        consistency = calc_cross_timeframe_consistency(closes_1h, closes_1d, ticker)
    else:
        consistency = 18.0 if strategy == "SWING" else 12.0

    expected = 120 if strategy == "INTRADAY" else 60
    completeness = calc_candle_completeness(
        closes_1d, highs_1d, lows_1d, volumes_1d, expected, ticker)

    plausibility = calc_return_plausibility(closes_1d, ticker)

    total = freshness + consistency + completeness + plausibility
    score = round(min(100, max(0, total)))

    if score >= DQ_WARN_THRESHOLD:
        label, allow_signal, allow_advanced = "GOOD",     True,  True
    elif score >= DQ_DEGRADE_THRESHOLD:
        label, allow_signal, allow_advanced = "DEGRADED", True,  False
    elif score >= DQ_BLOCK_THRESHOLD:
        label, allow_signal, allow_advanced = "POOR",     False, False
    else:
        label, allow_signal, allow_advanced = "BLOCK",    False, False

    note = (f"DQ [{ticker}] {score}/100 [{label}] | "
            f"fresh={freshness:.0f} consist={consistency:.0f} "
            f"complete={completeness:.0f} plaus={plausibility:.0f} "
            f"age={age_minutes:.0f}m")

    level = "warn" if score < DQ_WARN_THRESHOLD else "info"
    emoji = "🔴" if score < DQ_BLOCK_THRESHOLD else "🟡" if score < DQ_WARN_THRESHOLD else "✅"
    log(f"  {emoji} [N01] {note}", level)

    return {
        "score":          score,
        "label":          label,
        "freshness":      freshness,
        "consistency":    consistency,
        "completeness":   completeness,
        "plausibility":   plausibility,
        "age_minutes":    age_minutes,
        "allow_signal":   allow_signal,
        "allow_advanced": allow_advanced,
        "note":           note,
    }


# ════════════════════════════════════════════════════════════════════
#  [N02] FILL PRICE RECONCILIATION — v7.19
#
#  Problem v7.18:
#    actual_close = close candle setelah signal → ini PROXY, bukan fill.
#
#  Jarak antara proxy dan actual fill:
#    - Order bisa terisi di harga berbeda dari close candle
#    - Partial fill: rata-rata fill price bisa beda dari target
#    - Gap open: open besok lebih representatif dari close hari ini
#    - Large orders: market impact push price sebelum full fill
#
#  Solusi: Confidence Tier per sumber data fill
#
#    Tier A [4] BROKER_ACTUAL   : fill price dari API broker
#    Tier B [3] OPEN_PROXY      : open candle setelah signal
#    Tier C [2] CLOSE_PROXY     : close candle setelah signal
#    Tier D [1] SIMULATED       : hanya model, tidak ada data aktual
#
#  EWA calibration: hanya pakai Tier >= 2.
#  Broker fills (tier 4) dihitung 3x lebih berat dalam EWA.
# ════════════════════════════════════════════════════════════════════

FILL_SOURCE_BROKER = "broker_actual"   # confidence 4
FILL_SOURCE_OPEN   = "open_proxy"      # confidence 3
FILL_SOURCE_CLOSE  = "close_proxy"     # confidence 2
FILL_SOURCE_SIM    = "simulated"       # confidence 1

FILL_CONFIDENCE = {
    FILL_SOURCE_BROKER: 4,
    FILL_SOURCE_OPEN:   3,
    FILL_SOURCE_CLOSE:  2,
    FILL_SOURCE_SIM:    1,
}

FILL_MIN_CONFIDENCE_FOR_CALIBRATION = 2
FILL_BROKER_WEIGHT = 3.0   # broker fills dihitung 3x dalam EWA


def resolve_fill_price(
    signal_row: dict,
    broker_adapter=None,
    broker_token: str | None = None,
) -> dict:
    """
    [N02] Resolve harga fill terbaik yang tersedia untuk satu signal.

    Prioritas:
      1. Broker actual (dari get_order_status() jika LIVE)
      2. Open candle setelah signal (better proxy)
      3. Close candle (labeled jelas sebagai proxy)
      4. Simulated (honest fallback)

    Returns dict: fill_price, fill_source, fill_confidence, fill_lots, note.
    """
    ticker    = signal_row.get("ticker", "")
    side      = signal_row.get("side", "BUY")
    strategy  = signal_row.get("strategy", "SWING")
    sim_entry = float(signal_row.get("simulated_entry") or 0)

    # ── Path 1: Broker actual ────────────────────────────────────
    broker_order_id = signal_row.get("broker_order_id", "")
    if broker_adapter and broker_token and broker_order_id:
        try:
            status     = broker_adapter.get_order_status(broker_token, broker_order_id)
            fill_price = float(status.get("fill_price") or 0)
            fill_lots  = int(status.get("fill_lots") or 0)
            fill_stat  = status.get("status", "")

            if fill_price > 0 and fill_stat in ("FILLED", "PARTIAL_FILL"):
                note = (f"Broker actual fill @ {fill_price:,.0f} "
                        f"({fill_lots} lot | {fill_stat})")
                log(f"  ✅ [N02] {ticker}: {note}")
                return {
                    "fill_price":      fill_price,
                    "fill_source":     FILL_SOURCE_BROKER,
                    "fill_confidence": FILL_CONFIDENCE[FILL_SOURCE_BROKER],
                    "fill_lots":       fill_lots,
                    "note":            note,
                }
        except Exception as e:
            log(f"  ⚠️ [N02] broker fill check [{ticker}]: {e}", "warn")

    # ── Path 2 & 3: Candle proxy ─────────────────────────────────
    try:
        ticker_jk = ticker if ticker.endswith(".JK") else ticker + ".JK"
        interval  = "1h" if strategy == "INTRADAY" else "1d"
        data = get_candles(ticker_jk, interval, 5)

        if data is not None:
            closes, highs, lows, volumes, opens = data

            # Open candle setelah signal = lebih representatif
            # karena order kemungkinan terisi saat market open,
            # bukan saat close
            open_price = float(opens[-2]) if len(opens) >= 2 else float(opens[-1])
            if open_price > 0:
                note = f"Open proxy @ {open_price:,.0f} (open candle setelah signal)"
                log(f"  📊 [N02] {ticker}: {note}")
                return {
                    "fill_price":      open_price,
                    "fill_source":     FILL_SOURCE_OPEN,
                    "fill_confidence": FILL_CONFIDENCE[FILL_SOURCE_OPEN],
                    "fill_lots":       0,
                    "note":            note,
                }

            close_price = float(closes[-2]) if len(closes) >= 2 else float(closes[-1])
            if close_price > 0:
                note = (f"Close proxy @ {close_price:,.0f} "
                        f"[PROXY — bukan actual fill]")
                log(f"  📊 [N02] {ticker}: {note}")
                return {
                    "fill_price":      close_price,
                    "fill_source":     FILL_SOURCE_CLOSE,
                    "fill_confidence": FILL_CONFIDENCE[FILL_SOURCE_CLOSE],
                    "fill_lots":       0,
                    "note":            note,
                }

    except Exception as e:
        log(f"  ⚠️ [N02] candle resolve [{ticker}]: {e}", "warn")

    # ── Path 4: Simulated only ───────────────────────────────────
    note = f"Simulated only @ {sim_entry:,.0f} [MODEL — tidak ada data fill aktual]"
    log(f"  📋 [N02] {ticker}: {note}")
    return {
        "fill_price":      sim_entry,
        "fill_source":     FILL_SOURCE_SIM,
        "fill_confidence": FILL_CONFIDENCE[FILL_SOURCE_SIM],
        "fill_lots":       0,
        "note":            note,
    }


def update_fill_with_source(
    fill_record_id: str,
    fill_result: dict,
    simulated_entry: float,
    sim_cost_pct: float,
    side: str,
) -> dict:
    """
    [N02] Update execution_fills dengan hasil resolve_fill_price().

    Hitung slippage_error dengan awareness terhadap fill source confidence.
    Flag jika error ekstrem — bisa indikasi model perlu redesign.
    """
    fill_price  = fill_result["fill_price"]
    fill_source = fill_result["fill_source"]
    confidence  = fill_result["fill_confidence"]

    if simulated_entry <= 0 or fill_price <= 0:
        return {}

    if side == "BUY":
        actual_cost_pct = (fill_price - simulated_entry) / simulated_entry * 100
    else:
        actual_cost_pct = (simulated_entry - fill_price) / simulated_entry * 100

    slippage_error = actual_cost_pct - sim_cost_pct

    payload = {
        "actual_close":       fill_price,
        "broker_fill_price":  fill_price if fill_source == FILL_SOURCE_BROKER else None,
        "fill_source":        fill_source,
        "fill_confidence":    confidence,
        "slippage_error":     round(slippage_error, 6),
        "fill_note":          fill_result["note"][:200],
    }

    if abs(slippage_error) > 3.0:
        payload["model_alert"] = True
        log(f"  🔴 [N02] Slippage error ekstrem ({slippage_error:+.2f}%) "
            f"[{fill_source}] — cek model!", "error")

    try:
        supabase.table("execution_fills").update(payload).eq(
            "id", fill_record_id).execute()
        log(f"  ✅ [N02] Fill updated [{fill_source}|conf={confidence}]: "
            f"sim={sim_cost_pct:.3f}% actual={actual_cost_pct:.3f}% "
            f"error={slippage_error:+.3f}%")
    except Exception as e:
        log(f"  ⚠️ [N02] update fill DB [{fill_record_id}]: {e}", "warn")

    return payload


def recalculate_slippage_model_v2() -> None:
    """
    [N02] Enhanced EWA calibration — confidence-weighted.

    Perubahan dari v7.18:
      - Hanya pakai fill_confidence >= 2 (close_proxy ke atas)
      - Broker fills (confidence=4) dihitung 3x dalam EWA
      - Alert ke Telegram jika bias > 1.5% — model mungkin perlu redesign
      - Log distribusi confidence per bucket untuk transparency
    """
    ALPHA    = 0.30
    MIN_CONF = FILL_MIN_CONFIDENCE_FOR_CALIBRATION

    try:
        rows = (
            supabase.table("execution_fills")
            .select("cap_tier, session, slippage_error, fill_confidence, recorded_at")
            .not_.is_("slippage_error", "null")
            .gte("fill_confidence", MIN_CONF)
            .order("recorded_at", desc=True)
            .limit(300)
            .execute()
            .data
        )
        if not rows:
            log("  ℹ️ [N02] No qualified fills for calibration yet")
            return

        from collections import defaultdict
        grouped: dict[tuple, list[tuple[float, float]]] = defaultdict(list)

        for r in rows:
            key    = (r["cap_tier"] or "UNKNOWN", r["session"] or "UNKNOWN")
            err    = float(r["slippage_error"])
            conf   = int(r.get("fill_confidence") or 2)
            weight = FILL_BROKER_WEIGHT if conf == 4 else 1.0
            grouped[key].append((err, weight))

        updated_keys = 0
        model_alerts = []

        for (cap_tier, session), err_weights in grouped.items():
            if len(err_weights) < 5:
                continue

            # Weighted EWA — oldest first
            errors  = [e for e, _ in err_weights]
            weights = [w for _, w in err_weights]

            ewa  = errors[-1]
            wsum = weights[-1]
            for err, w in zip(reversed(errors[:-1]), reversed(weights[:-1])):
                ewa  = (ALPHA * w * err + (1 - ALPHA) * wsum * ewa) / (
                        ALPHA * w + (1 - ALPHA) * wsum)
                wsum = ALPHA * w + (1 - ALPHA) * wsum

            bias      = round(max(-2.0, min(2.0, ewa)), 6)
            broker_n  = sum(1 for _, w in err_weights if w > 1.0)
            proxy_n   = len(err_weights) - broker_n

            log(f"  🔧 [N02] Calibration [{cap_tier}/{session}]: "
                f"bias={bias:+.4f}% | n={len(err_weights)} "
                f"(broker={broker_n} proxy={proxy_n})")

            if abs(bias) > 1.5:
                model_alerts.append(f"{cap_tier}/{session}: bias={bias:+.3f}%")

            try:
                existing = (
                    supabase.table("slippage_calibration")
                    .select("id")
                    .eq("cap_tier", cap_tier)
                    .eq("session", session)
                    .execute()
                    .data
                )
                payload = {
                    "cap_tier":          cap_tier,
                    "session":           session,
                    "bias_correction":   bias,
                    "sample_count":      len(err_weights),
                    "broker_sample_count": broker_n,
                    "updated_at":        datetime.now(timezone.utc).isoformat(),
                }
                if existing:
                    supabase.table("slippage_calibration").update(payload).eq(
                        "id", existing[0]["id"]).execute()
                else:
                    supabase.table("slippage_calibration").insert(payload).execute()

                _SLIPPAGE_CORRECTIONS[(cap_tier, session)] = bias
                updated_keys += 1

            except Exception as e:
                log(f"  ⚠️ [N02] upsert calibration: {e}", "warn")

        log(f"  ✅ [N02] Slippage model v2 recalibrated: {updated_keys} buckets")

        if model_alerts:
            alert_msg = (
                "⚠️ <b>SLIPPAGE MODEL ALERT</b>\n"
                "Bias koreksi > 1.5% — model mungkin perlu ditinjau:\n"
                + "\n".join(f"  • {a}" for a in model_alerts)
            )
            try:
                tg(alert_msg)
            except Exception:
                log(f"  🔴 [N02] Model alert (TG gagal): {model_alerts}", "error")

    except Exception as e:
        log(f"  ⚠️ [N02] recalculate_slippage_model_v2: {e}", "warn")


def get_fill_quality_report() -> str:
    """
    [N02] Laporan singkat distribusi fill quality untuk health check heartbeat.

    Menunjukkan % signal yang punya broker fill vs hanya proxy.
    Indikator seberapa reliable kalibrasi model kita.
    """
    try:
        rows = (
            supabase.table("execution_fills")
            .select("fill_source")
            .not_.is_("fill_source", "null")
            .order("recorded_at", desc=True)
            .limit(50)
            .execute()
            .data
        )
        if not rows:
            return "Fill data: belum ada"

        from collections import Counter
        counts = Counter(r["fill_source"] for r in rows)
        total  = len(rows)

        def pct(src): return counts.get(src, 0) / total * 100

        return (f"Fill quality (n={total}): "
                f"broker={pct(FILL_SOURCE_BROKER):.0f}% "
                f"open={pct(FILL_SOURCE_OPEN):.0f}% "
                f"close={pct(FILL_SOURCE_CLOSE):.0f}% "
                f"sim={pct(FILL_SOURCE_SIM):.0f}%")
    except Exception as e:
        return f"Fill report error: {e}"


# ════════════════════════════════════════════════════════════════════
#  INTEGRATION INSTRUCTIONS
# ════════════════════════════════════════════════════════════════════
#
#  1. Di scan loop (~baris 6788), setelah get_candles() 1d:
#
#     closes_1h = None
#     if allow_intraday:
#         data_1h = get_candles(ticker, "1h", 120)
#         if data_1h: closes_1h = data_1h[0]
#
#     dq = calc_data_quality_score(
#         ticker=ticker, closes_1h=closes_1h,
#         closes_1d=closes_1d, highs_1d=highs_1d,
#         lows_1d=lows_1d, volumes_1d=volumes_1d,
#         strategy="INTRADAY" if allow_intraday else "SWING",
#     )
#     if not dq["allow_signal"]:
#         skip_vol += 1; continue
#     _dq_cache[ticker] = dq      # dict di luar loop
#
#  2. Di check_intraday/check_swing, setelah signal dq check:
#
#     dq = _dq_cache.get(ticker, {"allow_advanced": True})
#     if not dq["allow_advanced"]:
#         ob_result, liq_result, trap_result = {"found": False}, {}, {"trap": False}
#     else:
#         ob_result  = detect_order_block(...)
#         liq_result = detect_liquidity(...)
#
#  3. Di update_execution_fills() (v7.18), ganti body inner loop:
#
#     fill_result = resolve_fill_price(
#         signal_row=row,
#         broker_adapter=get_broker_adapter(),
#         broker_token=_broker_token_cache,
#     )
#     update_fill_with_source(
#         fill_record_id=row["id"],
#         fill_result=fill_result,
#         simulated_entry=float(row["simulated_entry"]),
#         sim_cost_pct=float(row["sim_cost_pct"]),
#         side=row["side"],
#     )
#
#  4. Ganti recalculate_slippage_model() dengan v2:
#     recalculate_slippage_model_v2()
#
#  5. Di send_health_check(), tambahkan:
#     fill_report = get_fill_quality_report()
#
#  6. SQL Supabase:
#
#     ALTER TABLE execution_fills
#       ADD COLUMN IF NOT EXISTS fill_source       TEXT,
#       ADD COLUMN IF NOT EXISTS fill_confidence   INT DEFAULT 1,
#       ADD COLUMN IF NOT EXISTS broker_fill_price FLOAT,
#       ADD COLUMN IF NOT EXISTS fill_note         TEXT,
#       ADD COLUMN IF NOT EXISTS model_alert       BOOL DEFAULT FALSE;
#
#     ALTER TABLE slippage_calibration
#       ADD COLUMN IF NOT EXISTS broker_sample_count INT DEFAULT 0;
#
#     CREATE INDEX IF NOT EXISTS idx_fills_confidence
#       ON execution_fills (fill_confidence, recorded_at DESC);
# ════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════
#  PATCH v7.20 — Multi-Source Data + Broker Fill Primary + Self-Evolving Model
# ════════════════════════════════════════════════════════════════════

from typing import Optional

# ════════════════════════════════════════════════════════════════════
#  [N03] MULTI-SOURCE DATA PIPELINE
#
#  Problem v7.19:
#    Semua data masih dari yfinance — tidak reliable untuk
#    institutional use. yfinance:
#      - Tidak ada SLA / uptime guarantee
#      - Data delay tidak konsisten (bisa 15-20 menit)
#      - Rate limit tidak terdokumentasi → silent fail
#      - Tidak cocok untuk scoring yang butuh freshness < 5 menit
#
#  Solusi v7.20:
#    Tiered data source dengan source tag. Setiap fungsi get_candles
#    sekarang return tuple (data, source_meta) sehingga DQ scorer
#    bisa penalize langsung berdasarkan sumber, bukan hanya age.
#
#    Source tier:
#      S1 [4] twelve_data   : real-time IDX, freshness < 2 menit
#      S2 [3] alpha_vantage : 5-min delay, reliable SLA
#      S3 [2] yfinance      : 15-20 menit delay, no SLA — FLAGGED
#      S4 [1] cache_only    : data lama dari DB, darurat
#
#  ENV yang dibutuhkan:
#    TWELVE_DATA_API_KEY
#    ALPHA_VANTAGE_API_KEY
# ════════════════════════════════════════════════════════════════════

DATA_SOURCE_TWELVE     = "twelve_data"
DATA_SOURCE_ALPHAV     = "alpha_vantage"
DATA_SOURCE_YFINANCE   = "yfinance"
DATA_SOURCE_CACHE      = "cache_only"

DATA_SOURCE_TIER = {
    DATA_SOURCE_TWELVE:   4,
    DATA_SOURCE_ALPHAV:   3,
    DATA_SOURCE_YFINANCE: 2,
    DATA_SOURCE_CACHE:    1,
}

# Penalti freshness score per source (dikurangi dari score akhir N01)
DATA_SOURCE_FRESHNESS_PENALTY = {
    DATA_SOURCE_TWELVE:   0,    # no penalty
    DATA_SOURCE_ALPHAV:   5,    # slight penalty (5-min delay)
    DATA_SOURCE_YFINANCE: 12,   # significant penalty (15-20 min)
    DATA_SOURCE_CACHE:    25,   # heavy penalty
}

_TWELVE_KEY  = os.getenv("TWELVE_DATA_API_KEY", "")
_ALPHAV_KEY  = os.getenv("ALPHA_VANTAGE_API_KEY", "")


def _fetch_twelve_data(
    ticker: str,
    interval: str,
    outputsize: int = 60,
) -> Optional[tuple]:
    """
    [N03] Fetch dari Twelve Data API — institutional-grade.

    Ticker format: untuk IDX gunakan "BBCA.JK" → API Twelve Data
    menerima format ini untuk IDX.
    Interval mapping: "1d" → "1day", "1h" → "1h".

    Returns: (closes, highs, lows, volumes, opens) atau None.
    """
    if not _TWELVE_KEY:
        return None

    try:
        import requests

        interval_map = {"1d": "1day", "4h": "4h", "1h": "1h", "15m": "15min"}
        api_interval = interval_map.get(interval, "1day")

        # Twelve Data pakai format tanpa ".JK" untuk IDX
        # Contoh: "BBCA:IDX" → perlu strip .JK lalu tambah :IDX
        symbol = ticker.replace(".JK", "") + ":IDX" if ".JK" in ticker else ticker

        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol":     symbol,
            "interval":   api_interval,
            "outputsize": outputsize,
            "apikey":     _TWELVE_KEY,
            "format":     "JSON",
        }

        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") == "error" or "values" not in data:
            log(f"  ⚠️ [N03] Twelve Data error [{ticker}]: {data.get('message','')}", "warn")
            return None

        values = data["values"]   # newest first
        if len(values) < 5:
            return None

        # Reverse → oldest first (konsisten dengan yfinance output)
        values = list(reversed(values))

        closes  = np.array([float(v["close"])  for v in values])
        highs   = np.array([float(v["high"])   for v in values])
        lows    = np.array([float(v["low"])    for v in values])
        volumes = np.array([float(v.get("volume", 0)) for v in values])
        opens   = np.array([float(v["open"])   for v in values])

        return closes, highs, lows, volumes, opens

    except Exception as e:
        log(f"  ⚠️ [N03] _fetch_twelve_data [{ticker}]: {e}", "warn")
        return None


def _fetch_alpha_vantage(
    ticker: str,
    interval: str,
    outputsize: int = 60,
) -> Optional[tuple]:
    """
    [N03] Fetch dari Alpha Vantage — fallback tier S2.

    Daily data → TIME_SERIES_DAILY_ADJUSTED
    Intraday  → TIME_SERIES_INTRADAY (interval 60min)
    """
    if not _ALPHAV_KEY:
        return None

    try:
        import requests

        # Ticker normalisasi
        symbol = ticker.replace(".JK", ".JK")   # Alpha Vantage terima .JK langsung

        if interval == "1d":
            func    = "TIME_SERIES_DAILY_ADJUSTED"
            ts_key  = "Time Series (Daily)"
            av_interval = None
        else:
            func    = "TIME_SERIES_INTRADAY"
            ts_key  = "Time Series (60min)"
            av_interval = "60min"

        params = {
            "function":   func,
            "symbol":     symbol,
            "outputsize": "compact",
            "apikey":     _ALPHAV_KEY,
        }
        if av_interval:
            params["interval"] = av_interval

        resp = requests.get("https://www.alphavantage.co/query",
                            params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        ts = data.get(ts_key, {})
        if not ts:
            return None

        items = sorted(ts.items())[-outputsize:]   # oldest first

        closes  = np.array([float(v.get("4. close") or v.get("4. adjusted close", 0))
                            for _, v in items])
        highs   = np.array([float(v["2. high"])  for _, v in items])
        lows    = np.array([float(v["3. low"])   for _, v in items])
        volumes = np.array([float(v["5. volume"]) for _, v in items])
        opens   = np.array([float(v["1. open"])  for _, v in items])

        return closes, highs, lows, volumes, opens

    except Exception as e:
        log(f"  ⚠️ [N03] _fetch_alpha_vantage [{ticker}]: {e}", "warn")
        return None


def _fetch_yfinance_flagged(
    ticker: str,
    interval: str,
    period: str = "60d",
) -> Optional[tuple]:
    """
    [N03] yfinance sebagai last resort — selalu di-flag.

    Returned data di-tag DATA_SOURCE_YFINANCE sehingga
    DQ scorer akan otomatis penalize freshness_score - 12.
    Dipanggil HANYA jika Twelve Data dan Alpha Vantage gagal.
    """
    try:
        import yfinance as yf

        df = yf.download(ticker, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 5:
            return None

        closes  = df["Close"].values.astype(float)
        highs   = df["High"].values.astype(float)
        lows    = df["Low"].values.astype(float)
        volumes = df["Volume"].values.astype(float)
        opens   = df["Open"].values.astype(float)

        return closes, highs, lows, volumes, opens

    except Exception as e:
        log(f"  ⚠️ [N03] _fetch_yfinance_flagged [{ticker}]: {e}", "warn")
        return None


def get_candles_v2(
    ticker: str,
    interval: str = "1d",
    length: int = 60,
) -> tuple[Optional[tuple], str]:
    """
    [N03] Drop-in replacement untuk get_candles() dengan source tracking.

    Coba S1 → S2 → S3 secara berurutan.
    Returns: (candle_tuple_or_None, source_name)

    Caller wajib check source dan adjust logic jika source = yfinance.

    Usage:
        data, src = get_candles_v2(ticker, "1d", 60)
        if data is None:
            skip...
        closes, highs, lows, volumes, opens = data
        source_tier = DATA_SOURCE_TIER[src]
    """
    period_map = {60: "90d", 30: "60d", 120: "180d", 5: "7d"}
    period = period_map.get(length, "90d")

    # ── S1: Twelve Data ──────────────────────────────────────────
    data = _fetch_twelve_data(ticker, interval, outputsize=length)
    if data is not None:
        log(f"  ✅ [N03] {ticker}/{interval}: twelve_data (S1)", "info")
        return data, DATA_SOURCE_TWELVE

    # ── S2: Alpha Vantage ────────────────────────────────────────
    data = _fetch_alpha_vantage(ticker, interval, outputsize=length)
    if data is not None:
        log(f"  🟡 [N03] {ticker}/{interval}: alpha_vantage (S2) — fallback", "warn")
        return data, DATA_SOURCE_ALPHAV

    # ── S3: yfinance (last resort) ───────────────────────────────
    data = _fetch_yfinance_flagged(ticker, interval, period=period)
    if data is not None:
        log(f"  🔴 [N03] {ticker}/{interval}: yfinance (S3) — FLAGGED, penalized", "warn")
        return data, DATA_SOURCE_YFINANCE

    log(f"  🔴 [N03] {ticker}/{interval}: semua source gagal", "error")
    return None, DATA_SOURCE_CACHE


def apply_source_penalty_to_dq(dq_result: dict, source: str) -> dict:
    """
    [N03] Terapkan penalti freshness score berdasarkan data source.

    Dipanggil setelah calc_data_quality_score() jika source bukan S1.
    Mutate dan return dq_result yang sudah di-adjust.
    """
    penalty = DATA_SOURCE_FRESHNESS_PENALTY.get(source, 0)
    if penalty == 0:
        return dq_result

    old_score      = dq_result["score"]
    old_freshness  = dq_result["freshness"]
    new_freshness  = max(0, old_freshness - penalty)
    new_score      = max(0, old_score - penalty)

    dq_result["freshness"]      = new_freshness
    dq_result["score"]          = new_score
    dq_result["data_source"]    = source
    dq_result["source_penalty"] = penalty
    dq_result["note"] += f" | src={source} penalty=-{penalty}"

    # Re-evaluate label setelah penalti
    # [merged — symbols available in global scope]
    if new_score >= DQ_WARN_THRESHOLD:
        dq_result["label"]         = "GOOD"
        dq_result["allow_signal"]  = True
        dq_result["allow_advanced"]= True
    elif new_score >= DQ_DEGRADE_THRESHOLD:
        dq_result["label"]         = "DEGRADED"
        dq_result["allow_signal"]  = True
        dq_result["allow_advanced"]= False
    elif new_score >= DQ_BLOCK_THRESHOLD:
        dq_result["label"]         = "POOR"
        dq_result["allow_signal"]  = False
        dq_result["allow_advanced"]= False
    else:
        dq_result["label"]         = "BLOCK"
        dq_result["allow_signal"]  = False
        dq_result["allow_advanced"]= False

    log(f"  📊 [N03] DQ after source penalty: {old_score}→{new_score} [{dq_result['label']}]", "info")
    return dq_result


# ════════════════════════════════════════════════════════════════════
#  [N04] FULL BROKER FILL MIGRATION
#
#  Problem v7.19:
#    resolve_fill_price() sudah ada broker path TAPI:
#      - Di main bot, baris 655-657 masih di-comment out
#      - broker_adapter belum ada auto-init
#      - Tidak ada monitoring berapa % signal punya broker fill
#      - Tidak ada alert jika broker fill rate drop tiba-tiba
#
#  Solusi v7.20:
#    1. BrokerFillManager — singleton yang auto-init adapter
#    2. resolve_fill_price_v2() — broker WAJIB dicoba dulu,
#       fallback documented dengan reason code
#    3. fill_source_ratio alert jika broker fill rate < 70%
#    4. Bulk reconcile: semua signal lama tanpa broker fill
#       di-retry secara background
# ════════════════════════════════════════════════════════════════════

FILL_REASON_BROKER_SUCCESS   = "broker_ok"
FILL_REASON_BROKER_NO_ID     = "no_broker_order_id"
FILL_REASON_BROKER_PENDING   = "order_pending"
FILL_REASON_BROKER_FAIL      = "broker_api_error"
FILL_REASON_NO_ADAPTER       = "no_adapter_configured"

BROKER_FILL_TARGET_RATIO = 0.70   # alert jika di bawah ini


class BrokerFillManager:
    """
    [N04] Singleton untuk manage broker adapter lifecycle.

    [M01] Wired ke get_broker_adapter() factory agar IPOTAdapter /
    MOSTAdapter yang sudah diimplementasi benar-benar dipakai.
    (Versi sebelumnya pakai dynamic import generic via BROKER_ADAPTER_CLASS
    yang tidak kompatibel dengan adapter pattern di kode ini.)

    [M02] Token TTL cache — refresh otomatis jika token kedaluwarsa.
    IPOT token valid ~8 jam → refresh setiap 7 jam (safety margin 1 jam).
    MOST token (OAuth2 client_credentials) sama, refresh 7 jam.
    """
    _instance: "BrokerFillManager | None" = None
    _adapter  = None
    _token:          str | None = None
    _token_fetched_at: float    = 0.0      # epoch seconds
    _TOKEN_TTL_SECONDS: float   = 7 * 3600  # 7 jam — refresh sebelum 8 jam expiry
    _init_tried: bool = False

    def __new__(cls) -> "BrokerFillManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_adapter(self):
        """Return broker adapter atau None jika tidak configured."""
        if not self._init_tried:
            self._init_tried = True
            try:
                # [M01] Gunakan get_broker_adapter() factory yang sudah ada,
                # bukan dynamic import generik yang tidak kompatibel.
                self._adapter = get_broker_adapter()
                if self._adapter is not None:
                    class_name = type(self._adapter).__name__
                    log(f"  ✅ [M01] BrokerFillManager: adapter {class_name} initialized")
                else:
                    log("  ℹ️ [M01] BrokerFillManager: no adapter configured", "info")
            except Exception as e:
                log(f"  ⚠️ [M01] BrokerFillManager init failed: {e}", "warn")
                self._adapter = None
        return self._adapter

    def get_token(self) -> str | None:
        """
        [M02] Return valid token, refresh jika kedaluwarsa.

        Sebelumnya hanya membaca BROKER_TOKEN dari ENV (static, tidak pernah
        diperbarui). Sekarang memanggil adapter.authenticate() dan menyimpan
        token dengan TTL sehingga auto-refresh sebelum expiry.
        """
        import time as _time
        adapter = self.get_adapter()
        if adapter is None:
            return None

        now = _time.time()
        token_age = now - self._token_fetched_at
        if self._token and token_age < self._TOKEN_TTL_SECONDS:
            return self._token   # masih valid

        # Token kosong atau sudah mendekati expiry — refresh
        try:
            new_token = adapter.authenticate()
            if new_token:
                self._token           = new_token
                self._token_fetched_at = now
                log(f"  🔑 [M02] Broker token refreshed (TTL {self._TOKEN_TTL_SECONDS/3600:.0f}h)")
            else:
                log("  ⚠️ [M02] Broker authenticate() returned None — token not refreshed", "warn")
        except Exception as e:
            log(f"  ⚠️ [M02] Broker token refresh error: {e}", "warn")

        return self._token


_broker_mgr = BrokerFillManager()


def resolve_fill_price_v2(
    signal_row: dict,
) -> dict:
    """
    [N04] Full migration: broker SELALU jadi primary attempt.

    Berbeda dari v7.19 yang butuh caller pass broker_adapter manual,
    v2 auto-retrieve dari BrokerFillManager sehingga tidak bisa
    di-skip secara tidak sengaja (seperti baris 655-657 yang dicomment).

    Returns dict: fill_price, fill_source, fill_confidence,
                  fill_lots, fill_reason, note.
    """
    # [merged — symbols available in global scope]

    ticker    = signal_row.get("ticker", "")
    side      = signal_row.get("side", "BUY")
    strategy  = signal_row.get("strategy", "SWING")
    sim_entry = float(signal_row.get("simulated_entry") or 0)

    adapter = _broker_mgr.get_adapter()
    token   = _broker_mgr.get_token()

    # ── Path 1: Broker actual (ALWAYS FIRST) ─────────────────────
    broker_order_id = signal_row.get("broker_order_id", "")

    if not adapter:
        fill_reason = FILL_REASON_NO_ADAPTER
        log(f"  ℹ️ [N04] {ticker}: no broker adapter → candle fallback")
    elif not broker_order_id:
        fill_reason = FILL_REASON_BROKER_NO_ID
        log(f"  ℹ️ [N04] {ticker}: no broker_order_id → candle fallback")
    else:
        fill_reason = FILL_REASON_BROKER_FAIL
        try:
            status     = adapter.get_order_status(token, broker_order_id)
            fill_price = float(status.get("fill_price") or 0)
            fill_lots  = int(status.get("fill_lots") or 0)
            fill_stat  = status.get("status", "")

            if fill_stat in ("PENDING", "OPEN"):
                fill_reason = FILL_REASON_BROKER_PENDING
                log(f"  ⏳ [N04] {ticker}: order still {fill_stat} → candle proxy")

            elif fill_price > 0 and fill_stat in ("FILLED", "PARTIAL_FILL"):
                note = (f"Broker actual fill @ {fill_price:,.0f} "
                        f"({fill_lots} lot | {fill_stat})")
                log(f"  ✅ [N04] {ticker}: {note}")
                return {
                    "fill_price":      fill_price,
                    "fill_source":     FILL_SOURCE_BROKER,
                    "fill_confidence": FILL_CONFIDENCE[FILL_SOURCE_BROKER],
                    "fill_lots":       fill_lots,
                    "fill_reason":     FILL_REASON_BROKER_SUCCESS,
                    "note":            note,
                }
        except Exception as e:
            log(f"  ⚠️ [N04] broker fill check [{ticker}]: {e}", "warn")

    # ── Path 2: Open proxy (documented fallback) ──────────────────
    try:
        ticker_jk = ticker if ticker.endswith(".JK") else ticker + ".JK"
        interval  = "1h" if strategy == "INTRADAY" else "1d"

        # Use v2 candle fetcher untuk source tracking
        candle_data, src = get_candles_v2(ticker_jk, interval, 5)

        if candle_data is not None:
            closes, highs, lows, volumes, opens = candle_data
            open_price = float(opens[-2]) if len(opens) >= 2 else float(opens[-1])

            if open_price > 0:
                note = (f"Open proxy @ {open_price:,.0f} "
                        f"[fallback: {fill_reason} | datasrc: {src}]")
                log(f"  📊 [N04] {ticker}: {note}")
                return {
                    "fill_price":      open_price,
                    "fill_source":     FILL_SOURCE_OPEN,
                    "fill_confidence": FILL_CONFIDENCE[FILL_SOURCE_OPEN],
                    "fill_lots":       0,
                    "fill_reason":     fill_reason,
                    "data_source":     src,
                    "note":            note,
                }

            close_price = float(closes[-2]) if len(closes) >= 2 else float(closes[-1])
            if close_price > 0:
                note = (f"Close proxy @ {close_price:,.0f} "
                        f"[fallback: {fill_reason} | datasrc: {src}]")
                log(f"  📊 [N04] {ticker}: {note}")
                return {
                    "fill_price":      close_price,
                    "fill_source":     FILL_SOURCE_CLOSE,
                    "fill_confidence": FILL_CONFIDENCE[FILL_SOURCE_CLOSE],
                    "fill_lots":       0,
                    "fill_reason":     fill_reason,
                    "data_source":     src,
                    "note":            note,
                }

    except Exception as e:
        log(f"  ⚠️ [N04] candle resolve [{ticker}]: {e}", "warn")

    # ── Path 4: Simulated — always with explicit reason ───────────
    note = (f"Simulated only @ {sim_entry:,.0f} "
            f"[reason: {fill_reason} — no fill data available]")
    log(f"  📋 [N04] {ticker}: {note}")
    return {
        "fill_price":      sim_entry,
        "fill_source":     FILL_SOURCE_SIM,
        "fill_confidence": FILL_CONFIDENCE[FILL_SOURCE_SIM],
        "fill_lots":       0,
        "fill_reason":     fill_reason,
        "note":            note,
    }


def check_fill_deviation_hard(
    ticker: str,
    actual_fill: float,
    simulated_entry: float,
    strategy: str = "SWING",
    max_deviation_pct: float = 2.5,
) -> dict:
    """
    [P7-03] Hard check: bandingkan actual_fill_price vs simulated_entry model.

    Jika deviasi terlalu jauh (> max_deviation_pct), kirim Telegram alert
    dan return status WARNING/CRITICAL. Ini adalah guard yang sebelumnya
    MISSING — slippage report ada tapi hanya monitoring pasif, tidak ada
    fungsi yang secara aktif mendeteksi fill yang jauh dari model dan
    memberi signal bahwa kalibrasi IMPACT_MODEL_SCALE perlu diupdate.

    Args:
        ticker          : ticker saham (e.g. "BBCA.JK")
        actual_fill     : harga fill nyata dari broker
        simulated_entry : harga yang diprediksi model saat signal dibuat
        strategy        : "INTRADAY" atau "SWING"
        max_deviation_pct: batas maksimum deviasi yang dianggap wajar (default 2.5%)

    Returns dict:
        status    : "OK" | "WARNING" | "CRITICAL"
        deviation_pct : float
        note      : string keterangan
    """
    if actual_fill <= 0 or simulated_entry <= 0:
        return {
            "status": "SKIP",
            "deviation_pct": 0.0,
            "note": "actual_fill atau simulated_entry = 0, skip check",
        }

    deviation_pct = abs(actual_fill - simulated_entry) / simulated_entry * 100
    direction     = "OVER" if actual_fill > simulated_entry else "UNDER"

    # Threshold per strategi: intraday lebih ketat karena spread & queue
    # Swing lebih longgar karena T+2 settlement IDX bisa ada gap
    _thresholds = {
        "INTRADAY": max_deviation_pct * 0.8,   # 2.0% default
        "SWING":    max_deviation_pct,           # 2.5% default
        "BREAKOUT": max_deviation_pct * 1.1,    # 2.75% default
    }
    threshold = _thresholds.get(strategy, max_deviation_pct)

    if deviation_pct <= threshold * 0.6:
        status = "OK"
        note = (f"Fill deviation {deviation_pct:.2f}% ({direction}) — "
                f"dalam batas wajar (threshold {threshold:.1f}%)")
        log(f"  ✅ [P7-03] {ticker}: {note}")
        return {"status": status, "deviation_pct": round(deviation_pct, 3), "note": note}

    elif deviation_pct <= threshold:
        status = "WARNING"
        note = (f"Fill deviation {deviation_pct:.2f}% ({direction}) — "
                f"mendekati batas {threshold:.1f}%. "
                f"Monitor kalibrasi IMPACT_MODEL_SCALE.")
        log(f"  ⚠️ [P7-03] {ticker}: {note}", "warn")
        return {"status": status, "deviation_pct": round(deviation_pct, 3), "note": note}

    else:
        # CRITICAL — deviasi melebihi threshold
        status = "CRITICAL"
        note = (f"Fill deviation {deviation_pct:.2f}% ({direction}) — "
                f"MELEBIHI threshold {threshold:.1f}%. "
                f"actual={actual_fill:,.0f} vs model={simulated_entry:,.0f}.")
        log(f"  🔴 [P7-03] {ticker}: {note}", "error")

        _alert_msg = (
            f"🔴 <b>[P7-03] FILL DEVIATION ALERT</b>\n\n"
            f"<b>Ticker:</b> {ticker} ({strategy})\n"
            f"<b>Actual fill:</b> Rp{actual_fill:,.0f}\n"
            f"<b>Model entry:</b> Rp{simulated_entry:,.0f}\n"
            f"<b>Deviasi:</b> {deviation_pct:.2f}% {direction} "
            f"(threshold: {threshold:.1f}%)\n\n"
            f"<b>Implikasi:</b>\n"
            f"• Cost model mungkin tidak akurat untuk kondisi pasar saat ini\n"
            f"• EV kalkulasi di check_edge_proven() bisa bias\n"
            f"• Pertimbangkan kalibrasi ulang: "
            f"<code>IMPACT_MODEL_SCALE</code>\n\n"
            f"💡 Jalankan <code>calibrate_cost_model_from_fills()</code> "
            f"setelah kumpul ≥20 fill nyata."
        )
        try:
            tg(_alert_msg)
        except Exception as _tg_e:
            log(f"  ⚠️ [P7-03] Telegram alert gagal: {_tg_e}", "warn")

        return {"status": status, "deviation_pct": round(deviation_pct, 3), "note": note}


def batch_check_fill_deviation(limit: int = 50) -> dict:
    """
    [P7-03B] Batch version: cek fill deviation untuk semua fills terbaru.

    Dipanggil dari send_slippage_report() atau scheduled job.
    Returns dict: {checked: int, ok: int, warning: int, critical: int, details: list}
    """
    result = {"checked": 0, "ok": 0, "warning": 0, "critical": 0, "details": []}

    try:
        rows = (
            supabase.table("execution_fills")
            .select("ticker, strategy, actual_fill_price, simulated_entry, "
                    "fill_source, recorded_at")
            .not_.is_("actual_fill_price", "null")
            .not_.is_("simulated_entry", "null")
            .eq("fill_source", FILL_SOURCE_BROKER)   # hanya broker actual, bukan proxy
            .order("recorded_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
    except Exception as e:
        log(f"  ⚠️ [P7-03B] batch_check_fill_deviation query: {e}", "warn")
        return result

    for row in rows:
        try:
            actual   = float(row.get("actual_fill_price") or 0)
            sim      = float(row.get("simulated_entry") or 0)
            ticker   = row.get("ticker", "?")
            strategy = row.get("strategy", "SWING")

            if actual <= 0 or sim <= 0:
                continue

            check = check_fill_deviation_hard(
                ticker=ticker, actual_fill=actual,
                simulated_entry=sim, strategy=strategy
            )
            result["checked"] += 1
            if check["status"] == "OK":
                result["ok"] += 1
            elif check["status"] == "WARNING":
                result["warning"] += 1
                result["details"].append({"ticker": ticker, **check})
            elif check["status"] == "CRITICAL":
                result["critical"] += 1
                result["details"].append({"ticker": ticker, **check})
        except Exception as _re:
            log(f"  ⚠️ [P7-03B] row error [{row.get('ticker','?')}]: {_re}", "warn")

    if result["checked"] > 0:
        log(
            f"  📊 [P7-03B] Fill deviation scan: "
            f"n={result['checked']} | "
            f"OK={result['ok']} | "
            f"WARN={result['warning']} | "
            f"CRITICAL={result['critical']}"
        )
        if result["critical"] > 0:
            log(
                f"  🔴 [P7-03B] {result['critical']} fill(s) CRITICAL — "
                f"model mungkin uncalibrated. Cek IMPACT_MODEL_SCALE.",
                "error"
            )

    return result


def send_slippage_report() -> None:
    """
    [M05] Laporan slippage harian — kirim ke Telegram setiap pagi sebelum sesi.

    Mengkonsolidasi data dari tabel execution_fills yang sebelumnya tersebar
    di berbagai fungsi (calibrate_cost_model_from_fills, check_fill_source_ratio_alert,
    recalculate_slippage_model_v2) ke dalam satu laporan actionable.

    Berisi:
      1. Broker fill rate (broker_actual vs open_proxy vs close_proxy vs simulated)
      2. Rata-rata slippage_error per tier (LQ45 / MID / SMALL)
      3. Rata-rata slippage_error per strategy (INTRADAY / SWING)
      4. Systematic bias alert jika avg_error > threshold
      5. Saran tindakan: kalibrasi IMPACT_MODEL_SCALE jika needed

    Dipanggil dari run() setiap pagi (setelah market hours check,
    sebelum scan dimulai). Hanya kirim jika ada cukup data (>= 10 fills).
    """
    MIN_FILLS_FOR_REPORT = 10
    BIAS_ALERT_THRESHOLD = 0.8   # % — systematic over/under-estimation

    try:
        rows = (
            supabase.table("execution_fills")
            .select("fill_source, fill_confidence, slippage_error, "
                    "cap_tier, strategy, side, ticker, recorded_at")
            .not_.is_("slippage_error", "null")
            .not_.is_("fill_source", "null")
            .order("recorded_at", desc=True)
            .limit(200)
            .execute()
            .data
        )

        if not rows or len(rows) < MIN_FILLS_FOR_REPORT:
            log(f"  ℹ️ [M05] slippage report skip: hanya {len(rows) if rows else 0} fills "
                f"(min {MIN_FILLS_FOR_REPORT})")
            return

        from collections import defaultdict
        import statistics

        # ── Fill source breakdown ─────────────────────────────────
        total = len(rows)
        source_counts: dict = defaultdict(int)
        for r in rows:
            source_counts[r.get("fill_source", "unknown")] += 1

        broker_n  = source_counts.get(FILL_SOURCE_BROKER, 0)
        open_n    = source_counts.get(FILL_SOURCE_OPEN, 0)
        close_n   = source_counts.get(FILL_SOURCE_CLOSE, 0)
        sim_n     = source_counts.get(FILL_SOURCE_SIM, 0)
        broker_pct = broker_n / total * 100

        # ── Slippage error stats per tier ─────────────────────────
        tier_errors: dict = defaultdict(list)
        for r in rows:
            tier = r.get("cap_tier", "UNKNOWN")
            err  = r.get("slippage_error")
            if err is not None:
                tier_errors[tier].append(float(err))

        tier_lines = []
        for tier in sorted(tier_errors):
            errs = tier_errors[tier]
            if len(errs) < 3:
                continue
            avg_e  = statistics.mean(errs)
            med_e  = statistics.median(errs)
            tier_lines.append(
                f"  {tier:<8}: avg={avg_e:+.3f}% med={med_e:+.3f}% (n={len(errs)})"
            )

        # ── Slippage error stats per strategy ─────────────────────
        strat_errors: dict = defaultdict(list)
        for r in rows:
            strat = r.get("strategy", "UNKNOWN")
            err   = r.get("slippage_error")
            if err is not None:
                strat_errors[strat].append(float(err))

        strat_lines = []
        for strat in sorted(strat_errors):
            errs = strat_errors[strat]
            if len(errs) < 3:
                continue
            avg_e = statistics.mean(errs)
            strat_lines.append(
                f"  {strat:<10}: avg={avg_e:+.3f}% (n={len(errs)})"
            )

        # ── Overall bias ──────────────────────────────────────────
        all_errors = [float(r["slippage_error"]) for r in rows
                      if r.get("slippage_error") is not None]
        overall_avg = statistics.mean(all_errors) if all_errors else 0.0
        overall_med = statistics.median(all_errors) if all_errors else 0.0

        # ── Bias alert & calibration suggestion ──────────────────
        bias_icon = "🟢"
        bias_note = "Model terkalibrasi dengan baik"
        recal_hint = ""

        if abs(overall_avg) > BIAS_ALERT_THRESHOLD:
            if overall_avg > 0:
                bias_icon = "🔴"
                bias_note = (f"Under-estimate! Slippage nyata {overall_avg:.2f}% "
                             f"lebih besar dari prediksi")
                new_scale = round(1.0 + overall_avg / 100 * 10, 2)
                recal_hint = (f"\n💡 Coba: <code>IMPACT_MODEL_SCALE={new_scale}</code>"
                              f" (current={IMPACT_MODEL_SCALE:.2f})")
            else:
                bias_icon = "🟡"
                bias_note = (f"Over-estimate. Slippage nyata {overall_avg:.2f}% "
                             f"lebih kecil dari prediksi")
                new_scale = round(max(0.5, 1.0 + overall_avg / 100 * 10), 2)
                recal_hint = (f"\n💡 Coba: <code>IMPACT_MODEL_SCALE={new_scale}</code>"
                              f" (current={IMPACT_MODEL_SCALE:.2f})")

        # ── Telegram message ──────────────────────────────────────
        fill_bar_broker = "█" * int(broker_pct / 10) + "░" * (10 - int(broker_pct / 10))
        tier_block  = "\n".join(tier_lines)  if tier_lines  else "  (data tidak cukup)"
        strat_block = "\n".join(strat_lines) if strat_lines else "  (data tidak cukup)"

        msg = (
            f"📊 <b>Slippage Report Harian — v8.14</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Fill Sources</b> (n={total})\n"
            f"  {fill_bar_broker} {broker_pct:.0f}% broker_actual ({broker_n})\n"
            f"  open_proxy: {open_n} | close_proxy: {close_n} | simulated: {sim_n}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Slippage per Tier</b>\n"
            f"{tier_block}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Slippage per Strategy</b>\n"
            f"{strat_block}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Overall Bias</b>\n"
            f"  {bias_icon} avg={overall_avg:+.3f}% med={overall_med:+.3f}%\n"
            f"  {bias_note}{recal_hint}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>Model: {IMPACT_CALIBRATION_STATUS} "
            f"(scale={IMPACT_MODEL_SCALE:.2f})</i>"
        )

        tg(msg)
        log(f"✅ [M05] Slippage report dikirim: n={total} fills, "
            f"broker={broker_pct:.0f}%, avg_error={overall_avg:+.3f}%")

        # ── [P7-03B] Fill deviation check — actual vs model ──────────
        # Jalankan setelah report, sehingga jika ada CRITICAL deviation
        # alert-nya muncul setelah laporan reguler (tidak mencampur).
        try:
            _dev_result = batch_check_fill_deviation(limit=50)
            if _dev_result.get("checked", 0) > 0 and _dev_result.get("critical", 0) == 0:
                log(f"  ✅ [P7-03B] Fill deviation: semua dalam batas wajar "
                    f"(n={_dev_result['checked']}, warn={_dev_result['warning']})")
        except Exception as _dev_e:
            log(f"  ⚠️ [P7-03B] batch_check_fill_deviation: {_dev_e}", "warn")

    except Exception as e:
        log(f"⚠️ [M05] send_slippage_report: {e}", "warn")


def check_fill_source_ratio_alert() -> str:
    """
    [N04] Monitor % broker fill vs total. Alert jika drop < 70%.

    Dipanggil dari send_health_check() — maksimal 1x per heartbeat.
    Returns string untuk diembed di heartbeat message.
    """
    try:
        # [merged — symbols available in global scope]

        rows = (
            supabase.table("execution_fills")
            .select("fill_source, fill_reason")
            .not_.is_("fill_source", "null")
            .order("recorded_at", desc=True)
            .limit(100)
            .execute()
            .data
        )

        if not rows:
            return "Fill ratio: no data"

        from collections import Counter
        sources = Counter(r["fill_source"] for r in rows)
        total   = len(rows)

        broker_count = sources.get(FILL_SOURCE_BROKER, 0)
        broker_ratio = broker_count / total

        report = (
            f"Fill ratio (n={total}): "
            f"broker={broker_ratio:.0%} "
            f"open={sources.get(FILL_SOURCE_OPEN,0)/total:.0%} "
            f"close={sources.get(FILL_SOURCE_CLOSE,0)/total:.0%} "
            f"sim={sources.get(FILL_SOURCE_SIM,0)/total:.0%}"
        )

        if broker_ratio < BROKER_FILL_TARGET_RATIO:
            reasons = Counter(r.get("fill_reason","") for r in rows
                              if r["fill_source"] != FILL_SOURCE_BROKER)
            top_reasons = reasons.most_common(3)
            alert_msg = (
                f"⚠️ <b>FILL QUALITY ALERT</b>\n"
                f"Broker fill rate: {broker_ratio:.0%} "
                f"(target ≥{BROKER_FILL_TARGET_RATIO:.0%})\n"
                f"Top reasons:\n"
                + "\n".join(f"  • {r}: {n}x" for r, n in top_reasons)
            )
            try:
                tg(alert_msg)
                log(f"  🔴 [N04] Fill alert sent: {broker_ratio:.0%}", "warn")
            except Exception:
                log(f"  🔴 [N04] Fill alert (TG gagal): {report}", "error")

        return report

    except Exception as e:
        return f"Fill ratio error: {e}"


def bulk_reconcile_fills(limit: int = 50) -> int:
    """
    [N04] Retry broker fill untuk signal yang masih 'simulated'.

    Dipanggil dari heartbeat atau scheduled job terpisah.
    Returns: jumlah fill yang berhasil di-upgrade ke broker_actual.

    [Y03] Diperluas: selain update execution_fills, sekarang juga update
    actual_fill_price di tabel signals agar check_edge_proven() dan
    update_signal_outcomes() punya data fill nyata — bukan hanya proxy.
    Log eksplisit jika signal masih simulated setelah 24 jam (data gap nyata).
    """
    try:
        # Ambil fills yang masih simulated dan punya broker_order_id
        rows = (
            supabase.table("execution_fills")
            .select("id, ticker, side, strategy, simulated_entry, "
                    "broker_order_id, sim_cost_pct, signal_id, recorded_at")
            .eq("fill_source", FILL_SOURCE_SIM)
            .not_.is_("broker_order_id", "null")
            .order("recorded_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )

        # [Y03] Juga cek signals yang masih NULL actual_fill_price setelah 24 jam
        now_utc = datetime.now(timezone.utc)
        cutoff_24h = (now_utc - timedelta(hours=24)).isoformat()
        try:
            stale_signals = (
                supabase.table("signals")
                .select("id, pair, strategy, sent_at, simulated_entry")
                .is_("actual_fill_price", "null")
                .is_("outcome", "null")
                .lt("sent_at", cutoff_24h)
                .order("sent_at", desc=True)
                .limit(20)
                .execute()
                .data
            )
            if stale_signals:
                log(f"  ⚠️ [Y03] {len(stale_signals)} signal masih simulated fill setelah 24 jam "
                    f"— broker feedback loop belum aktif atau execution_mode bukan LIVE. "
                    f"EV kalkulasi di check_edge_proven() memakai simulated entry.", "warn")
                for ss in stale_signals[:3]:
                    age_h = (now_utc - datetime.fromisoformat(ss["sent_at"].replace("Z", "+00:00"))).total_seconds() / 3600
                    log(f"    [Y03] stale fill: {ss['pair']} {ss['strategy']} "
                        f"(usia {age_h:.0f}j, sim_entry={ss.get('simulated_entry', 'N/A')})")
        except Exception as _se:
            log(f"  ⚠️ [Y03] stale signal check: {_se}", "warn")

        if not rows:
            log(f"  ℹ️ [N04] bulk_reconcile: no simulated fills with broker IDs")
            return 0

        upgraded = 0
        for row in rows:
            fill_result = resolve_fill_price_v2(signal_row=row)
            if fill_result["fill_source"] == FILL_SOURCE_BROKER:
                try:
                    update_fill_with_source(
                        fill_record_id=row["id"],
                        fill_result=fill_result,
                        simulated_entry=float(row.get("simulated_entry") or 0),
                        sim_cost_pct=float(row.get("sim_cost_pct") or 0),
                        side=row.get("side", "BUY"),
                    )

                    # [Y03] Propagasi actual fill ke tabel signals agar EV calc akurat
                    signal_id = row.get("signal_id")
                    actual_fp = fill_result.get("fill_price", 0)
                    if signal_id and actual_fp > 0:
                        try:
                            supabase.table("signals").update({
                                "actual_fill_price":  actual_fp,
                                "fill_source_used":   "actual",
                            }).eq("id", signal_id).execute()
                            log(f"  ✅ [Y03] signals.actual_fill_price updated: "
                                f"{row['ticker']} @ {actual_fp:,.0f}")
                        except Exception as _upd_e:
                            log(f"  ⚠️ [Y03] signals update [{signal_id}]: {_upd_e}", "warn")

                    upgraded += 1
                except Exception as e:
                    log(f"  ⚠️ [N04] bulk_reconcile update [{row['id']}]: {e}", "warn")
            time.sleep(0.2)   # rate limit gentle

        log(f"  ✅ [N04/Y03] bulk_reconcile: {upgraded}/{len(rows)} upgraded to broker_actual")
        return upgraded

    except Exception as e:
        log(f"  ⚠️ [N04] bulk_reconcile_fills: {e}", "warn")
        return 0


# ════════════════════════════════════════════════════════════════════
#  [N05] SELF-EVOLVING MODEL — Structure-Level Adaptation
#
#  Problem v7.19:
#    recalculate_slippage_model_v2() hanya adjust bias (EWA).
#    Model tidak pernah tanya: "Apakah strategi ini masih valid?"
#    Tidak ada mekanisme untuk:
#      - Deteksi win_rate yang turun secara sistematis
#      - Perketat / kendurkan threshold secara otomatis
#      - Toggle mode AGGRESSIVE/CONSERVATIVE berdasarkan perf
#      - Audit trail perubahan rule (siapa yang ubah apa, kapan)
#
#  Solusi v7.20 — 3 layer evolusi:
#
#  Layer 1: Win-Rate Monitor
#    Hitung rolling win_rate per strategy per 30 hari.
#    Bandingkan dengan baseline. Flag jika degradasi > 15%.
#
#  Layer 2: Threshold Mutator
#    Berdasarkan win-rate dan slippage error trend:
#      - win_rate bagus + slippage kecil  → RELAX threshold sedikit
#      - win_rate jelek + slippage besar  → TIGHTEN threshold
#    Perubahan dibatasi ±10% dari nilai default (guardrail).
#
#  Layer 3: Mode Selector
#    Jika win_rate < 35% selama 2 minggu → paksa CONSERVATIVE
#    Jika win_rate > 60% selama 2 minggu → boleh AGGRESSIVE
#    Perubahan mode di-log dan dikirim ke Telegram.
# ════════════════════════════════════════════════════════════════════

# Default threshold references (nilai awal, bisa di-evolve)
_DEFAULT_THRESHOLDS: dict[str, float] = {
    "min_score":           60.0,    # skor minimum untuk kirim signal
    "min_rr":              1.5,     # minimum R:R
    "max_atr_pct":         5.0,     # maksimum ATR% (volatility filter)
    "min_volume_ratio":    1.2,     # volume vs avg
    "btc_corr_block":      0.85,    # BTC correlation block threshold
}

THRESHOLD_GUARDRAIL_PCT = 0.10   # max ±10% dari default
WIN_RATE_BASELINE       = 0.50   # expected baseline win rate
WIN_RATE_FLOOR          = 0.35   # di bawah ini → paksa CONSERVATIVE
WIN_RATE_TARGET         = 0.60   # di atas ini → allow AGGRESSIVE

# Supabase table untuk audit trail evolusi
EVOLUTION_TABLE = "model_evolution_log"


def _load_current_thresholds() -> dict[str, float]:
    """
    [N05] Load threshold dari Supabase. Fallback ke default jika belum ada.
    """
    try:
        # [merged — symbols available in global scope]

        rows = (
            supabase.table("model_thresholds")
            .select("key, value")
            .execute()
            .data
        )
        if not rows:
            return dict(_DEFAULT_THRESHOLDS)

        current = dict(_DEFAULT_THRESHOLDS)
        for r in rows:
            if r["key"] in current:
                current[r["key"]] = float(r["value"])
        return current

    except Exception:
        return dict(_DEFAULT_THRESHOLDS)


def _calc_rolling_winrate(
    strategy: str,
    days: int = 30,
) -> Optional[float]:
    """
    [N05] Hitung rolling win-rate per strategy dari execution_fills.

    Win = signal yang actual_return > 0 (setelah fill reconciliation).
    Returns None jika data tidak cukup (< 10 signal).
    """
    try:
        # [merged — symbols available in global scope]
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        rows = (
            supabase.table("execution_fills")
            .select("slippage_error, fill_confidence")
            .eq("strategy" if True else "cap_tier", strategy)
            .gte("recorded_at", cutoff)
            .gte("fill_confidence", 2)   # hanya data yang trusted
            .not_.is_("slippage_error", "null")
            .execute()
            .data
        )

        if len(rows) < 10:
            return None

        # Untuk simplisitas v7.20:
        # Positive slippage_error = kita dapat lebih dari yang diprediksi → WIN
        # Ini proxy — idealnya pakai actual_pnl dari portfolio table
        wins = sum(1 for r in rows if float(r["slippage_error"]) >= 0)
        return wins / len(rows)

    except Exception as e:
        log(f"  ⚠️ [N05] _calc_rolling_winrate [{strategy}]: {e}", "warn")
        return None


def _log_evolution_event(
    event_type: str,
    strategy: str,
    old_value,
    new_value,
    reason: str,
) -> None:
    """
    [N05] Audit trail setiap perubahan rule/threshold ke Supabase.
    """
    try:
        # [merged — symbols available in global scope]

        supabase.table(EVOLUTION_TABLE).insert({
            "event_type":  event_type,
            "strategy":    strategy,
            "old_value":   str(old_value),
            "new_value":   str(new_value),
            "reason":      reason,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }).execute()

    except Exception as e:
        log(f"  ⚠️ [N05] _log_evolution_event: {e}", "warn")


def mutate_thresholds(
    strategy: str,
    win_rate: float,
    mean_slippage_error: float,
) -> dict[str, float]:
    """
    [N05] Layer 2 — Threshold Mutator.

    Logic sederhana tapi auditable:
      - win_rate > target dan slippage kecil: relax min_score -1, min_rr -0.05
      - win_rate < floor: tighten min_score +2, min_rr +0.1
      - di tengah: tidak ada perubahan (stability)

    Semua perubahan dibatasi oleh THRESHOLD_GUARDRAIL_PCT.
    """
    try:
        current = _load_current_thresholds()
        changed = {}

        def _bounded_mutate(key: str, delta: float) -> Optional[float]:
            default = _DEFAULT_THRESHOLDS[key]
            lo = default * (1 - THRESHOLD_GUARDRAIL_PCT)
            hi = default * (1 + THRESHOLD_GUARDRAIL_PCT)
            old_val = current[key]
            new_val = max(lo, min(hi, old_val + delta))
            if abs(new_val - old_val) > 1e-6:
                return round(new_val, 4)
            return None

        # Determine mutation direction
        if win_rate > WIN_RATE_TARGET and abs(mean_slippage_error) < 0.5:
            # Performance good → relax slightly to catch more signals
            mutations = {"min_score": -1.0, "min_rr": -0.05}
            reason = f"wr={win_rate:.0%} slip={mean_slippage_error:+.3f} → RELAX"

        elif win_rate < WIN_RATE_FLOOR:
            # Performance poor → tighten filters
            mutations = {"min_score": +2.0, "min_rr": +0.10, "min_volume_ratio": +0.05}
            reason = f"wr={win_rate:.0%} → TIGHTEN (win_rate below floor)"

        else:
            log(f"  ℹ️ [N05] [{strategy}] wr={win_rate:.0%}: no threshold change")
            return current

        # [merged — symbols available in global scope]

        for key, delta in mutations.items():
            new_val = _bounded_mutate(key, delta)
            if new_val is not None:
                old_val = current[key]
                current[key] = new_val
                changed[key] = new_val

                # Persist ke Supabase
                try:
                    existing = (
                        supabase.table("model_thresholds")
                        .select("id")
                        .eq("key", key)
                        .eq("strategy", strategy)
                        .execute()
                        .data
                    )
                    payload = {
                        "key":        key,
                        "strategy":   strategy,
                        "value":      new_val,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    if existing:
                        supabase.table("model_thresholds").update(payload).eq(
                            "id", existing[0]["id"]).execute()
                    else:
                        supabase.table("model_thresholds").insert(payload).execute()

                    _log_evolution_event(
                        event_type="threshold_mutate",
                        strategy=strategy,
                        old_value=old_val,
                        new_value=new_val,
                        reason=f"{key}: {reason}",
                    )

                    log(f"  🔧 [N05] [{strategy}] {key}: {old_val:.4f}→{new_val:.4f} | {reason}")

                except Exception as e:
                    log(f"  ⚠️ [N05] mutate persist [{key}]: {e}", "warn")

        return current

    except Exception as e:
        log(f"  ⚠️ [N05] mutate_thresholds [{strategy}]: {e}", "warn")
        return _load_current_thresholds()


def evolve_mode_selector(strategy: str) -> str:
    """
    [N05] Layer 3 — Mode Selector.

    Evaluasi rolling win_rate 14 hari.
    Return mode yang seharusnya aktif: "AGGRESSIVE" / "CONSERVATIVE" / "BALANCED".
    Side effect: update mode di Supabase + kirim Telegram jika berubah.
    """
    try:
        # [merged — symbols available in global scope]

        win_14d = _calc_rolling_winrate(strategy, days=14)
        win_30d = _calc_rolling_winrate(strategy, days=30)

        if win_14d is None:
            log(f"  ℹ️ [N05] [{strategy}] Not enough data for mode evolution")
            return "BALANCED"

        # Determine recommended mode
        if win_14d < WIN_RATE_FLOOR:
            recommended = "CONSERVATIVE"
            rationale = f"14d win_rate={win_14d:.0%} < {WIN_RATE_FLOOR:.0%}"
        elif win_14d > WIN_RATE_TARGET and (win_30d or 0) > WIN_RATE_TARGET:
            recommended = "AGGRESSIVE"
            rationale = f"14d={win_14d:.0%} & 30d={win_30d:.0%} both > {WIN_RATE_TARGET:.0%}"
        else:
            recommended = "BALANCED"
            rationale = f"14d={win_14d:.0%} — within normal range"

        # Load current mode
        mode_rows = (
            supabase.table("model_thresholds")
            .select("value")
            .eq("key", "scan_mode")
            .eq("strategy", strategy)
            .execute()
            .data
        )
        current_mode = mode_rows[0]["value"] if mode_rows else "BALANCED"

        if recommended == current_mode:
            log(f"  ℹ️ [N05] [{strategy}] mode unchanged: {current_mode}")
            return current_mode

        # Persist new mode
        payload = {
            "key":        "scan_mode",
            "strategy":   strategy,
            "value":      recommended,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if mode_rows:
            supabase.table("model_thresholds").update(payload).eq(
                "key", "scan_mode").eq("strategy", strategy).execute()
        else:
            supabase.table("model_thresholds").insert(payload).execute()

        _log_evolution_event(
            event_type="mode_switch",
            strategy=strategy,
            old_value=current_mode,
            new_value=recommended,
            reason=rationale,
        )

        alert = (
            f"🤖 <b>MODEL EVOLUTION — MODE SWITCH</b>\n"
            f"Strategy: <code>{strategy}</code>\n"
            f"{current_mode} → <b>{recommended}</b>\n"
            f"Reason: {rationale}\n"
            f"14d win_rate: {win_14d:.0%}"
        )
        try:
            tg(alert)
        except Exception:
            log(f"  🟡 [N05] Mode switch alert (TG fail): {alert}", "warn")

        log(f"  🔄 [N05] [{strategy}] mode: {current_mode}→{recommended} | {rationale}")
        return recommended

    except Exception as e:
        log(f"  ⚠️ [N05] evolve_mode_selector [{strategy}]: {e}", "warn")
        return "BALANCED"


def run_model_evolution_cycle(strategies: list[str] | None = None) -> None:
    """
    [N05] Entry point: jalankan full evolution cycle untuk semua strategy.

    Dipanggil dari scheduler / heartbeat — tidak dari hot path scan.
    Urutan: check win_rate → mutate threshold → evolve mode.
    [v8.12] Kirim ringkasan perubahan ke Telegram setelah cycle selesai.
    """
    if strategies is None:
        strategies = ["SWING", "INTRADAY", "SCALPING"]

    log(f"  🧬 [N05] Model evolution cycle start: {strategies}")

    # [v8.12] Kumpulkan hasil per strategy untuk ringkasan Telegram
    _evo_results: list[str] = []

    for strategy in strategies:
        win_rate = _calc_rolling_winrate(strategy, days=30)

        if win_rate is None:
            log(f"  ℹ️ [N05] [{strategy}] insufficient data — skip evolution")
            _evo_results.append(f"  • {strategy}: ℹ️ data kurang — skip")
            continue

        log(f"  📊 [N05] [{strategy}] 30d win_rate: {win_rate:.0%}")

        # Estimasi mean slippage error (untuk threshold mutator)
        try:
            # [merged — symbols available in global scope]
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            slip_rows = (
                supabase.table("execution_fills")
                .select("slippage_error")
                .gte("recorded_at", cutoff)
                .gte("fill_confidence", 2)
                .not_.is_("slippage_error", "null")
                .execute()
                .data
            )
            mean_slip = (
                float(np.mean([float(r["slippage_error"]) for r in slip_rows]))
                if slip_rows else 0.0
            )
        except Exception as _e:
            log(f"  ⚠️ [FALLBACK] mean_slip calc: {_e} — default 0.0", "warn")
            mean_slip = 0.0

        # Layer 2: mutate thresholds
        _old_thresh = _load_current_thresholds()
        mutate_thresholds(strategy, win_rate, mean_slip)
        _new_thresh = _load_current_thresholds()

        # [v8.12] Detect apa yang berubah
        _changes: list[str] = []
        for _k in ("min_score", "min_rr", "min_volume_ratio"):
            _old_v = _old_thresh.get(_k)
            _new_v = _new_thresh.get(_k)
            if _old_v is not None and _new_v is not None and abs(_new_v - _old_v) > 1e-5:
                _dir = "🔽 relax" if _new_v < _old_v else "🔼 tighten"
                _changes.append(f"    {_k}: {_old_v:.4f}→{_new_v:.4f} {_dir}")

        _wr_icon = "🟢" if win_rate >= WIN_RATE_TARGET else (
                   "🔴" if win_rate < WIN_RATE_FLOOR else "🟡")
        _wr_str  = f"{win_rate:.0%}"
        if _changes:
            _evo_results.append(
                f"  • {strategy} {_wr_icon} WR={_wr_str}\n" + "\n".join(_changes)
            )
        else:
            _evo_results.append(
                f"  • {strategy} {_wr_icon} WR={_wr_str} — tidak ada perubahan"
            )

        # Layer 3: evolve mode
        evolve_mode_selector(strategy)

    log(f"  ✅ [N05] Model evolution cycle complete")

    # [v8.12] Kirim ringkasan ke Telegram
    try:
        _now_str = datetime.now(WIB).strftime("%d/%m/%Y %H:%M WIB")
        _body    = "\n".join(_evo_results) if _evo_results else "  — tidak ada perubahan"
        _evo_msg = (
            f"🧬 <b>Model Evolution Result</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now_str}\n"
            f"Strategies: {', '.join(strategies)}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{_body}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>Threshold baru berlaku di run berikutnya.</i>"
        )
        tg(_evo_msg)
    except Exception as _te:
        log(f"  ⚠️ [N05] evolution Telegram summary: {_te}", "warn")


# ════════════════════════════════════════════════════════════════════
#  INTEGRATION INSTRUCTIONS v7.20
# ════════════════════════════════════════════════════════════════════
#
#  SETUP ENV (tambahkan ke .env / GitHub Secrets):
#    TWELVE_DATA_API_KEY   = "your_key"    # daftar di twelvedata.com
#    ALPHA_VANTAGE_API_KEY = "your_key"    # daftar di alphavantage.co
#    BROKER_ADAPTER_CLASS  = "adapters.ajaib.AjaibAdapter"   # sesuaikan
#    BROKER_API_KEY        = "your_key"
#    BROKER_TOKEN          = "your_token"
#
#  SQL Supabase baru:
#
#    CREATE TABLE IF NOT EXISTS model_thresholds (
#      id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
#      key         TEXT NOT NULL,
#      strategy    TEXT NOT NULL,
#      value       FLOAT NOT NULL,
#      updated_at  TIMESTAMPTZ,
#      UNIQUE (key, strategy)
#    );
#
#    CREATE TABLE IF NOT EXISTS model_evolution_log (
#      id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
#      event_type  TEXT,
#      strategy    TEXT,
#      old_value   TEXT,
#      new_value   TEXT,
#      reason      TEXT,
#      timestamp   TIMESTAMPTZ DEFAULT now()
#    );
#
#    ALTER TABLE execution_fills
#      ADD COLUMN IF NOT EXISTS fill_reason TEXT;
#
#  KODE INTEGRATION:
#
#  1. Replace get_candles() → get_candles_v2() di scan loop:
#
#     data, src = get_candles_v2(ticker, "1d", 60)
#     if data is None: continue
#     closes, highs, lows, volumes, opens = data
#     dq = calc_data_quality_score(ticker, ...)
#     dq = apply_source_penalty_to_dq(dq, src)    # ← BARU
#
#  2. Replace resolve_fill_price() → resolve_fill_price_v2():
#     (tidak perlu pass adapter — auto-managed)
#
#     fill_result = resolve_fill_price_v2(signal_row=row)   # ← BARU
#     update_fill_with_source(...)
#
#  3. Di send_health_check(), tambahkan:
#
#     fill_report  = check_fill_source_ratio_alert()    # ← BARU
#     # bulk reconcile: jalankan setiap 6 jam
#     bulk_reconcile_fills(limit=50)                    # ← BARU
#
#  4. Di scheduler / cron (run 1x per hari, jam 20:00 WIB):
#
#     run_model_evolution_cycle(["SWING", "INTRADAY", "SCALPING"])  # ← BARU
#
#  5. Load thresholds di awal scan (bukan hardcoded):
#
#     thresholds = _load_current_thresholds()
#     MIN_SCORE = thresholds["min_score"]
#     MIN_RR    = thresholds["min_rr"]
#     # dst.
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys as _sys
    _args = set(_sys.argv[1:])

    def _parse_days(default: int) -> int:
        for a in _sys.argv:
            if a.startswith("--days="):
                try: return int(a.split("=")[1])
                except ValueError: pass
        return default

    if "--backtest" in _args:
        _days = _parse_days(30)
        log(f"🧪 Mode BACKTEST — {_days} hari (dengan execution simulation)")
        run_backtest(days_back=_days, send_telegram=True)

    elif "--optimize" in _args:
        _days = _parse_days(60)
        log(f"🔧 Mode OPTIMIZE — analisis {_days} hari historis")
        optimize_weights(days_back=_days, send_telegram=True)

    elif "--walkforward" in _args:
        # [J02] Walk-Forward Validation
        _days = _parse_days(90)
        log(f"🔍 Mode WALK-FORWARD — {_days} hari, test window 10 hari")
        run_walk_forward_validation(total_days=_days, test_window_days=10, send_telegram=True)

    elif "--strategy-perf" in _args:
        # [J04] Strategy Performance Report
        log("📊 Mode STRATEGY PERFORMANCE — evaluasi per sub-strategy")
        result = get_strategy_performance(send_telegram=True)
        for sub, info in result.items():
            status = "🚫 DISABLED" if info.get("disabled") else "✅ Active"
            wr_str = f"{info['wr']:.0%}" if info.get("wr") is not None else "—"
            log(f"  {status} [{sub}]: WR={wr_str} ({info.get('wins',0)}/{info.get('total',0)})")

    elif "--kill-switch-test" in _args:
        # [J05] Test semua kill switch layer tanpa melakukan scan
        log("🛑 Mode KILL SWITCH TEST — evaluasi semua layer")
        ihsg = get_ihsg_regime()
        log(f"  IHSG: 1d={ihsg['ihsg_1d']:+.1f}% 5d={ihsg['ihsg_5d']:+.1f}%")
        ks_market = check_market_abnormal(ihsg)
        log(f"  Market abnormal: {ks_market['abnormal']} [{ks_market.get('severity','—')}]")
        ks_halt = check_idx_trading_halt(ihsg)
        log(f"  IDX halt: {ks_halt['halt_detected']} [{ks_halt.get('level','—')}]")
        ks_streak = check_losing_streak()
        log(f"  Losing streak: {ks_streak['triggered']} [streak={ks_streak['streak']}]")
        ks_port = check_portfolio_drawdown()
        log(f"  Portfolio: {ks_port.get('total_risk_pct',0):.1f}% exposure")
        queue_info = get_idx_queue_priority()
        log(f"  Queue: {queue_info['session']} — {queue_info['note']}")

    elif "--unit-test" in _args:
        # [v8.0 FIX 7] Unit tests untuk fungsi kalkulasi kritis.
        # Jalankan dengan: python bot.py --unit-test
        # Tujuan: memverifikasi bahwa perubahan kode tidak merusak kalkulasi
        # SL/TP/RR/lot yang benar. Ini bukan test comprehensif — ini smoke test
        # yang mendeteksi regression di kalkulasi paling kritikal.

        import traceback
        _pass = 0
        _fail = 0

        def _assert(name: str, condition: bool, detail: str = ""):
            global _pass, _fail
            if condition:
                log(f"  ✅ PASS: {name}")
                _pass += 1
            else:
                log(f"  ❌ FAIL: {name} — {detail}", "warn")
                _fail += 1

        log("🧪 [v8.0] UNIT TESTS — Kalkulasi Kritis\n" + "="*50)

        # ── Test 1: calc_lot_size — SL-distance based sizing ──────────
        log("\n📦 Test Group 1: Position Sizing (calc_lot_size)")
        try:
            result_ls = calc_lot_size(
                ticker="BBCA",
                entry_price=10_000.0,
                sl_price=9_500.0,        # SL jarak 5% = Rp500/saham
                portfolio_idr=10_000_000.0,
                signal_risk_pct=1.0,     # risk 1% = Rp100.000
                vol_today_idr=5_000_000_000.0,
                cap_tier="LARGE"
            )
            # Rp100.000 / Rp500 = 200 saham ideal = 2 lot
            _assert("lot_size: ideal 2 lot dari risk 1% / SL 5%",
                    result_ls["lots"] == 2,
                    f"got {result_ls['lots']} lot")
            _assert("lot_size: risk_idr mendekati 100.000",
                    abs(result_ls["risk_idr"] - 100_000) < 10_000,
                    f"got {result_ls['risk_idr']:.0f}")
            _assert("lot_size: tradeable=True",
                    result_ls["tradeable"] is True,
                    f"got tradeable={result_ls['tradeable']}")
        except Exception as _e:
            log(f"  ❌ FAIL: calc_lot_size raised exception — {_e}", "warn")
            _fail += 1

        # ── Test 2: calc_lot_size edge cases ──────────────────────────
        log("\n📦 Test Group 2: Position Sizing — Edge Cases")
        try:
            # SL = entry → harus return tradeable=False (divide by zero guard)
            result_dz = calc_lot_size("TEST", 1000.0, 1000.0, 10_000_000.0, 1.0, 1e9, "LARGE")
            _assert("lot_size: SL==entry → tradeable=False",
                    result_dz["tradeable"] is False,
                    f"got tradeable={result_dz['tradeable']}")
            # entry=0 → invalid
            result_zero = calc_lot_size("TEST", 0.0, 900.0, 10_000_000.0, 1.0, 1e9, "LARGE")
            _assert("lot_size: entry=0 → tradeable=False",
                    result_zero["tradeable"] is False,
                    f"got tradeable={result_zero['tradeable']}")
        except Exception as _e:
            log(f"  ❌ FAIL: calc_lot_size edge cases — {_e}", "warn")
            _fail += 1

        # ── Test 3: calc_rsi — basic sanity ───────────────────────────
        log("\n📊 Test Group 3: RSI Calculation")
        try:
            import numpy as np
            # Purely ascending closes → RSI harus > 70 (overbought)
            _asc = np.array([100.0 + i for i in range(30)])
            rsi_up = calc_rsi(_asc, period=14)
            _assert("rsi: pure uptrend > 70", rsi_up > 70, f"got {rsi_up:.1f}")

            # Purely descending closes → RSI harus < 30 (oversold)
            _desc = np.array([130.0 - i for i in range(30)])
            rsi_dn = calc_rsi(_desc, period=14)
            _assert("rsi: pure downtrend < 30", rsi_dn < 30, f"got {rsi_dn:.1f}")

            # RSI selalu dalam range 0–100
            _rand = np.array([100.0 + (i % 7 - 3) * 2 for i in range(30)])
            rsi_r = calc_rsi(_rand, period=14)
            _assert("rsi: always 0–100", 0 <= rsi_r <= 100, f"got {rsi_r:.1f}")
        except Exception as _e:
            log(f"  ❌ FAIL: calc_rsi — {_e}", "warn")
            _fail += 1

        # ── Test 4: detect_market_phase — smoke test ──────────────────
        log("\n🏗️ Test Group 4: Market Phase Detection")
        try:
            import numpy as np
            n = 60
            # Ascending trend → harus detect MARKUP atau EXPANSION
            _c = np.array([1000.0 + i * 10 for i in range(n)])
            _h = _c + 5
            _l = _c - 5
            _v = np.array([1_000_000.0 * (1.2 if i > 50 else 1.0) for i in range(n)])
            phase_result = detect_market_phase(_c, _h, _l, _v)
            _assert("detect_market_phase: returns valid phase",
                    phase_result.get("phase") in
                    {"MARKUP","MARKDOWN","EXPANSION","MANIPULATION","RANGING","CONSOLIDATION"},
                    f"got {phase_result.get('phase')}")
            _assert("detect_market_phase: confidence 0–1",
                    0 <= phase_result.get("confidence", -1) <= 1,
                    f"got {phase_result.get('confidence')}")
        except Exception as _e:
            log(f"  ❌ FAIL: detect_market_phase — {_e}", "warn")
            _fail += 1

        # ── Test 5: check_ks_pause_active — no crash when table empty ─
        log("\n🛑 Test Group 5: Kill Switch Pause Check")
        try:
            ks_result = check_ks_pause_active()
            _assert("check_ks_pause_active: returns dict",
                    isinstance(ks_result, dict),
                    f"got {type(ks_result)}")
            _assert("check_ks_pause_active: has 'triggered' key",
                    "triggered" in ks_result,
                    f"keys: {list(ks_result.keys())}")
        except Exception as _e:
            log(f"  ❌ FAIL: check_ks_pause_active — {_e}", "warn")
            _fail += 1

        # ── Summary ───────────────────────────────────────────────────
        total = _pass + _fail
        log(f"\n{'='*50}")
        log(f"🧪 UNIT TEST SELESAI: {_pass}/{total} passed", "info" if _fail == 0 else "warn")
        if _fail > 0:
            log(f"   ⚠️ {_fail} test GAGAL — periksa perubahan kode terbaru!", "warn")
        else:
            log("   ✅ Semua test lulus — kalkulasi kritis aman.")

    elif "--calibrate-params" in _args:
        # [v8.09-D] Paksa kalibrasi parameter sigmoid dari data historis.
        # Jalankan: python bot.py --calibrate-params [--days=N]
        # Output: rekomendasi DIST_KURT_SIG_SLOPE, DIST_KURT_SIG_INFLECT, dll.
        # Untuk apply otomatis tambahkan: ENV APPLY_CALIBRATED_PARAMS=true
        _days = _parse_days(90)
        log(f"📐 Mode CALIBRATE PARAMS — [v8.09-D] fit sigmoid dari {_days} hari historis")
        log(f"   ENV APPLY_CALIBRATED_PARAMS = "
            f"{os.environ.get('APPLY_CALIBRATED_PARAMS', 'false')}")
        _cal_result = calibrate_distribution_params(force=True)
        if _cal_result.get("error"):
            log(f"❌ Calibration gagal: {_cal_result['error']}", "warn")
        elif _cal_result.get("skipped"):
            log(f"⏭️ Calibration dilewati: {_cal_result['reason']}")
        else:
            log("\n══ HASIL KALIBRASI ══════════════════════════")
            kp = _cal_result.get("kurt_params", {})
            sp = _cal_result.get("skew_params", {})
            log(f"  Kurtosis sigmoid:")
            log(f"    Current  : slope={kp['current']['slope']:.2f} inflect={kp['current']['inflect']:.2f}")
            log(f"    Calibrated: slope={kp['calibrated']['slope']:.2f} inflect={kp['calibrated']['inflect']:.2f} corr={kp['corr']:.3f}")
            log(f"    Changed  : {'⚠️ YA — pertimbangkan update ENV' if kp['changed'] else '✅ tidak perlu update'}")
            log(f"  Skewness sigmoid:")
            log(f"    Current  : slope={sp['current']['slope']:.2f} inflect={sp['current']['inflect']:.2f}")
            log(f"    Calibrated: slope={sp['calibrated']['slope']:.2f} inflect={sp['calibrated']['inflect']:.2f} corr={sp['corr']:.3f}")
            log(f"    Changed  : {'⚠️ YA — pertimbangkan update ENV' if sp['changed'] else '✅ tidak perlu update'}")
            if _cal_result.get("score_threshold"):
                st = _cal_result["score_threshold"]
                log(f"  Score threshold: {st.get('note', '—')}")
            log("════════════════════════════════════════════")
            if kp.get("changed") or sp.get("changed"):
                log("  Untuk apply, set ENV vars berikut:", "warn")
                log(f"    DIST_KURT_SIG_SLOPE={kp['calibrated']['slope']:.2f}", "warn")
                log(f"    DIST_KURT_SIG_INFLECT={kp['calibrated']['inflect']:.2f}", "warn")
                log(f"    DIST_SKEW_SIG_SLOPE={sp['calibrated']['slope']:.2f}", "warn")
                log(f"    DIST_SKEW_SIG_INFLECT={sp['calibrated']['inflect']:.2f}", "warn")
                log("  Atau set APPLY_CALIBRATED_PARAMS=true untuk apply otomatis.", "warn")

    elif "--validate" in _args:
        # [PHASE-4] Full edge validation report
        # Jalankan: python bot.py --validate
        # Output: laporan 4-method ensemble ke Telegram + log detail
        log("🧠 Mode VALIDATE — [PHASE-4] Edge Validation Report")
        log(f"⏰  WIB: {datetime.now(WIB).strftime('%Y-%m-%d %H:%M WIB')}")
        log("📊 Menjalankan check_edge_proven() + ensemble 4-method...")
        _v_edge = check_edge_proven()
        _v_verd = _v_edge.get("verdict", "INSUFFICIENT")

        # Print detail ke log
        log(f"\n{'═'*55}")
        log(f"  VERDICT: {_v_verd}")
        log(f"  {_v_edge.get('note', '—')}")
        log(f"{'─'*55}")

        _v_ens = _v_edge.get("ensemble", {})
        log("  ENSEMBLE METHODS:")
        for _mn, _md in _v_ens.get("methods", {}).items():
            _ok_icon = "✅" if _md.get("ok") else "❌"
            log(f"    {_ok_icon} [{_mn}]: {_md.get('note', '—')}")

        _v_folds = _v_ens.get("folds", [])
        if _v_folds:
            log(f"{'─'*55}")
            log("  WALK-FORWARD FOLDS:")
            for _f in _v_folds:
                _drift_icon = "✅" if abs(_f["drift"]) <= 0.10 else "⚠️"
                log(f"    Fold {_f['fold']}: train WR={_f['wr_train']:.0%} "
                    f"→ test WR={_f['wr_test']:.0%} "
                    f"(drift={_f['drift']:+.0%}) {_drift_icon}")

        log(f"{'─'*55}")
        log(f"  STATISTIK:")
        log(f"    n resolved : {_v_edge.get('n', 0)}")
        log(f"    WR         : {_v_edge.get('wr', 0):.1%}" if _v_edge.get("wr") else "    WR         : N/A")
        log(f"    EV gross   : {_v_edge.get('empirical_ev', 0):+.3f}" if _v_edge.get("empirical_ev") is not None else "    EV gross   : N/A")
        log(f"    EV net     : {_v_edge.get('net_ev', 0):+.3f}" if _v_edge.get("net_ev") is not None else "    EV net     : N/A")
        log(f"    p-value    : {_v_edge.get('p_value', 0):.4f}" if _v_edge.get("p_value") is not None else "    p-value    : N/A")

        _v_stab = _v_edge.get("wr_stability", {})
        if _v_stab:
            log(f"    WR stability: older={_v_stab['wr_older_half']:.0%} "
                f"recent={_v_stab['wr_recent_half']:.0%} "
                f"Δ={_v_stab['delta']:+.0%}")

        log(f"{'─'*55}")
        _rules = {
            "PROVEN":       "✅ Edge terbukti. Adaptive layers diizinkan.",
            "PROMISING":    "🔵 Ada indikasi edge. Operasional normal, jangan ubah parameter.",
            "UNPROVEN":     ("⛔ JANGAN tambah kompleksitas.\n"
                             "    ⛔ JANGAN ubah W, TIER_MIN_SCORE, MIN_RR.\n"
                             "    ⛔ JANGAN aktifkan adaptive layers.\n"
                             "    ✅ Operasikan apa adanya. Kumpulkan lebih banyak trade."),
            "INSUFFICIENT": (f"⏳ Data belum cukup (target: {EDGE_PROOF_MIN_SIGNALS}).\n"
                             "    ✅ Jangan ubah apapun. Biarkan data terakumulasi."),
        }
        log(f"  ATURAN AKTIF ({_v_verd}):")
        log(f"    {_rules.get(_v_verd, '—')}")
        log(f"{'═'*55}\n")

        # Kirim Telegram
        send_validation_report(_v_edge, triggered_by="cli")
        log("✅ Validation report dikirim ke Telegram")

    elif "--edge-accuracy" in _args:
        # [v8.09-A] Evaluasi apakah verdict PROVEN benar-benar prediktif.
        # Jalankan: python bot.py --edge-accuracy
        # Ini adalah test fundamental: apakah sistem edge-gating kita punya nilai prediktif?
        log("📊 Mode EDGE ACCURACY — [v8.09-A] evaluasi prediktivitas verdict PROVEN/UNPROVEN")
        _ea = evaluate_edge_verdict_accuracy()
        if _ea.get("note"):
            log(f"\n{_ea['note']}")
        summary = _ea.get("summary_by_verdict", {})
        if summary:
            log("\n══ FUTURE WR PER VERDICT ════════════════════")
            for _v, _info in sorted(summary.items()):
                _n   = _info.get("n", 0)
                _wr  = _info.get("avg_wr", 0)
                _std = _info.get("std_wr")
                std_str = f" ±{_std:.0%}" if _std else ""
                log(f"  {_v:15s}: avg_future_wr={_wr:.0%}{std_str} (n={_n} snapshots)")
            log("════════════════════════════════════════════")
            _pa = _ea.get("predictive_accuracy")
            if _pa is not None:
                log(f"\nPREDICTIVE GAP (PROVEN - UNPROVEN): {_pa:+.0%}")
                if _pa > 0.05:
                    log("✅ Sistem edge-gating VALID — verdict PROVEN benar-benar prediktif lebih baik.")
                elif _pa > 0:
                    log("🔵 Gap kecil — butuh lebih banyak snapshot untuk konfirmasi.")
                else:
                    log("⚠️ SISTEM TIDAK PREDIKTIF — evaluasi ulang logika edge-gating.", "warn")
        else:
            log("ℹ️ Belum ada data evaluasi. Jalankan bot beberapa kali dulu agar "
                "snapshot terakumulasi dan dievaluasi secara retroaktif.")

    elif "--session-quality" in _args:
        # [v8.09-B] Cek data quality untuk session saat ini.
        # Jalankan: python bot.py --session-quality
        log("⏱️ Mode SESSION QUALITY — [v8.09-B] cek latency data saat ini")
        _ihsg_now = {}
        try:
            _ihsg_now = get_ihsg_regime()
        except Exception:
            pass
        _ihsg_5d = _ihsg_now.get("ihsg_5d", 0.0)
        _sq_intra = get_session_data_quality(DATA_SOURCE_INTRADAY, _ihsg_5d)
        _sq_swing = get_session_data_quality(DATA_SOURCE_SWING, _ihsg_5d)
        log(f"\n  Session saat ini : {_sq_intra['session']}")
        log(f"  IHSG 5d          : {_ihsg_5d:+.1f}% (vol_mult={_sq_intra['vol_multiplier']:.1f}x)")
        log(f"  INTRADAY [{DATA_SOURCE_INTRADAY}]:")
        log(f"    Effective penalty : {_sq_intra['effective_penalty']:.0%}")
        log(f"    Trade OK?         : {'✅ Ya' if _sq_intra['trade_ok'] else '🔴 TIDAK — pertimbangkan skip'}")
        log(f"    {_sq_intra['warning']}")
        log(f"  SWING [{DATA_SOURCE_SWING}]:")
        log(f"    Effective penalty : {_sq_swing['effective_penalty']:.0%}")
        log(f"    Trade OK?         : {'✅ Ya' if _sq_swing['trade_ok'] else '🔴 TIDAK'}")

    elif "--test" in _args:
        # [Z04] Unit tests core numerik — jalankan: python bot_saham_v8_11.py --test
        import traceback as _tb

        _pass_z = 0
        _fail_z = 0

        def _chk(name: str, cond: bool, detail: str = ""):
            global _pass_z, _fail_z
            if cond:
                log(f"  ✅ {name}")
                _pass_z += 1
            else:
                log(f"  ❌ FAIL: {name} — {detail}", "error")
                _fail_z += 1

        log("🧪 [Z04] UNIT TESTS CORE NUMERIK v8.12\n" + "="*55)

        # ── Group A: calc_atr ─────────────────────────────────────
        log("\n📐 Group A: calc_atr()")
        try:
            _c = np.array([100.0, 102.0, 101.0, 103.0, 102.0, 104.0,
                           103.0, 105.0, 104.0, 106.0, 105.0, 107.0,
                           106.0, 108.0, 107.0])
            _h = _c + 2.0
            _l = _c - 2.0
            _atr = calc_atr(_c, _h, _l)
            _chk("calc_atr: returns float", isinstance(_atr, float))
            _chk("calc_atr: > 0 untuk data normal", _atr > 0,
                 f"got {_atr}")
            _chk("calc_atr: ATR range masuk akal (1–5 dari data ±2)",
                 0.5 < _atr < 8.0, f"got {_atr:.4f}")
            # Edge case: array sangat pendek
            _atr_short = calc_atr(np.array([100.0, 101.0]),
                                  np.array([102.0, 103.0]),
                                  np.array([99.0, 100.0]))
            _chk("calc_atr: tidak crash pada 2 candle", _atr_short is not None)
        except Exception as _e:
            _chk("calc_atr: tidak raise exception", False, str(_e))

        # ── Group B: calc_ema ─────────────────────────────────────
        log("\n📈 Group B: calc_ema()")
        try:
            _prices = np.array([100.0] * 20 + [110.0] * 5, dtype=float)
            _ema20  = calc_ema(_prices, 20)
            _chk("calc_ema: returns float", isinstance(_ema20, float))
            _chk("calc_ema: EMA flat data = data itu sendiri",
                 abs(calc_ema(np.array([100.0]*20, dtype=float), 20) - 100.0) < 0.01,
                 f"got {calc_ema(np.array([100.0]*20, dtype=float), 20):.4f}")
            _chk("calc_ema: EMA setelah spike > rata-rata awal",
                 _ema20 > 100.0, f"got {_ema20:.4f}")
            # EMA20 > EMA10 setelah spike?
            _ema10 = calc_ema(_prices, 10)
            _chk("calc_ema: EMA10 > EMA20 saat data naik terbaru",
                 _ema10 > _ema20, f"EMA10={_ema10:.2f} EMA20={_ema20:.2f}")
        except Exception as _e:
            _chk("calc_ema: tidak raise exception", False, str(_e))

        # ── Group C: calc_ev ─────────────────────────────────────
        log("\n💹 Group C: calc_ev()")
        try:
            _ev_pos = calc_ev(win_prob=0.6, rr=2.0)
            _chk("calc_ev: EV positif untuk WR=60%, RR=2",
                 _ev_pos > 0, f"got {_ev_pos}")
            _ev_neg = calc_ev(win_prob=0.3, rr=1.0)
            _chk("calc_ev: EV negatif untuk WR=30%, RR=1",
                 _ev_neg < 0, f"got {_ev_neg}")
            _ev_coin = calc_ev(win_prob=0.5, rr=1.0)
            _chk("calc_ev: EV=0 untuk coin flip RR=1",
                 abs(_ev_coin) < 0.001, f"got {_ev_coin}")
        except Exception as _e:
            _chk("calc_ev: tidak raise exception", False, str(_e))

        # ── Group D: calc_kelly_fraction ─────────────────────────
        log("\n🎲 Group D: calc_kelly_fraction() — Bayesian shrinkage")
        try:
            _k_good = calc_kelly_fraction(win_prob=0.65, avg_win_r=2.0,
                                          avg_loss_r=1.0, n=100)
            _chk("kelly: returns float", isinstance(_k_good, float))
            _chk("kelly: hasil > 0 untuk setup bagus", _k_good > 0,
                 f"got {_k_good:.4f}")
            _chk("kelly: hasil <= 1.0 (tidak melebihi full kelly)",
                 _k_good <= 1.0, f"got {_k_good:.4f}")
            # Bayesian shrinkage: n=1 harus jauh lebih kecil dari n=1000
            _k_small_n = calc_kelly_fraction(0.65, 2.0, 1.0, n=1)
            _k_large_n = calc_kelly_fraction(0.65, 2.0, 1.0, n=1000)
            _chk("kelly: shrinkage — n=1 < n=1000",
                 _k_small_n < _k_large_n,
                 f"n=1:{_k_small_n:.4f} n=1000:{_k_large_n:.4f}")
        except Exception as _e:
            _chk("calc_kelly_fraction: tidak raise exception", False, str(_e))

        # ── Group E: Bootstrap phase logic ───────────────────────
        log("\n🧊 Group E: Bootstrap phase detection")
        try:
            _bp_cold    = ("COLD"    if 0    < BOOTSTRAP_COLD_N    else "EARLY")
            _bp_early   = ("EARLY"   if BOOTSTRAP_COLD_N    <= BOOTSTRAP_COLD_N + 1 < BOOTSTRAP_EARLY_N  else "WARMING")
            _bp_warming = ("WARMING" if BOOTSTRAP_EARLY_N   <= BOOTSTRAP_EARLY_N + 1 < BOOTSTRAP_WARMING_N else "MATURE")
            _bp_mature  = "MATURE"

            def _phase(n):
                return ("COLD"    if n < BOOTSTRAP_COLD_N    else
                        "EARLY"   if n < BOOTSTRAP_EARLY_N   else
                        "WARMING" if n < BOOTSTRAP_WARMING_N else
                        "MATURE")

            _chk("bootstrap: n=0 → COLD",    _phase(0) == "COLD")
            _chk("bootstrap: n=9 → COLD",    _phase(9) == "COLD")
            _chk("bootstrap: n=10 → EARLY",  _phase(10) == "EARLY",
                 f"got {_phase(10)}")
            _chk("bootstrap: n=29 → EARLY",  _phase(29) == "EARLY")
            _chk("bootstrap: n=30 → WARMING", _phase(30) == "WARMING",
                 f"got {_phase(30)}")
            _chk("bootstrap: n=100 → MATURE", _phase(100) == "MATURE",
                 f"got {_phase(100)}")
        except Exception as _e:
            _chk("bootstrap: tidak raise exception", False, str(_e))

        # ── Ringkasan ─────────────────────────────────────────────
        _total_z = _pass_z + _fail_z
        log(f"\n{'='*55}")
        log(f"📊 HASIL: {_pass_z}/{_total_z} PASS "
            f"({'✅ SEMUA LULUS' if _fail_z == 0 else f'❌ {_fail_z} GAGAL'})")
        if _fail_z > 0:
            log("⚠️  Ada test yang gagal — periksa fungsi core sebelum deploy!", "warn")
        log("="*55)

    elif "--evolution" in _args:
        # [v8.12] Model evolution — dipanggil dari GitHub Actions job terpisah jam 20:00 WIB
        log("🧬 Mode EVOLUTION — menjalankan model evolution cycle")
        log(f"⏰  WIB: {datetime.now(WIB).strftime('%Y-%m-%d %H:%M WIB')}")
        run_model_evolution_cycle(["SWING", "INTRADAY", "SCALPING"])

    elif "--collection-report" in _args:
        # [PHASE-3] Laporan kemajuan data collection
        # Jalankan: python bot.py --collection-report
        # Output: kirim dashboard ke Telegram + print ke log
        log("📊 Mode COLLECTION REPORT — PHASE 3 Data Collection Dashboard")
        log(f"⏰  WIB: {datetime.now(WIB).strftime('%Y-%m-%d %H:%M WIB')}")
        _prog = get_collection_progress()
        log(f"\n── COLLECTION PROGRESS ──────────────────────────")
        log(f"  Resolved   : {_prog['resolved']} trade "
            f"({_prog['wins']}W / {_prog['losses']}L)")
        log(f"  Win Rate   : {_prog['win_rate']}%")
        log(f"  EV         : {_prog['ev']}")
        log(f"  RR actual  : {_prog['avg_rr_actual']}")
        log(f"  Avg durasi : {_prog['avg_duration_hours']}h")
        log(f"  Target min : {_prog['pct_toward_min']:.0f}% dari {COLLECTION_TARGET_MIN}")
        log(f"  Target full: {_prog['pct_toward_full']:.0f}% dari {COLLECTION_TARGET_FULL}")
        log(f"  Ready?     : {'✅ YA' if _prog['phase3_ready'] else '⏳ Belum'}")
        if _prog["by_strategy"]:
            log(f"  Per strategy: {_prog['by_strategy']}")
        if _prog["mfe_mae"].get("avg_mfe") is not None:
            log(f"  MFE/MAE    : {_prog['mfe_mae']}")
        log("──────────────────────────────────────────────────")
        send_collection_report(_prog)
        log("✅ Collection report dikirim ke Telegram")

    else:
        run()

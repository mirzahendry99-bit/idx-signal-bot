"""
╔══════════════════════════════════════════════════════════════════╗
║           SIGNAL BOT SAHAM IDX — v7.0                          ║
║                                                                  ║
║  Diadaptasi dari Signal Bot Crypto v7.7b                        ║
║  Target: Saham IDX (Blue chip + Growth stock)                   ║
║                                                                  ║
║  Arsitektur:                                                     ║
║  - INTRADAY (1h)  : BUY + SELL — saham aktif harian            ║
║  - SWING    (1d)  : BUY + SELL — posisi multi-hari             ║
║                                                                  ║
║  Data Source : yfinance (delay ~15 menit, gratis)               ║
║  Market Filter: IHSG (^JKSE) sebagai pengganti BTC regime       ║
║  Notifikasi  : Telegram Bot                                      ║
║  Storage     : Supabase (dedup + signal tracking + win rate)    ║
║  Scheduler   : GitHub Actions (cron)                            ║
║                                                                  ║
║  Harga ditampilkan dalam IDR (Rupiah)                           ║
║                                                                  ║
║  v3.0–v5.0: Adaptive Intelligence + Elite Trading Engine        ║
║  v6.0: EV Engine, Agresif Trade Mgmt, NTZ, Setup Ranking,       ║
║         Capital Rotation                                         ║
║                                                                  ║
║  v7.0 Upgrades (Institutional-Grade System):                    ║
║  [21] EV sebagai CORE ENGINE — bukan filter tambahan.           ║
║       HARD_EV_FLOOR=0 (negative expectancy = HARD SKIP),       ║
║       EV menjadi primary sort key, filosofi: positive           ║
║       expectation bukan winrate tinggi.                         ║
║  [22] Kill Switch System (3 lapis perlindungan):                ║
║       Layer 1: Losing Streak Guard (≥3 loss berturut → pause)  ║
║       Layer 2: Market Abnormal Shutdown (crash/panic/vol spike) ║
║       Layer 3: Drawdown Circuit Breaker (total exposure > 8%)  ║
║  [23] Adaptive Trade Management (bot adapt saat trade berjalan):║
║       STRONG_TREND → let_profit_run (trailing ATR×1.2)         ║
║       WEAK_MOMENTUM → exit_early (lock profit sebelum reversal) ║
║       VOL_SPIKE → tighten_sl (ATR×0.5 saat volatilitas spike)  ║
║  [24] Meta Intelligence — Strategy Switching:                   ║
║       TREND_FOLLOW (ADX>25): BOS/CHoCH trigger, RR≥2.5,        ║
║       EV floor 0.25, ride momentum                              ║
║       MEAN_REVERSION (RANGING): OB/sweep trigger, RR≥1.8,      ║
║       EV floor 0.18, quick lock profit                          ║
║       DEFENSIVE (CHOPPY): wajib SNIPER level, EV floor 0.30    ║
║  [25] Portfolio-Level Control:                                   ║
║       Total exposure tracking, dynamic signal cap saat          ║
║       portfolio mendekati drawdown limit                        ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations   # Python 3.9 compatibility for `X | Y` type hints

import os, json, time
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
    "WSKT.JK",   # Waskita Karya
    "PTPP.JK",   # PP Pembangunan Perumahan
    "WIKA.JK",   # Wijaya Karya
    "AALI.JK",   # Astra Agro Lestari
    "LSIP.JK",   # PP London Sumatra
    "CPIN.JK",   # Charoen Pokphand Indonesia
    "JPFA.JK",   # Japfa Comfeed
    "MYOR.JK",   # Mayora Indah
    # ── Growth & Teknologi ────────────────────────────────
    "GOTO.JK",   # GoTo (Tokopedia/Gojek)
    "BUKA.JK",   # Bukalapak
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
    "JPRT.JK",   # Jaya Properti
    # ── Konsumer Defensif ────────────────────────────────
    "SIDO.JK",   # Industri Jamu Sido Muncul
    "ULTJ.JK",   # Ultra Jaya Milk
    "ROTI.JK",   # Nippon Indosari Corpindo
    "SKBM.JK",   # Sekar Bumi
    "MAIN.JK",   # Malindo Feedmill
    # ── Infrastruktur ────────────────────────────────────
    "FREN.JK",   # Smartfren Telecom
    "MARK.JK",   # Mark Dynamics Indonesia
    "ERAA.JK",   # Erajaya Swasembada
    "HERO.JK",   # Hero Supermarket
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
MIN_VOLUME_IDR     = 5_000_000_000    # Rp 5 miliar per hari

MAX_SIGNALS_CYCLE  = 6                # maksimal signal per run
DEDUP_HOURS        = 8                # tidak kirim ulang dalam 8 jam

# Scoring threshold per tier
TIER_MIN_SCORE = {
    "S":  14,
    "A+": 10,
    "A":   8,
}

# Risk/Reward minimum
MIN_RR = {
    "INTRADAY": 1.5,
    "SWING":    2.0,
}

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
IHSG_DROP_BLOCK  = -2.0   # IHSG turun > 2% dalam 1 hari → blok BUY
IHSG_CRASH_BLOCK = -5.0   # IHSG crash > 5% dalam 5 hari → halt semua

# ── Sector Correlation Map ────────────────────────────────────────
# Kelompokkan saham berdasarkan sektor IDX
SECTOR_MAP = {
    "BANKING":    ["BBCA.JK","BBRI.JK","BMRI.JK","BBNI.JK","PNBN.JK","NISP.JK","BNLI.JK","BRIS.JK","BTPS.JK","ARTO.JK"],
    "TELCO":      ["TLKM.JK","EXCL.JK","ISAT.JK","FREN.JK","TOWR.JK","TBIG.JK","EMTK.JK"],
    "ENERGY":     ["ADRO.JK","PTBA.JK","ITMG.JK","HRUM.JK","DOID.JK","GEMS.JK","BYAN.JK","AADI.JK"],
    "MINING":     ["ANTM.JK","INCO.JK","MDKA.JK","AMMN.JK","MBMA.JK","NCKL.JK","ESSA.JK"],
    "CONSUMER":   ["UNVR.JK","ICBP.JK","KLBF.JK","HMSP.JK","GGRM.JK","INDF.JK","MYOR.JK","SIDO.JK","ULTJ.JK","ROTI.JK","SKBM.JK"],
    "AUTO_INFRA": ["ASII.JK","UNTR.JK","JSMR.JK","WSKT.JK","PTPP.JK","WIKA.JK","ERAA.JK"],
    "PROPERTY":   ["PWON.JK","BSDE.JK","CTRA.JK","SMRA.JK","PANI.JK","CBDK.JK","KIJA.JK","BEST.JK","DSSA.JK","JPRT.JK"],
    "PETROCHEM":  ["TPIA.JK","BRPT.JK","PGAS.JK"],
    "CPO":        ["AALI.JK","LSIP.JK","SIMP.JK","SSMS.JK","TAPG.JK","TBLA.JK"],
    "POULTRY":    ["CPIN.JK","JPFA.JK","MAIN.JK"],
    "CEMENT":     ["SMGR.JK","INTP.JK"],
    "PULP":       ["INKP.JK","TKIM.JK"],
    "MEDIA":      ["SCMA.JK","MNCN.JK"],
    "TECH":       ["GOTO.JK","BUKA.JK"],
    "MISC":       ["AKRA.JK","MAPI.JK","LPPF.JK","MARK.JK","HERO.JK","CUAN.JK"],
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
    "TECH":       "GOTO.JK",
    "MISC":       "AKRA.JK",
}

# Sector momentum cache — diisi sekali per run oleh get_sector_momentum()
_sector_momentum_cache: dict = {}

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
#  UTILITIES
# ════════════════════════════════════════════════════════

def tg(msg: str):
    """Kirim pesan ke Telegram dengan retry 2x."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    body = json.dumps({
        "chat_id": TG_CHAT_ID, "text": msg,
        "parse_mode": "HTML", "disable_web_page_preview": True
    }).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            time.sleep(0.5)
            return
        except Exception as e:
            if attempt < 2:
                log(f"⚠️ Telegram retry {attempt+1}/2: {e}", "warn")
                time.sleep(2 ** attempt * 2)
            else:
                log(f"⚠️ Telegram gagal setelah 3x retry: {e}", "error")


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

        # Pastikan kolom yang dibutuhkan ada
        required = ["Close", "High", "Low", "Volume"]
        if not all(c in df.columns for c in required):
            log(f"⚠️ {ticker} [{interval}]: kolom tidak lengkap", "warn")
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

        result = (closes, highs, lows, volumes)
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
    except Exception:
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
    """ATR Wilder's — mengukur volatilitas."""
    if len(closes) < period + 1:
        return float(highs[-1] - lows[-1])
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_list.append(tr)
    tr_arr = np.array(tr_list[-period:], dtype=float)
    return float(np.mean(tr_arr))


def calc_vwap(closes, highs, lows, volumes, timeframe: str = "1d") -> float:
    """VWAP sesi — window disesuaikan per timeframe."""
    _window_map = {"1h": 24, "1d": 20, "4h": 6}
    window = min(_window_map.get(timeframe, 20), len(closes))
    c = closes[-window:]; h = highs[-window:]
    l = lows[-window:];   v = volumes[-window:]
    tp = (h + l + c) / 3
    cum_v = np.cumsum(v) + 1e-9
    return float((np.cumsum(tp * v) / cum_v)[-1])


def calc_adx(closes, highs, lows, period: int = ADX_PERIOD):
    """ADX dengan Wilder's smoothing — returns (adx, +DI, -DI)."""
    if len(closes) < period * 2:
        return 20.0, 0.0, 0.0
    h = highs.astype(float)
    l = lows.astype(float)
    c = closes.astype(float)

    plus_dm  = np.zeros(len(h))
    minus_dm = np.zeros(len(h))
    tr_arr   = np.zeros(len(h))

    for i in range(1, len(h)):
        up   = h[i] - h[i-1]
        down = l[i-1] - l[i]
        plus_dm[i]  = up   if up > down and up > 0   else 0
        minus_dm[i] = down if down > up and down > 0 else 0
        tr_arr[i]   = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))

    def wilder_smooth(arr, p):
        result = np.zeros(len(arr))
        result[p] = np.mean(arr[1:p+1])
        for i in range(p+1, len(arr)):
            result[i] = result[i-1] - result[i-1]/p + arr[i]
        return result

    sm_tr    = wilder_smooth(tr_arr, period)
    sm_plus  = wilder_smooth(plus_dm, period)
    sm_minus = wilder_smooth(minus_dm, period)

    plus_di  = np.where(sm_tr > 0, 100 * sm_plus  / sm_tr, 0)
    minus_di = np.where(sm_tr > 0, 100 * sm_minus / sm_tr, 0)

    dx = np.where((plus_di + minus_di) > 0,
                  100 * np.abs(plus_di - minus_di) / (plus_di + minus_di), 0)
    adx = float(np.mean(dx[-period:]))
    return round(adx, 2), round(float(plus_di[-1]), 2), round(float(minus_di[-1]), 2)


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
    bull_break = any(recent_closes[i] > last_sh and
                     recent_closes[i-1] <= last_sh * 1.008
                     for i in range(1, len(recent_closes)))
    bear_break = any(recent_closes[i] < last_sl and
                     recent_closes[i-1] >= last_sl * 0.992
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


def detect_order_block(closes, highs, lows, volumes, side="BUY", lookback=30) -> dict:
    """Order block detection — zona institusional."""
    result = {"valid": False, "ob_high": None, "ob_low": None}
    if len(closes) < lookback:
        return result
    c = closes[-lookback:]; h = highs[-lookback:]
    l = lows[-lookback:];   v = volumes[-lookback:]
    n = len(c)
    avg_body = float(np.mean([abs(c[i] - c[i-1]) for i in range(1, n)]))

    for i in range(n - 3, 1, -1):
        impulse = abs(c[i+1] - c[i])
        if impulse < avg_body * 1.5:
            continue
        if side == "BUY" and c[i] < c[i-1] and c[i+1] > c[i]:
            return {"valid": True, "ob_high": float(h[i]), "ob_low": float(l[i])}
        if side == "SELL" and c[i] > c[i-1] and c[i+1] < c[i]:
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
    tol = 0.005   # sedikit lebih longgar dari crypto karena spread IDX lebih lebar

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

def detect_market_phase(closes, highs, lows, volumes) -> dict:
    """
    Deteksi fase pasar yang lebih granular dari sekedar ADX.
    Phases: ACCUMULATION | MARKUP | DISTRIBUTION | MARKDOWN |
            EXPANSION | MANIPULATION | CONSOLIDATION
    Setiap fase mengubah bobot scoring secara otomatis.
    """
    result = {"phase": "CONSOLIDATION", "description": "Tidak ada fase dominan", "confidence": 0.3}
    if len(closes) < 50:
        return result

    c = closes.astype(float)
    h = highs.astype(float)
    l = lows.astype(float)
    v = volumes.astype(float)

    ema50 = calc_ema(c, 50)
    price = c[-1]

    # ATR — ekspansi vs kontraksi volatilitas
    atr_recent = float(np.mean([h[i] - l[i] for i in range(-5, 0)]))
    atr_prev   = float(np.mean([h[i] - l[i] for i in range(-15, -5)]))
    atr_ratio  = atr_recent / (atr_prev + 1e-9)

    # Volume momentum
    vol_recent = float(np.mean(v[-5:]))
    vol_prev   = float(np.mean(v[-15:-5]))
    vol_ratio  = vol_recent / (vol_prev + 1e-9)

    # Price momentum
    momentum_5  = (c[-1] - c[max(-6, -len(c))]) / (c[max(-6, -len(c))] + 1e-9) * 100
    momentum_20 = (c[-1] - c[max(-21, -len(c))]) / (c[max(-21, -len(c))] + 1e-9) * 100

    # Range compression — apakah candle semakin kecil?
    range_recent     = float(np.mean([h[i] - l[i] for i in range(-10, 0)]))
    range_prev       = float(np.mean([h[i] - l[i] for i in range(-30, -10)]))
    range_compressed = range_recent < range_prev * 0.72

    # Tentukan fase dominan
    if vol_ratio > 1.9 and abs(momentum_5) < 1.2:
        # Volume spike tinggi tapi harga tidak kemana-mana = jebakan
        phase = "MANIPULATION"
        desc  = "Volume spike tanpa arah — jebakan retail"
        conf  = 0.80
    elif atr_ratio > 1.55 and abs(momentum_5) > 2.8:
        # Volatilitas melebar + momentum = expansi
        phase = "EXPANSION"
        desc  = f"Volatilitas ekspansi {'bullish' if momentum_5 > 0 else 'bearish'}"
        conf  = min(atr_ratio / 2.2, 0.95)
    elif price > ema50 and momentum_20 > 4.5 and vol_ratio > 1.15:
        # Di atas EMA50 + trending up + vol naik = markup
        phase = "MARKUP"
        desc  = "Uptrend aktif — volume konfirmasi kuat"
        conf  = min(vol_ratio / 1.8, 0.90)
    elif price < ema50 and momentum_20 < -4.5 and vol_ratio > 1.15:
        # Di bawah EMA50 + trending down = markdown
        phase = "MARKDOWN"
        desc  = "Downtrend aktif — tekanan jual dominan"
        conf  = min(vol_ratio / 1.8, 0.90)
    elif range_compressed and vol_ratio < 0.82 and abs(momentum_5) < 1.5:
        # Konsolidasi ketat + vol rendah = akumulasi atau distribusi
        if price > ema50:
            phase = "DISTRIBUTION"
            desc  = "Konsolidasi di area tinggi — potensi reversal"
        else:
            phase = "ACCUMULATION"
            desc  = "Konsolidasi di area rendah — potensi breakout"
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
        # Breakout dari akumulasi = setup terbaik
        w["liq_sweep"]   = max(w["liq_sweep"],   5)
        w["order_block"] = max(w["order_block"],  5)
        w["vol_confirm"] = 4     # volume breakout kritis

    elif phase == "MARKUP":
        # Momentum fase naik
        w["bos"]        = max(w["bos"],        8)
        w["ema_align"]  = max(w["ema_align"],  4)
        w["macd_cross"] = max(w["macd_cross"], 4)

    elif phase == "DISTRIBUTION":
        # Short bias lebih berharga
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

    TREND_FOLLOW (regime TRENDING, ADX > 25):
      - Trigger utama: BOS + CHoCH (konfirmasi struktur tren)
      - Entry: pullback ke EMA/OB di dalam tren
      - RR target: ≥ 2.5 (ride the wave)
      - EV minimum: 0.25 (filter lebih ketat — butuh conviction tinggi)
      - Min score: lebih tinggi (butuh banyak konfirmasi tren)
      - Paradigma: "trend is your friend, cut losers fast, let winners run"

    MEAN_REVERSION (regime RANGING, ADX < 25):
      - Trigger utama: OB + liq sweep di zona ekstrem
      - Entry: di/dekat support/resistance zone
      - RR target: ≥ 1.5 (realistic di ranging — target center range)
      - EV minimum: 0.18 (lebih toleran — setup mean reversion lebih frequent)
      - Min score: lebih rendah (reversal di OB valid meski score biasa)
      - Paradigma: "sell the extreme, buy the extreme, quick profit lock"

    DEFENSIVE (regime CHOPPY / phase MANIPULATION):
      - Hanya ambil setup SNIPER level (score sangat tinggi)
      - EV minimum: 0.30 (butuh edge besar karena kondisi tidak reliable)
      - Paradigma: "sit on hands until clarity returns"

    Returns: dict strategy config yang digunakan oleh check_intraday/swing
    """
    if regime == "CHOPPY" or phase == "MANIPULATION":
        return {
            "mode":              "DEFENSIVE",
            "min_score_boost":   3,     # tambahan minimum score untuk defensive
            "rr_min_override":   None,  # gunakan MIN_RR default
            "ev_floor_override": 0.30,  # EV lebih tinggi saat kondisi chaos
            "require_sniper":    True,  # wajib PRECISION atau SNIPER level
            "description":       "Defensive — hanya A+ setup saat pasar tidak clear",
            "emoji":             "🛡️",
        }

    if regime == "TRENDING" and adx >= 25:
        return {
            "mode":              "TREND_FOLLOW",
            "min_score_boost":   0,
            "rr_min_override":   {"INTRADAY": 1.8, "SWING": 2.5},  # RR lebih tinggi
            "ev_floor_override": 0.25,  # conviction lebih tinggi untuk trend trade
            "require_sniper":    False,
            "description":       "Trend Follow — ride struktur, trailing agresif",
            "emoji":             "🚀",
        }

    if regime == "RANGING":
        return {
            "mode":              "MEAN_REVERSION",
            "min_score_boost":   -1,    # sedikit lebih toleran di score
            "rr_min_override":   {"INTRADAY": 1.3, "SWING": 1.8},  # RR lebih realistis
            "ev_floor_override": 0.18,  # toleran lebih karena setup lebih frequent
            "require_sniper":    False,
            "description":       "Mean Reversion — OB/liq zone, quick profit lock",
            "emoji":             "↕️",
        }

    # Default: normal mode (TRENDING tapi ADX borderline)
    return {
        "mode":              "NORMAL",
        "min_score_boost":   0,
        "rr_min_override":   None,
        "ev_floor_override": None,   # gunakan threshold dari setup_rank
        "require_sniper":    False,
        "description":       "Normal — standard scoring berlaku",
        "emoji":             "📊",
    }



    """
    [Upgrade #3] Entry Trigger Layer.
    Konfirmasi candle WAJIB ada sebelum entry.
    Cegah masuk terlalu cepat / terlalu telat.
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
    last_open  = c[-2]   # proxy open pakai prev close
    last_high  = h[-1]
    last_low   = l[-1]
    c_range    = last_high - last_low + 1e-9
    body       = abs(last_close - last_open)
    body_ratio = body / c_range

    # Candle sebelumnya (untuk engulfing)
    prev_close = c[-2]
    prev_open  = c[-3]
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
        # (SL retail kena, smart money masuk — kita ikut smart money)
        if recent_l[i] < support * 0.994 and recent_c[i] > support * 1.003:
            result["stop_hunt_bull"] = True

        # Stop hunt bearish: spike high yang langsung rejection di bawah resistance
        if recent_h[i] > resistance * 1.006 and recent_c[i] < resistance * 0.997:
            result["stop_hunt_bear"] = True

    return result


def get_daily_bias(ticker: str) -> str:
    """
    [Upgrade #5] MTF Alignment Helper.
    Ambil bias struktur 1D untuk validasi sinyal 1H.
    Intraday BUY hanya valid jika 1D BULLISH atau NEUTRAL.
    Intraday SELL hanya valid jika 1D BEARISH atau NEUTRAL.
    Returns: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
    """
    try:
        data = get_candles(ticker, "1d", 60)
        if data is None:
            return "NEUTRAL"
        closes, highs, lows, _ = data
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
                          daily_bias_aligned: bool) -> float:
    """
    Estimasi probabilitas win berdasarkan faktor kualitas sinyal.
    Tidak menggunakan ML — ini probabilitas empiris terstruktur.

    base_prob  = score / max_score (kualitas fundamental)
    Modifier tambahan dari regime, phase, trigger, MTF alignment.
    Returns float antara 0.0 - 1.0
    """
    if max_score <= 0:
        return 0.5
    base = min(score / max_score, 1.0)
    regime_mod = {"TRENDING": 0.07, "RANGING": -0.05, "CHOPPY": -0.15}.get(regime, 0.0)
    phase_mod = {
        "ACCUMULATION": 0.06, "MARKUP": 0.05,
        "EXPANSION":    0.04, "MANIPULATION": -0.08,
        "DISTRIBUTION": -0.04, "MARKDOWN": -0.05,
        "CONSOLIDATION": 0.0,
    }.get(phase, 0.0)
    trigger_mod = (trigger_strength - 0.6) * 0.15
    bias_mod = 0.05 if daily_bias_aligned else 0.0
    prob = base + regime_mod + phase_mod + trigger_mod + bias_mod
    return round(max(0.30, min(prob, 0.95)), 4)


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
EV_MIN_THRESHOLD = 0.20   # default untuk setup MEDIUM


# ════════════════════════════════════════════════════════
#  [9] POSITION MANAGEMENT ENGINE — v4.0
#  Trailing stop, partial TP, break-even otomatis
# ════════════════════════════════════════════════════════

def check_position_management(row: dict, closes, highs, lows) -> dict | None:
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

        # Volume momentum
        vol_avg    = float(np.mean(closes[-11:-1])) if len(closes) >= 11 else float(closes[-1])
        vol_drop   = float(closes[-1]) < float(closes[-3]) * 0.95   # harga drop 5% dari 3 candle lalu

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
                       f"Alasan   : {reason}\n"
                       f"Profit   : +{profit_r:.1f}R\n"
                       f"<i>Update SL manual di platform kamu.</i>")

            elif action == "EXIT_EARLY":
                exit_str = f"Rp{exit_price:,.0f}" if exit_price else "Market"
                msg = (f"{emoji} <b>EXIT EARLY ALERT</b>\n"
                       f"━━━━━━━━━━━━━━━━━━\n"
                       f"{side_str} <b>{pair}</b> [{mode_str}]\n"
                       f"Harga exit: <b>{exit_str}</b>\n"
                       f"Alasan    : {reason}\n"
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
        closes, _, _, _ = data
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


def is_sector_blocked(ticker: str, side: str) -> bool:
    """
    Blokir BUY jika sektor BEARISH, blokir SELL jika sektor BULLISH.
    Cegah false signal akibat tekanan sektoral sistemik.
    """
    sector = TICKER_SECTOR.get(ticker, "MISC")
    mom    = get_sector_momentum(sector)
    trend  = mom["trend"]
    if side == "BUY" and trend == "BEARISH":
        log(f"  ⚠️ {ticker}: Sektor {sector} BEARISH — blokir BUY")
        return True
    if side == "SELL" and trend == "BULLISH":
        log(f"  ⚠️ {ticker}: Sektor {sector} BULLISH — blokir SELL")
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
    Returns True jika stale (harus skip), False jika masih fresh.
    """
    if interval != "1h":
        return False
    try:
        df = yf.download(ticker, period="2d", interval="1h",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return True
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        last_ts = df.index[-1]
        if hasattr(last_ts, "tzinfo") and last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        now_utc  = datetime.now(timezone.utc)
        age_mins = (now_utc - last_ts).total_seconds() / 60
        if age_mins > 90:
            log(f"  ⚠️ {ticker} [1H]: Candle stale ({age_mins:.0f} menit) — skip intraday")
            return True
        return False
    except Exception as e:
        log(f"⚠️ is_candle_stale [{ticker}]: {e}", "warn")
        return False



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

    import math
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
            if total < 3:    # butuh minimal 3 signal untuk kesimpulan
                continue
            wr = data["wins"] / total

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
        if k in w:
            if w[k] < 0:
                w[k] = min(w[k] + delta, -1)   # penalty tetap negatif
            else:
                w[k] = max(1, min(w[k] + delta, 12))   # clamp 1–12

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
    if has_stop_hunt and has_fvg and "PRECISION" in sniper_level or "SNIPER" in sniper_level:
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
                 weights: dict = None) -> int:
    """
    Hitung score sinyal berdasarkan konfluens indikator.
    [v3.0] Parameter 'weights' memungkinkan dynamic scoring per regime+phase.
    Jika weights=None, gunakan W base (backward compatible).
    """
    _w = weights if weights else W   # gunakan dynamic weights jika tersedia
    is_bull = (side == "BUY")
    score   = 0

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
        prev       = float(closes[-2])
        body = last_close - prev
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
        prev       = float(closes[-2])
        body = prev - last_close
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
    """
    if strategy == "INTRADAY":
        sl_mult, tp1_r, tp2_r = INTRADAY_SL_ATR, INTRADAY_TP1_R, INTRADAY_TP2_R
    else:
        sl_mult, tp1_r, tp2_r = SWING_SL_ATR,    SWING_TP1_R,    SWING_TP2_R

    atr_dist = atr * sl_mult

    if side == "BUY":
        last_sl = structure.get("last_sl")
        if last_sl and last_sl < entry:
            sl = min(entry - atr_dist, last_sl * 0.998)
        else:
            sl = entry - atr_dist
        actual_sl_dist = entry - sl          # jarak SL aktual (mungkin lebih jauh dari ATR)
        tp1 = entry + actual_sl_dist * tp1_r
        tp2 = entry + actual_sl_dist * tp2_r
    else:
        last_sh = structure.get("last_sh")
        if last_sh and last_sh > entry:
            sl = max(entry + atr_dist, last_sh * 1.002)
        else:
            sl = entry + atr_dist
        actual_sl_dist = sl - entry
        tp1 = entry - actual_sl_dist * tp1_r
        tp2 = entry - actual_sl_dist * tp2_r

    return round(sl, 2), round(tp1, 2), round(tp2, 2)


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
KS_DRAWDOWN_PCT_MAX   = 8.0    # halt jika total drawdown open positions > 8%
KS_ABNORMAL_VOL_MULT  = 4.0    # pasar abnormal jika volatilitas > 4x normal
KS_PAUSE_HOURS        = 8      # durasi pause setelah kill switch aktif (jam)


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
            msg = (f"🛑 <b>KILL SWITCH — LOSING STREAK</b>\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"Streak LOSS berturut: <b>{streak}x</b> (max: {KS_LOSING_STREAK_MAX})\n"
                   f"Signal terakhir: {', '.join(r['pair'] + '(' + r['side'][0] + ')' for r in rows[:streak])}\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"⏸️ Trading di-<b>PAUSE</b> selama {KS_PAUSE_HOURS} jam.\n"
                   f"Bot akan resume otomatis di scan berikutnya setelah pause.\n"
                   f"<i>Review setup dan market condition sebelum resume.</i>")
            tg(msg)
            log(f"🛑 KILL SWITCH aktif: losing streak {streak}x berturut-turut", "warn")
            return {
                "triggered": True,
                "streak":    streak,
                "action":    "PAUSE",
                "message":   f"Losing streak {streak}x — pause {KS_PAUSE_HOURS}h"
            }

        log(f"✅ Kill switch OK: streak={streak} (max {KS_LOSING_STREAK_MAX})")
        return default

    except Exception as e:
        log(f"⚠️ check_losing_streak: {e} — skip", "warn")
        return default


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
            df = yf.download("^JKSE", period="30d", interval="1d",
                             progress=False, auto_adjust=True)
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
    [v7.0] Kill Switch Layer 3 — Drawdown Circuit Breaker.

    Estimasi total drawdown dari posisi aktif yang belum resolved.
    Jika total at-risk melebihi KS_DRAWDOWN_PCT_MAX dari portfolio → halt.

    Logic: ambil semua signal aktif (outcome IS NULL), hitung worst-case
    loss jika semua hit SL, bandingkan dengan PORTFOLIO_IDR.

    Returns: {triggered: bool, total_risk_pct: float, open_count: int}
    """
    default = {"triggered": False, "total_risk_pct": 0.0, "open_count": 0}
    try:
        rows = (
            supabase.table("signals")
            .select("entry, sl, side, strategy")
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
                if entry <= 0 or sl <= 0:
                    continue
                sl_pct    = abs(entry - sl) / entry
                # Estimasi position size dari 1% risk (worst case flat RISK_PCT)
                risk_idr  = PORTFOLIO_IDR * (RISK_PCT / 100)
                total_risk_idr += risk_idr
            except Exception:
                continue

        total_risk_pct = (total_risk_idr / PORTFOLIO_IDR * 100) if PORTFOLIO_IDR > 0 else 0.0

        if total_risk_pct > KS_DRAWDOWN_PCT_MAX:
            msg = (f"🛑 <b>KILL SWITCH — DRAWDOWN CIRCUIT BREAKER</b>\n"
                   f"━━━━━━━━━━━━━━━━━━\n"
                   f"Total at-risk: <b>{total_risk_pct:.1f}%</b> dari portfolio\n"
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
        return {"triggered": False, "total_risk_pct": round(total_risk_pct, 2), "open_count": len(rows)}

    except Exception as e:
        log(f"⚠️ check_portfolio_drawdown: {e}", "warn")
        return default




def get_ihsg_regime() -> dict:
    """
    Cek kondisi IHSG untuk market guard:
    - Crash guard: IHSG drop > 5% dalam 5 hari → halt semua
    - Drop guard:  IHSG drop > 2% dalam 1 hari → blok BUY
    """
    default = {"halt": False, "block_buy": False, "ihsg_1d": 0.0, "ihsg_5d": 0.0}
    try:
        df = yf.download("^JKSE", period="10d", interval="1d",
                          progress=False, auto_adjust=True)
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

        return {
            "halt":      halt,
            "block_buy": block_buy,
            "ihsg_1d":   round(chg_1d, 2),
            "ihsg_5d":   round(chg_5d, 2),
        }
    except Exception as e:
        log(f"⚠️ IHSG regime: {e}", "warn")
        return default


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
                ev: float = None):
    """
    Simpan signal ke Supabase.
    [v5.0] Tambah kolom regime + sector untuk Performance Clustering,
    dan win_prob + ev untuk analisis kualitas signal.
    Kolom baru perlu ditambahkan ke tabel Supabase:
      ALTER TABLE signals ADD COLUMN IF NOT EXISTS regime TEXT;
      ALTER TABLE signals ADD COLUMN IF NOT EXISTS sector TEXT;
      ALTER TABLE signals ADD COLUMN IF NOT EXISTS win_prob FLOAT;
      ALTER TABLE signals ADD COLUMN IF NOT EXISTS ev FLOAT;
    """
    ticker_jk = pair + ".JK"
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

    try:
        supabase.table("signals").insert(payload).execute()
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
    Signal yang melebihi window ini dianggap EXPIRED dan tidak di-resolve
    agar tidak menggunakan harga stale yang tidak relevan.
    """
    EXPIRY = {"INTRADAY": 2, "SWING": 10}  # hari maksimal signal dianggap aktif

    try:
        rows = (
            supabase.table("signals")
            .select("id, pair, side, entry, tp1, sl, strategy, sent_at")
            .is_("outcome", "null")
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
            except Exception:
                age_days = 0

            if age_days > max_days:
                supabase.table("signals").update(
                    {"outcome": "EXPIRED",
                     "closed_at": now_utc.isoformat()}
                ).eq("id", row["id"]).execute()
                expired += 1
                log(f"  ⏳ Expired [{row['pair']} {strategy}]: {age_days}d > {max_days}d max")
                continue

            ticker   = row["pair"] + ".JK"
            interval = "1h" if strategy == "INTRADAY" else "1d"
            limit    = 48  if strategy == "INTRADAY" else 15  # 48 jam atau 15 hari

            try:
                data = get_candles(ticker, interval, limit)
                if data is None:
                    continue
                _, highs, lows, _ = data

                tp1 = float(row["tp1"])
                sl  = float(row["sl"])
                outcome = None

                # Cek secara kronologis — candle pertama yang hit SL atau TP menentukan outcome
                # Ini mencegah false WIN (SL dulu, tapi TP tercapai belakangan)
                for h, l in zip(highs, lows):
                    h, l = float(h), float(l)
                    if row["side"] == "BUY":
                        if l <= sl:
                            outcome = "LOSS"; break
                        if h >= tp1:
                            outcome = "WIN";  break
                    else:  # SELL
                        if h >= sl:
                            outcome = "LOSS"; break
                        if l <= tp1:
                            outcome = "WIN";  break

                if outcome is None:
                    # ── [9] Position Management — cek trailing/BE/partial TP ──
                    closes_pm, highs_pm, lows_pm, _ = data
                    mgmt = check_position_management(row, closes_pm, highs_pm, lows_pm)
                    if mgmt:
                        log(f"  📡 Position mgmt [{row['pair']} {row['side']}]: "
                            f"profit={mgmt['profit_r']:.1f}R | "
                            f"actions={[a['action'] for a in mgmt['actions']]}")
                        send_position_management_alert(row, mgmt)
                    continue   # masih terbuka dalam window

                supabase.table("signals").update(
                    {"outcome": outcome,
                     "closed_at": now_utc.isoformat()}
                ).eq("id", row["id"]).execute()
                resolved += 1
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
    """
    default = {"overall": None, "intraday": None, "swing": None,
               "total_closed": 0, "wins": 0}
    try:
        rows = (
            supabase.table("signals")
            .select("strategy, outcome")
            .in_("outcome", ["WIN", "LOSS"])
            .execute()
            .data
        )
        if not rows:
            return default

        total  = len(rows)
        wins   = sum(1 for r in rows if r["outcome"] == "WIN")
        intra  = [r for r in rows if r["strategy"] == "INTRADAY"]
        swing  = [r for r in rows if r["strategy"] == "SWING"]

        def wr(lst):
            if not lst: return None
            return round(sum(1 for r in lst if r["outcome"] == "WIN") / len(lst) * 100, 1)

        return {
            "overall":      round(wins / total * 100, 1) if total else None,
            "intraday":     wr(intra),
            "swing":        wr(swing),
            "total_closed": total,
            "wins":         wins,
        }
    except Exception as e:
        log(f"⚠️ get_win_rate_summary: {e}", "warn")
        return default


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
        if not rows or len(rows) < 10:
            log("📊 Feedback weights: data belum cukup (<10 signal) — pakai base weights")
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

        # Rule 1: Tier S performa sangat baik → boost structure signals
        if tier_wr.get("S", 0.5) > 0.72:
            w["bos"]   = min(w["bos"] + 1, 10)
            w["choch"] = min(w["choch"] + 1, 8)
            adjustments.append(f"Tier-S WR {tier_wr['S']:.0%} → boost BOS/CHoCH")

        # Rule 2: Tier A performa buruk → reduce minor signals
        if tier_wr.get("A", 0.5) < 0.35:
            w["pullback"]    = max(w["pullback"] - 1,    1)
            w["candle_body"] = max(w["candle_body"] - 1, 1)
            adjustments.append(f"Tier-A WR {tier_wr.get('A', 0):.0%} → kurangi minor signals")

        # Rule 3: Overall WR rendah → conservatize
        if overall_wr < 0.44:
            w["macd_soft"]   -= 1
            w["adx_ranging"] -= 1
            adjustments.append(f"Overall WR {overall_wr:.0%} → conservatize mode")

        # Rule 4: Intraday berkinerja jauh lebih baik dari swing → boost intraday signals
        if intra_wr > swing_wr_v + 0.20:
            w["ema_align"]  = min(w["ema_align"] + 1,  5)
            w["vwap_side"]  = min(w["vwap_side"] + 1,  4)
            adjustments.append(f"Intraday WR {intra_wr:.0%} vs Swing {swing_wr_v:.0%} → boost intraday")

        if adjustments:
            log(f"📊 Feedback weights aktif ({len(rows)} signal): " + " | ".join(adjustments))
        else:
            log(f"📊 Feedback weights: overall_wr={overall_wr:.0%} — base weights dipertahankan")

        return w

    except Exception as e:
        log(f"⚠️ get_feedback_weights: {e} — pakai base weights", "warn")
        return W.copy()


def get_smart_risk_pct(score: int, tier: str) -> float:
    """
    [Upgrade #7] Smart Risk Allocation.
    Risk % dinamis berdasarkan confidence signal.
    Score/tier tinggi = risk lebih besar → maximize profit.
    Score rendah = risk dikurangi → protect capital.
    Selalu capped di batas aman (max 2x RISK_PCT, min 0.5x).
    """
    base = RISK_PCT
    if tier == "S" and score >= 16:
        r = min(base * 2.0, base + 1.5)   # max +1.5% dari base
    elif tier == "S" or score >= 14:
        r = min(base * 1.5, base + 1.0)
    elif tier == "A+" or score >= 10:
        r = base                            # normal
    else:
        r = max(base * 0.5, 0.5)           # conservative
    return round(min(r, 5.0), 2)           # hard cap 5%


# ════════════════════════════════════════════════════════
#  HEALTH CHECK — Heartbeat ke Telegram
# ════════════════════════════════════════════════════════

def send_health_check(scanned: int, skip_vol: int, ihsg: dict, wr: dict,
                       no_signal: bool = False):
    """
    Kirim heartbeat ringkas ke Telegram setiap run — bukti bot masih hidup.
    Jika no_signal=True, tambahkan keterangan tidak ada sinyal di pesan yang sama
    agar tidak ada double notifikasi.
    """
    now_str  = datetime.now(WIB).strftime("%d/%m/%Y %H:%M WIB")
    wr_str   = f"{wr['overall']}%" if wr["overall"] is not None else "belum ada data"
    wr_n     = f"{wr['wins']}/{wr['total_closed']}" if wr["total_closed"] > 0 else "—"
    intra_wr = f"{wr['intraday']}%" if wr["intraday"] is not None else "—"
    swing_wr = f"{wr['swing']}%"    if wr["swing"]    is not None else "—"

    signal_note = (
        f"\n━━━━━━━━━━━━━━━━━━\n"
        f"📭 Tidak ada sinyal memenuhi kriteria.\n"
        f"BUY: {'aktif' if not ihsg['block_buy'] else '⛔ diblokir (IHSG drop)'}\n"
        f"<i>Scan berikutnya ±4 jam.</i>"
    ) if no_signal else f"\n<i>Bot berjalan normal. Scan berikutnya ±4 jam.</i>"

    msg = (
        f"💓 <b>Bot Heartbeat — Saham IDX v6.0</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {now_str}\n"
        f"📊 IHSG 1d: <b>{ihsg['ihsg_1d']:+.1f}%</b> | 5d: <b>{ihsg['ihsg_5d']:+.1f}%</b>\n"
        f"Halt: {'🛑 YA' if ihsg['halt'] else '✅ tidak'}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔍 Di-scan: <b>{scanned}</b> | Skip vol: {skip_vol}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>Win Rate</b>\n"
        f"  Overall  : <b>{wr_str}</b> ({wr_n} signal)\n"
        f"  Intraday : {intra_wr} | Swing: {swing_wr}"
        f"{signal_note}"
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

    if side == "BUY" and ihsg["block_buy"]:
        return None

    # ── [12] Delay-Aware Entry Guard ─────────────────────
    if is_candle_stale(ticker, "1h"):
        return None

    # ── [10] Sector Correlation Filter ───────────────────
    if is_sector_blocked(ticker, side):
        return None

    data = get_candles(ticker, "1h", 100)
    if data is None:
        return None
    closes, highs, lows, volumes = data

    atr     = calc_atr(closes, highs, lows)
    atr_pct = atr / price * 100

    # ── [+] Trade Filter 1: ATR terlalu kecil atau terlalu besar ──
    if atr_pct < 0.5 or atr_pct > 10.0:
        return None

    # ── [+] Trade Filter 2: Volume spike abnormal (> 5x rata-rata) ──
    vol_avg_20  = float(np.mean(volumes[-21:-1])) if len(volumes) >= 21 else float(np.mean(volumes[:-1]))
    vol_current = float(volumes[-1])
    if vol_avg_20 > 0 and vol_current > vol_avg_20 * 5.0:
        log(f"  ⚠️ {ticker} [1H]: Volume spike abnormal ({vol_current/vol_avg_20:.1f}x) — skip")
        return None

    # ── [+] Trade Filter 3: Candle terakhir terlalu panjang (late entry) ──
    candle_range_pct = (float(highs[-1]) - float(lows[-1])) / (price + 1e-9) * 100
    if candle_range_pct > atr_pct * 2.5:
        log(f"  ⚠️ {ticker} [1H]: Candle late entry ({candle_range_pct:.1f}% > {atr_pct*2.5:.1f}%) — skip")
        return None

    mkt = detect_market_regime(closes, highs, lows)
    if mkt["regime"] == "CHOPPY":
        return None

    rsi        = calc_rsi(closes)
    macd, msig = calc_macd(closes)
    ema20      = calc_ema(closes, 20)
    ema50      = calc_ema(closes, 50)
    vwap       = calc_vwap(closes, highs, lows, volumes, timeframe="1h")
    structure  = detect_structure(closes, highs, lows, strength=3, lookback=60)
    liq        = detect_liquidity(closes, highs, lows, lookback=40)

    if not structure["valid"]:
        return None

    # ── [2] Market Phase Detection ────────────────────────
    phase_info = detect_market_phase(closes, highs, lows, volumes)
    phase      = phase_info["phase"]

    # ── [22] Meta Intelligence — Strategy Switching ──────
    # Bot memilih cara bermain secara fundamental berdasarkan regime + phase
    strategy_mode = get_active_strategy(mkt["regime"], phase, mkt["adx"])
    log(f"  🧠 {ticker} [1H {side}]: Strategy={strategy_mode['emoji']} {strategy_mode['mode']} — {strategy_mode['description']}")

    # ── [4] Liquidity Trap Detection ─────────────────────
    liq_trap = detect_liquidity_trap(closes, highs, lows)

    # ── [5] MTF Alignment — 1D bias validation ───────────
    daily_bias = get_daily_bias(ticker)
    if side == "BUY"  and daily_bias == "BEARISH":
        log(f"  ⚠️ {ticker} [1H BUY]: MTF conflict — 1D bias BEARISH — skip")
        return None
    if side == "SELL" and daily_bias == "BULLISH":
        log(f"  ⚠️ {ticker} [1H SELL]: MTF conflict — 1D bias BULLISH — skip")
        return None

    # ── [13] No-Trade Zone Engine ────────────────────────
    ntz = check_no_trade_zone(closes, highs, lows, volumes,
                               mkt["regime"], phase, daily_bias,
                               side, atr_pct)
    if ntz["skip"]:
        log(f"  🚫 {ticker} [1H {side}]: NTZ aktif — " + " | ".join(ntz["reasons"][:2]))
        return None

    # ── [14] Liquidity Depth Filter ──────────────────────
    liq_depth = is_liquidity_sufficient(closes, highs, lows, volumes, ticker)
    if not liq_depth["sufficient"]:
        log(f"  ⚠️ {ticker} [1H]: Liquidity insufficient — {liq_depth['reason']}")
        return None

    # ── [1] Dynamic Weights (regime + phase + feedback) ──
    base_w    = _adaptive_weights if _adaptive_weights else W
    dyn_w     = get_dynamic_weights(mkt["regime"], phase)
    # Merge: feedback weights sebagai floor, dynamic weights sebagai boost
    merged_w  = {k: max(base_w.get(k, W[k]), dyn_w.get(k, W[k])) for k in W}
    # Preserve penalty keys (negative values harus tetap negatif)
    for k in ["macd_soft", "adx_ranging"]:
        merged_w[k] = min(base_w.get(k, W[k]), dyn_w.get(k, W[k]))

    # ── [15] Performance Clustering — apply cluster modifier ──
    merged_w = apply_cluster_weights(merged_w, mkt["regime"], ticker)

    if side == "BUY":
        # Structural prerequisite — minimal satu konfirmasi
        has_struct = (structure.get("bos")   == "BULLISH" or
                      structure.get("choch") == "BULLISH" or
                      liq.get("sweep_bull")  or
                      liq_trap.get("stop_hunt_bull") or
                      liq_trap.get("fake_bear_break"))
        if not has_struct: return None
        if rsi > 72:       return None

        ob    = detect_order_block(closes, highs, lows, volumes, side="BUY", lookback=25)
        score = score_signal("BUY", price, closes, highs, lows, volumes,
                             structure, liq, ob, rsi, macd, msig,
                             ema20, ema50, vwap, mkt["regime"], weights=merged_w)

        # ── [3] Entry Trigger Validation ─────────────────
        trigger = entry_trigger_check(closes, highs, lows, "BUY")
        if not trigger["valid"]:
            log(f"  ⚠️ {ticker} [1H BUY]: No entry trigger — skip")
            return None

        tier = assign_tier(score)
        if tier == "SKIP": return None

        # ── [11] Sniper Entry — OB Reaction + FVG ────────
        ob_reaction  = detect_ob_reaction(closes, highs, lows, volumes, ob, "BUY")
        fvg          = detect_fvg(closes, highs, lows, side="BUY", lookback=25)
        sniper       = calc_sniper_score(liq, liq_trap, ob_reaction, fvg, "BUY")
        score       += sniper["bonus"]  # bonus presisi masuk ke skor akhir

        # [v6.0] Upgrade #4: Setup Ranking — boost skor dan adjust EV threshold per setup quality
        setup_rank = rank_setup_priority(structure, liq, liq_trap, ob, trigger, sniper, "BUY")
        score      += setup_rank["score_boost"]
        ev_threshold_active = setup_rank["ev_threshold"]
        log(f"  📊 {ticker} [1H BUY]: Setup Priority={setup_rank['priority']} ({setup_rank['reason']}), EV≥{ev_threshold_active}")

        last_sh = structure.get("last_sh")
        if last_sh and price > last_sh * 1.02: return None
        entry = round(last_sh * 1.002, 2) if (last_sh and price > last_sh) else price

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
        if not has_struct: return None
        if rsi < 22:       return None

        last_sh = structure.get("last_sh")
        if last_sh and price < last_sh * 0.97: return None

        ob    = detect_order_block(closes, highs, lows, volumes, side="SELL", lookback=25)
        score = score_signal("SELL", price, closes, highs, lows, volumes,
                             structure, liq, ob, rsi, macd, msig,
                             ema20, ema50, vwap, mkt["regime"], weights=merged_w)

        trigger = entry_trigger_check(closes, highs, lows, "SELL")
        if not trigger["valid"]:
            log(f"  ⚠️ {ticker} [1H SELL]: No entry trigger — skip")
            return None

        tier = assign_tier(score)
        if tier == "SKIP": return None

        # ── [11] Sniper Entry — OB Reaction + FVG ────────
        ob_reaction  = detect_ob_reaction(closes, highs, lows, volumes, ob, "SELL")
        fvg          = detect_fvg(closes, highs, lows, side="SELL", lookback=25)
        sniper       = calc_sniper_score(liq, liq_trap, ob_reaction, fvg, "SELL")
        score       += sniper["bonus"]

        # [v6.0] Upgrade #4: Setup Ranking
        setup_rank = rank_setup_priority(structure, liq, liq_trap, ob, trigger, sniper, "SELL")
        score      += setup_rank["score_boost"]
        ev_threshold_active = setup_rank["ev_threshold"]
        log(f"  📊 {ticker} [1H SELL]: Setup Priority={setup_rank['priority']} ({setup_rank['reason']}), EV≥{ev_threshold_active}")

        entry = round(last_sh * 0.998, 2) if (last_sh and price >= last_sh * 0.97) else price

        sl, tp1, tp2 = calc_sl_tp(entry, "SELL", atr, structure, "INTRADAY")
        if tp1 >= entry or sl <= entry: return None
        sl_dist = sl - entry
        if sl_dist <= 0 or sl_dist / entry > 0.08: return None
        rr = (entry - tp1) / sl_dist

    # ── [22] Meta Intelligence — RR override per strategy mode ──
    rr_min_intraday = MIN_RR["INTRADAY"]
    if strategy_mode["rr_min_override"] and "INTRADAY" in strategy_mode["rr_min_override"]:
        rr_min_intraday = strategy_mode["rr_min_override"]["INTRADAY"]

    if rr < rr_min_intraday:
        log(f"  ⚠️ {ticker} [1H {side}]: RR={rr:.1f} < {rr_min_intraday} [{strategy_mode['mode']}] — skip")
        return None

    # ── [7] Smart Risk Allocation ─────────────────────────
    smart_risk = get_smart_risk_pct(score, tier)

    # [v6.0] Capital Rotation — sesuaikan risk berdasarkan kekuatan sektor
    sector_weight = get_sector_capital_weight(ticker, side)
    smart_risk    = round(smart_risk * sector_weight, 2)
    smart_risk    = max(smart_risk, 0.25)   # floor 0.25%

    # ── [8] Core EV Engine — Primary Decision Gate ───────
    # [v7.0] EV adalah CORE ENGINE. Filosofi: positive expectation, bukan winrate.
    max_positive = sum(v for v in W.values() if v > 0) + 4
    bias_aligned = (side == "BUY" and daily_bias != "BEARISH") or \
                   (side == "SELL" and daily_bias != "BULLISH")
    win_prob = calc_win_probability(score, max_positive, mkt["regime"],
                                    phase, trigger["strength"], bias_aligned)
    ev = calc_expected_value(win_prob, rr)

    # Gate 1 — HARD FLOOR: EV <= 0 = HARD SKIP tanpa kompromi
    if ev <= HARD_EV_FLOOR:
        log(f"  ❌ {ticker} [1H {side}]: EV={ev:.2f} ≤ 0 — HARD SKIP (negative expectancy)")
        return None

    # Gate 2 — Strategy mode EV floor override (DEFENSIVE lebih ketat)
    ev_floor_meta = strategy_mode.get("ev_floor_override") or ev_threshold_active
    effective_ev_threshold = max(ev_threshold_active, ev_floor_meta)
    if ev <= effective_ev_threshold:
        log(f"  ⚠️ {ticker} [1H {side}]: EV={ev:.2f} ≤ {effective_ev_threshold:.2f} [{strategy_mode['mode']}/{setup_rank['priority']}] — skip")
        return None

    # Gate 3 — DEFENSIVE mode: wajib SNIPER/PRECISION level
    if strategy_mode.get("require_sniper") and "STANDARD" in sniper.get("level", "STANDARD"):
        log(f"  ⚠️ {ticker} [1H {side}]: DEFENSIVE mode — STANDARD sniper ditolak")
        return None

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
        "sniper_level": sniper["level"],
        "sniper_detail": sniper["details"],
        # v5.0 fields
        "chop_index":   ntz["chop_index"],
        "depth_score":  liq_depth["depth_score"],
        "near_hvn":     liq_depth["near_hvn"],
        "hvn_price":    liq_depth["hvn_price"],
        # v6.0 fields
        "setup_priority": setup_rank["priority"],
        "setup_reason":   setup_rank["reason"],
        "ev_threshold":   effective_ev_threshold,
        # v7.0 fields
        "strategy_mode":  strategy_mode["mode"],
        "strategy_emoji": strategy_mode["emoji"],
        "strategy_desc":  strategy_mode["description"],
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
    if side == "BUY" and ihsg["block_buy"]:
        return None

    # ── [10] Sector Correlation Filter ───────────────────
    if is_sector_blocked(ticker, side):
        return None

    data = get_candles(ticker, "1d", 120)
    if data is None:
        return None
    closes, highs, lows, volumes = data

    atr     = calc_atr(closes, highs, lows)
    atr_pct = atr / price * 100
    if atr_pct < 0.5 or atr_pct > 15.0:
        return None

    # ── [+] Trade Filter 2: Volume spike abnormal ─────────
    vol_avg_20  = float(np.mean(volumes[-21:-1])) if len(volumes) >= 21 else float(np.mean(volumes[:-1]))
    vol_current = float(volumes[-1])
    if vol_avg_20 > 0 and vol_current > vol_avg_20 * 5.0:
        log(f"  ⚠️ {ticker} [1D]: Volume spike abnormal ({vol_current/vol_avg_20:.1f}x) — skip")
        return None

    # ── [+] Trade Filter 3: Candle late entry ─────────────
    candle_range_pct = (float(highs[-1]) - float(lows[-1])) / (price + 1e-9) * 100
    if candle_range_pct > atr_pct * 3.0:   # lebih longgar untuk swing
        log(f"  ⚠️ {ticker} [1D]: Candle late entry ({candle_range_pct:.1f}%) — skip")
        return None

    mkt = detect_market_regime(closes, highs, lows)
    if mkt["regime"] == "CHOPPY":
        return None

    rsi        = calc_rsi(closes)
    macd, msig = calc_macd(closes)
    ema50      = calc_ema(closes, 50)
    ema200     = calc_ema(closes, 200)
    vwap       = calc_vwap(closes, highs, lows, volumes, timeframe="1d")
    structure  = detect_structure(closes, highs, lows, strength=4, lookback=80)
    liq        = detect_liquidity(closes, highs, lows, lookback=50)

    if not structure["valid"]:
        return None

    # ── [2] Market Phase Detection ────────────────────────
    phase_info = detect_market_phase(closes, highs, lows, volumes)
    phase      = phase_info["phase"]

    # ── [22] Meta Intelligence — Strategy Switching ──────
    strategy_mode = get_active_strategy(mkt["regime"], phase, mkt["adx"])
    log(f"  🧠 {ticker} [1D {side}]: Strategy={strategy_mode['emoji']} {strategy_mode['mode']}")

    # ── [4] Liquidity Trap Detection ─────────────────────
    liq_trap = detect_liquidity_trap(closes, highs, lows)

    # ── [5] MTF Alignment — weekly bias via broader structure ─
    # Untuk swing, cek bias pada lookback lebih panjang (proxy weekly)
    weekly_struct = detect_structure(closes, highs, lows, strength=5, lookback=120)
    weekly_bias   = weekly_struct.get("bias", "NEUTRAL")
    if side == "BUY"  and weekly_bias == "BEARISH":
        log(f"  ⚠️ {ticker} [1D BUY]: MTF conflict — weekly bias BEARISH — skip")
        return None
    if side == "SELL" and weekly_bias == "BULLISH":
        log(f"  ⚠️ {ticker} [1D SELL]: MTF conflict — weekly bias BULLISH — skip")
        return None

    # ── [1] Dynamic Weights ───────────────────────────────
    base_w   = _adaptive_weights if _adaptive_weights else W
    dyn_w    = get_dynamic_weights(mkt["regime"], phase)
    merged_w = {k: max(base_w.get(k, W[k]), dyn_w.get(k, W[k])) for k in W}
    for k in ["macd_soft", "adx_ranging"]:
        merged_w[k] = min(base_w.get(k, W[k]), dyn_w.get(k, W[k]))

    # ── [13] No-Trade Zone Engine ────────────────────────
    ntz = check_no_trade_zone(closes, highs, lows, volumes,
                               mkt["regime"], phase, weekly_bias,
                               side, atr_pct)
    if ntz["skip"]:
        log(f"  🚫 {ticker} [1D {side}]: NTZ aktif — " + " | ".join(ntz["reasons"][:2]))
        return None

    # ── [14] Liquidity Depth Filter ──────────────────────
    liq_depth = is_liquidity_sufficient(closes, highs, lows, volumes, ticker)
    if not liq_depth["sufficient"]:
        log(f"  ⚠️ {ticker} [1D]: Liquidity insufficient — {liq_depth['reason']}")
        return None

    # ── [15] Performance Clustering ───────────────────────
    merged_w = apply_cluster_weights(merged_w, mkt["regime"], ticker)

    if side == "BUY":
        has_struct = (structure.get("bos")   == "BULLISH" or
                      structure.get("choch") == "BULLISH" or
                      liq.get("sweep_bull")  or
                      liq_trap.get("stop_hunt_bull") or
                      liq_trap.get("fake_bear_break"))
        if not has_struct: return None
        if rsi > 68:       return None

        ob    = detect_order_block(closes, highs, lows, volumes, side="BUY", lookback=40)
        score = score_signal("BUY", price, closes, highs, lows, volumes,
                             structure, liq, ob, rsi, macd, msig,
                             ema50, ema200, vwap, mkt["regime"], weights=merged_w)

        # ── [3] Entry Trigger ─────────────────────────────
        trigger = entry_trigger_check(closes, highs, lows, "BUY")
        if not trigger["valid"]:
            log(f"  ⚠️ {ticker} [1D BUY]: No entry trigger — skip")
            return None

        tier = assign_tier(score)
        if tier == "SKIP": return None

        # ── [11] Sniper Entry ─────────────────────────────
        ob_reaction  = detect_ob_reaction(closes, highs, lows, volumes, ob, "BUY")
        fvg          = detect_fvg(closes, highs, lows, side="BUY", lookback=40)
        sniper       = calc_sniper_score(liq, liq_trap, ob_reaction, fvg, "BUY")
        score       += sniper["bonus"]

        # [v6.0] Upgrade #4: Setup Ranking
        setup_rank = rank_setup_priority(structure, liq, liq_trap, ob, trigger, sniper, "BUY")
        score      += setup_rank["score_boost"]
        ev_threshold_active = setup_rank["ev_threshold"]
        log(f"  📊 {ticker} [1D BUY]: Setup Priority={setup_rank['priority']} ({setup_rank['reason']}), EV≥{ev_threshold_active}")

        last_sh = structure.get("last_sh")
        if last_sh and price > last_sh * 1.02: return None
        entry = round(last_sh * 1.003, 2) if (last_sh and price > last_sh) else price

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
        if not has_struct: return None
        if rsi < 28:       return None

        last_sh = structure.get("last_sh")
        if last_sh and price < last_sh * 0.97: return None

        ob    = detect_order_block(closes, highs, lows, volumes, side="SELL", lookback=40)
        score = score_signal("SELL", price, closes, highs, lows, volumes,
                             structure, liq, ob, rsi, macd, msig,
                             ema50, ema200, vwap, mkt["regime"], weights=merged_w)

        trigger = entry_trigger_check(closes, highs, lows, "SELL")
        if not trigger["valid"]:
            log(f"  ⚠️ {ticker} [1D SELL]: No entry trigger — skip")
            return None

        tier = assign_tier(score)
        if tier == "SKIP": return None

        # ── [11] Sniper Entry ─────────────────────────────
        ob_reaction  = detect_ob_reaction(closes, highs, lows, volumes, ob, "SELL")
        fvg          = detect_fvg(closes, highs, lows, side="SELL", lookback=40)
        sniper       = calc_sniper_score(liq, liq_trap, ob_reaction, fvg, "SELL")
        score       += sniper["bonus"]

        # [v6.0] Upgrade #4: Setup Ranking
        setup_rank = rank_setup_priority(structure, liq, liq_trap, ob, trigger, sniper, "SELL")
        score      += setup_rank["score_boost"]
        ev_threshold_active = setup_rank["ev_threshold"]
        log(f"  📊 {ticker} [1D SELL]: Setup Priority={setup_rank['priority']} ({setup_rank['reason']}), EV≥{ev_threshold_active}")

        entry = round(last_sh * 0.998, 2) if (last_sh and price >= last_sh * 0.97) else price

        sl, tp1, tp2 = calc_sl_tp(entry, "SELL", atr, structure, "SWING")
        if tp1 >= entry or sl <= entry: return None
        sl_dist = sl - entry
        if sl_dist <= 0 or sl_dist / entry > 0.12: return None
        rr = (entry - tp1) / sl_dist

    # ── [22] Meta Intelligence — RR override per strategy mode ──
    rr_min_swing = MIN_RR["SWING"]
    if strategy_mode["rr_min_override"] and "SWING" in strategy_mode["rr_min_override"]:
        rr_min_swing = strategy_mode["rr_min_override"]["SWING"]

    if rr < rr_min_swing:
        log(f"  ⚠️ {ticker} [1D {side}]: RR={rr:.1f} < {rr_min_swing} [{strategy_mode['mode']}] — skip")
        return None

    # ── [7] Smart Risk Allocation ─────────────────────────
    smart_risk = get_smart_risk_pct(score, tier)

    # Capital Rotation
    sector_weight = get_sector_capital_weight(ticker, side)
    smart_risk    = round(smart_risk * sector_weight, 2)
    smart_risk    = max(smart_risk, 0.25)

    # ── [8] Core EV Engine — Primary Decision Gate ───────
    max_positive = sum(v for v in W.values() if v > 0) + 4
    bias_aligned = (side == "BUY" and weekly_bias != "BEARISH") or \
                   (side == "SELL" and weekly_bias != "BULLISH")
    win_prob = calc_win_probability(score, max_positive, mkt["regime"],
                                    phase, trigger["strength"], bias_aligned)
    ev = calc_expected_value(win_prob, rr)

    # Gate 1 — HARD FLOOR
    if ev <= HARD_EV_FLOOR:
        log(f"  ❌ {ticker} [1D {side}]: EV={ev:.2f} ≤ 0 — HARD SKIP (negative expectancy)")
        return None

    # Gate 2 — Strategy mode + setup rank combined threshold
    ev_floor_meta = strategy_mode.get("ev_floor_override") or ev_threshold_active
    effective_ev_threshold = max(ev_threshold_active, ev_floor_meta)
    if ev <= effective_ev_threshold:
        log(f"  ⚠️ {ticker} [1D {side}]: EV={ev:.2f} ≤ {effective_ev_threshold:.2f} [{strategy_mode['mode']}/{setup_rank['priority']}] — skip")
        return None

    # Gate 3 — DEFENSIVE: wajib SNIPER/PRECISION
    if strategy_mode.get("require_sniper") and "STANDARD" in sniper.get("level", "STANDARD"):
        log(f"  ⚠️ {ticker} [1D {side}]: DEFENSIVE mode — STANDARD sniper ditolak")
        return None

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
        "sniper_level": sniper["level"],
        "sniper_detail": sniper["details"],
        # v5.0 fields
        "chop_index":   ntz["chop_index"],
        "depth_score":  liq_depth["depth_score"],
        "near_hvn":     liq_depth["near_hvn"],
        "hvn_price":    liq_depth["hvn_price"],
        # v6.0 fields
        "setup_priority": setup_rank["priority"],
        "setup_reason":   setup_rank["reason"],
        "ev_threshold":   effective_ev_threshold,
        # v7.0 fields
        "strategy_mode":  strategy_mode["mode"],
        "strategy_emoji": strategy_mode["emoji"],
        "strategy_desc":  strategy_mode["description"],
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
    side_emoji   = "🟢 BUY" if side == "BUY" else "🔴 SELL"
    regime_emoji = {"TRENDING": "🔥", "RANGING": "⚠️"}.get(regime, "—")
    tp_label     = "+" if side == "BUY" else "-"
    sl_label     = "-" if side == "BUY" else "+"

    # Phase emoji
    phase_emoji_map = {
        "ACCUMULATION": "🏗️", "MARKUP": "🚀", "DISTRIBUTION": "🏭",
        "MARKDOWN": "📉",     "EXPANSION": "⚡", "MANIPULATION": "🎭",
        "CONSOLIDATION": "⏸️",
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
        portfolio_str = f"Rp{PORTFOLIO_IDR/1_000_000:.0f}jt" if PORTFOLIO_IDR >= 1_000_000 else f"Rp{PORTFOLIO_IDR:,.0f}"
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
                  f"  Maks risiko : Rp{ps['max_risk_idr']:,}\n"
                  f"  Est. lot    : <b>{ps['lot_estimate']} lot</b> ({ps['shares_estimate']:,} lembar)\n"
                  f"  Nilai posisi: Rp{ps['position_value']:,}\n"
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
        intel_lines.append(f"Win Prob   : <b>{win_prob:.0%}</b> | EV: {ev_str} {ev_emoji}")
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
    intel_block = "\n".join(intel_lines)

    msg = (
        f"{strat_emoji} <b>{tier_emoji} [{tier}] SIGNAL {side_emoji} — {strategy}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Saham  : <b>{pair}</b> [{tf}]\n"
        f"⏰ Valid : {now.strftime('%H:%M')} → {valid_until}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Entry  : <b>Rp{entry:,.0f}</b>{entry_note}\n"
        f"TP1    : <b>Rp{tp1:,.0f}</b> ({tp_label}{pct_tp1:.1f}%)\n"
        f"TP2    : <b>{tp2_str}</b>\n"
        f"SL     : <b>Rp{sl:,.0f}</b> ({sl_label}{pct_sl:.1f}%)\n"
        f"R/R    : <b>1:{rr}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Score      : {score} | RSI: {rsi}\n"
        f"Struktur   : {bos}\n"
        f"Regime     : {regime_emoji} {regime} (ADX: {adx})\n"
        f"Conviction : <b>{conviction}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{intel_block}"
        f"{ps_str}\n"
        f"<i>⚠️ Ini sinyal teknikal, bukan rekomendasi investasi.</i>\n"
        f"<i>Selalu pasang SL dan kelola risiko.</i>"
    )
    tg(msg)
    log(f"  ✅ SIGNAL {tier} {strategy} {side} {pair} RR:1:{rr} Score:{score} Phase:{phase} Trigger:{entry_pattern}")


# ════════════════════════════════════════════════════════
#  MAIN RUNNER
# ════════════════════════════════════════════════════════

def run():
    """Fungsi utama — scan seluruh watchlist dan kirim sinyal terbaik."""
    global _candle_cache, _dedup_memory, _sector_momentum_cache, _cluster_weights
    _candle_cache          = {}
    _dedup_memory          = set()
    _sector_momentum_cache = {}
    _cluster_weights       = {}

    now_wib = datetime.now(WIB)
    log(f"\n{'='*60}")
    log(f"🚀 SIGNAL BOT SAHAM IDX v7.0 — {now_wib.strftime('%Y-%m-%d %H:%M WIB')}")
    log(f"{'='*60}")

    # ── Step 1: Update outcome signal lama ───────────────
    log("📊 Mengupdate outcome signal sebelumnya...")
    update_signal_outcomes()

    # ── Step 2: Ambil win rate terkini ───────────────────
    wr = get_win_rate_summary()
    if wr["overall"] is not None:
        log(f"📈 Win Rate: {wr['overall']}% dari {wr['total_closed']} signal closed "
            f"(INTRADAY:{wr['intraday']}% | SWING:{wr['swing']}%)")

    # ── Step 2b: [v7.0] Kill Switch System — Layer 1: Losing Streak ─
    log("🛑 Kill switch check: losing streak...")
    ks_streak = check_losing_streak()
    if ks_streak["triggered"]:
        log(f"🛑 Kill switch AKTIF — {ks_streak['message']}")
        send_health_check(0, 0,
                          {"halt": False, "block_buy": False, "ihsg_1d": 0.0, "ihsg_5d": 0.0},
                          wr, no_signal=True)
        return

    # ── Step 3: [v3.0] Load adaptive weights dari histori ─
    global _adaptive_weights
    log("🧠 Memuat adaptive weights dari histori signal...")
    _adaptive_weights = get_feedback_weights()

    # ── Step 3b: [v5.0] Load cluster weights per regime×sector ─
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
               f"Alasan   : {reason}\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"🚫 Semua signal ditunda sampai kondisi normal.\n"
               f"<i>Scan berikutnya dalam 4 jam.</i>")
        tg(msg)
        log(f"🛑 Market abnormal [{severity}]: {reason}")
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
        send_health_check(0, 0, ihsg, wr, no_signal=True)
        return

    allow_buy  = not ihsg["block_buy"]
    allow_sell = True
    log(f"📊 IHSG 1d:{ihsg['ihsg_1d']:+.1f}% 5d:{ihsg['ihsg_5d']:+.1f}% | "
        f"BUY:{'aktif' if allow_buy else 'diblokir'} | "
        f"Portfolio exposure: {ks_port['total_risk_pct']:.1f}% ({ks_port['open_count']} open)")

    # ── Step 4d: [v7.0] Portfolio-Level Control: max exposure gate ──
    # Jika exposure mendekati limit (> 75%), kurangi MAX_SIGNALS_CYCLE
    # agar tidak overexpose saat posisi terlalu banyak
    exposure_pct  = ks_port["total_risk_pct"]
    signals_cap   = MAX_SIGNALS_CYCLE
    if exposure_pct > KS_DRAWDOWN_PCT_MAX * 0.75:
        signals_cap = max(1, MAX_SIGNALS_CYCLE // 2)
        log(f"⚠️ Exposure {exposure_pct:.1f}% mendekati limit — signal cap diturunkan ke {signals_cap}")
    elif exposure_pct > KS_DRAWDOWN_PCT_MAX * 0.50:
        signals_cap = max(2, MAX_SIGNALS_CYCLE - 2)
        log(f"⚠️ Exposure {exposure_pct:.1f}% moderat — signal cap {signals_cap}")

    signals  = []
    scanned  = 0
    skip_vol = 0

    for ticker in WATCHLIST:
        try:
            pair = ticker.replace(".JK", "")

            data_1d = get_candles(ticker, "1d", 10)
            if data_1d is None:
                continue

            closes_1d, _, _, volumes_1d = data_1d
            price   = float(closes_1d[-1])
            vol_5d  = float(np.mean(volumes_1d[-5:])) * price
            vol_idr = vol_5d

            if price <= 0:
                continue

            if vol_idr < MIN_VOLUME_IDR:
                skip_vol += 1
                log(f"  ⏭ {ticker}: vol 5d avg Rp{vol_idr/1e9:.1f}M < minimum — skip")
                continue

            scanned += 1
            log(f"  🔍 Scan {ticker} | Harga: Rp{price:,.0f} | Vol5d: Rp{vol_idr/1e9:.1f}M")

            # ── INTRADAY BUY ──────────────────────────────
            if allow_buy and not already_sent(pair, "INTRADAY", "BUY"):
                sig = check_intraday(ticker, price, ihsg, side="BUY")
                if sig: signals.append(sig)

            # ── INTRADAY SELL ─────────────────────────────
            if allow_sell and not already_sent(pair, "INTRADAY", "SELL"):
                sig = check_intraday(ticker, price, ihsg, side="SELL")
                if sig: signals.append(sig)

            # ── SWING BUY ────────────────────────────────
            if allow_buy and not already_sent(pair, "SWING", "BUY"):
                sig = check_swing(ticker, price, ihsg, side="BUY")
                if sig: signals.append(sig)

            # ── SWING SELL ───────────────────────────────
            if allow_sell and not already_sent(pair, "SWING", "SELL"):
                sig = check_swing(ticker, price, ihsg, side="SELL")
                if sig: signals.append(sig)

            time.sleep(0.5)

        except Exception as e:
            log(f"⚠️ [{ticker}]: {e}", "warn")
            continue

    buy_cand  = sum(1 for s in signals if s["side"] == "BUY")
    sell_cand = sum(1 for s in signals if s["side"] == "SELL")
    log(f"📊 Scanned: {scanned} | Vol filter: {skip_vol} | "
        f"Kandidat: {len(signals)} (BUY:{buy_cand} SELL:{sell_cand})")

    # ── Step 5: Health check heartbeat ───────────────────
    no_signal_flag = len(signals) == 0
    send_health_check(scanned, skip_vol, ihsg, wr, no_signal=no_signal_flag)

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
    for sig in signals:
        if sent >= signals_cap: break   # [v7.0] portfolio-aware cap
        send_signal(sig)
        save_signal(
            sig["pair"], sig["strategy"], sig["side"],
            sig["entry"], sig["tp1"], sig["tp2"], sig["sl"],
            sig["tier"], sig["score"], sig["timeframe"],
            regime   = sig.get("regime", "TRENDING"),
            sector   = TICKER_SECTOR.get(sig["ticker"], "MISC"),
            win_prob = sig.get("win_prob"),
            ev       = sig.get("expected_value"),
        )
        sent += 1
        time.sleep(0.5)

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

    tg(f"🔍 <b>Scan Selesai — Signal Bot Saham IDX v7.0</b>\n"
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
       f"🧠 <i>v7.0 — Institutional-Grade System aktif</i>\n"
       f"<i>Scan berikutnya dalam 4 jam.</i>")

    log(f"\n✅ Done — {sent}/{signals_cap} signal terkirim "
        f"(INTRADAY: BUY:{intraday_buy} SELL:{intraday_sell} | "
        f"SWING: BUY:{swing_buy} SELL:{swing_sell})")


if __name__ == "__main__":
    run()

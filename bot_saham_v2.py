"""
╔══════════════════════════════════════════════════════════════════╗
║           SIGNAL BOT SAHAM IDX — v2.0                          ║
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
║  v2.0 Upgrades:                                                  ║
║  - MACD true iterative EMA (akurat seperti TradingView)         ║
║  - Win rate tracker otomatis via Supabase                       ║
║  - Health check / heartbeat ke Telegram                         ║
║  - Position sizing guidance di output sinyal                    ║
║  - Low-liquidity ticker flagging                                 ║
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
#  SCORING ENGINE
# ════════════════════════════════════════════════════════

def score_signal(side: str, price: float, closes, highs, lows, volumes,
                 structure: dict, liq: dict, ob: dict,
                 rsi: float, macd: float, msig: float,
                 ema_fast: float, ema_slow: float,
                 vwap: float, regime: str = "TRENDING") -> int:
    """Hitung score sinyal berdasarkan konfluens indikator."""
    is_bull = (side == "BUY")
    score   = 0

    if is_bull:
        if structure.get("bos")   == "BULLISH": score += W["bos"]
        if structure.get("choch") == "BULLISH": score += W["choch"]
        if liq.get("sweep_bull"):               score += W["liq_sweep"]
        if ob.get("valid"):                     score += W["order_block"]
        if macd > msig:                         score += W["macd_cross"]
        elif macd < msig:                       score += W["macd_soft"]
        if 30 < rsi < 60:                       score += W["rsi_zone"]
        if rsi <= 30:                           score += W["rsi_extreme"]
        vol_avg = float(np.mean(volumes[-11:-1])) if len(volumes) >= 11 else float(np.mean(volumes[:-1])) if len(volumes) > 1 else float(volumes[-1])
        if float(volumes[-1]) > vol_avg * 1.3: score += W["vol_confirm"]
        if price > vwap:                        score += W["vwap_side"]
        last_sl = structure.get("last_sl")
        if last_sl and last_sl <= price <= last_sl * 1.015:
            score += W["pullback"]
        last_close = float(closes[-1])
        prev       = float(closes[-2])
        body = last_close - prev
        rng  = float(highs[-1]) - float(lows[-1]) + 1e-9
        if body > 0 and body / rng > 0.5:      score += W["candle_body"]
        if liq.get("equal_lows"):               score += W["equal_lows"]
        if ema_fast > ema_slow:                 score += W["ema_align"]   # Fix #3: EMA alignment bullish
    else:
        if structure.get("bos")   == "BEARISH": score += W["bos"]
        if structure.get("choch") == "BEARISH": score += W["choch"]
        if liq.get("sweep_bear"):               score += W["liq_sweep"]
        if ob.get("valid"):                     score += W["order_block"]
        if macd < msig:                         score += W["macd_cross"]
        elif macd > msig:                       score += W["macd_soft"]
        if 40 < rsi < 70:                       score += W["rsi_zone"]
        if rsi >= 70:                           score += W["rsi_extreme"]
        vol_avg = float(np.mean(volumes[-11:-1])) if len(volumes) >= 11 else float(np.mean(volumes[:-1])) if len(volumes) > 1 else float(volumes[-1])
        if float(volumes[-1]) > vol_avg * 1.3: score += W["vol_confirm"]
        if price < vwap:                        score += W["vwap_side"]
        last_sh = structure.get("last_sh")
        if last_sh and last_sh * 0.97 <= price <= last_sh * 1.01:
            score += W["pullback"]
        last_close = float(closes[-1])
        prev       = float(closes[-2])
        body = prev - last_close
        rng  = float(highs[-1]) - float(lows[-1]) + 1e-9
        if body > 0 and body / rng > 0.5:      score += W["candle_body"]
        if liq.get("equal_highs"):              score += W["equal_highs"]
        if ema_fast < ema_slow:                 score += W["ema_align"]   # Fix #3: EMA alignment bearish

    if regime == "TRENDING":  score += W["adx_trend"]
    elif regime == "RANGING": score += W["adx_ranging"]

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
#  MARKET CONTEXT — IHSG sebagai pengganti BTC regime
# ════════════════════════════════════════════════════════

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
                timeframe: str):
    """Simpan signal ke Supabase."""
    try:
        supabase.table("signals").insert({
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
        }).execute()
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
        f"💓 <b>Bot Heartbeat — Saham IDX v2.0</b>\n"
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
    INTRADAY signal — timeframe 1h.
    Jam bursa IDX: 09:00–16:00 WIB (Senin–Jumat).
    Signal hanya dikirim dalam jam bursa aktif.
    """
    # ── Market hours guard ────────────────────────────────
    now_wib  = datetime.now(WIB)
    weekday  = now_wib.weekday()   # 0=Senin, 4=Jumat, 5=Sabtu, 6=Minggu
    hour_wib = now_wib.hour
    if weekday >= 5 or not (9 <= hour_wib < 16):
        return None   # Bursa tutup — tidak ada sinyal intraday

    if side == "BUY" and ihsg["block_buy"]:
        return None

    data = get_candles(ticker, "1h", 100)
    if data is None:
        return None
    closes, highs, lows, volumes = data

    atr = calc_atr(closes, highs, lows)
    # Filter saham terlalu volatil atau hampir tidak bergerak
    atr_pct = atr / price * 100
    if atr_pct < 0.3 or atr_pct > 10.0:
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

    if side == "BUY":
        has_struct = (structure.get("bos")   == "BULLISH" or
                      structure.get("choch") == "BULLISH" or
                      liq.get("sweep_bull"))
        if not has_struct: return None
        if rsi > 72:       return None

        ob    = detect_order_block(closes, highs, lows, volumes, side="BUY", lookback=25)
        score = score_signal("BUY", price, closes, highs, lows, volumes,
                             structure, liq, ob, rsi, macd, msig,
                             ema20, ema50, vwap, mkt["regime"])
        tier  = assign_tier(score)
        if tier == "SKIP": return None

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
                      liq.get("sweep_bear"))
        if not has_struct: return None
        if rsi < 22:       return None

        last_sh = structure.get("last_sh")
        if last_sh and price < last_sh * 0.97: return None

        ob    = detect_order_block(closes, highs, lows, volumes, side="SELL", lookback=25)
        score = score_signal("SELL", price, closes, highs, lows, volumes,
                             structure, liq, ob, rsi, macd, msig,
                             ema20, ema50, vwap, mkt["regime"])
        tier  = assign_tier(score)
        if tier == "SKIP": return None

        entry = round(last_sh * 0.998, 2) if (last_sh and price >= last_sh * 0.97) else price

        sl, tp1, tp2 = calc_sl_tp(entry, "SELL", atr, structure, "INTRADAY")
        if tp1 >= entry or sl <= entry: return None
        sl_dist = sl - entry
        if sl_dist <= 0 or sl_dist / entry > 0.08: return None
        rr = (entry - tp1) / sl_dist

    if rr < MIN_RR["INTRADAY"]: return None

    return {
        "ticker":    ticker,
        "pair":      ticker.replace(".JK", ""),   # tampilan bersih
        "strategy":  "INTRADAY",
        "side":      side,
        "timeframe": "1h",
        "entry":     entry,
        "current_price": price,   # diupdate ke live price di send_signal
        "tp1": tp1, "tp2": tp2, "sl": sl,
        "tier":      tier,
        "score":     score,
        "rr":        round(rr, 1),
        "rsi":       round(rsi, 1),
        "structure": structure,
        "regime":    mkt["regime"],
        "adx":       mkt["adx"],
        "conviction": calc_conviction(score),
    }


def check_swing(ticker: str, price: float, ihsg: dict, side: str = "BUY") -> dict | None:
    """
    SWING signal — timeframe 1d.
    Target: posisi 3–10 hari.
    """
    if side == "BUY" and ihsg["block_buy"]:
        return None

    data = get_candles(ticker, "1d", 120)
    if data is None:
        return None
    closes, highs, lows, volumes = data

    atr = calc_atr(closes, highs, lows)
    atr_pct = atr / price * 100
    if atr_pct < 0.5 or atr_pct > 15.0:
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

    if side == "BUY":
        has_struct = (structure.get("bos")   == "BULLISH" or
                      structure.get("choch") == "BULLISH" or
                      liq.get("sweep_bull"))
        if not has_struct: return None
        if rsi > 68:       return None

        ob    = detect_order_block(closes, highs, lows, volumes, side="BUY", lookback=40)
        score = score_signal("BUY", price, closes, highs, lows, volumes,
                             structure, liq, ob, rsi, macd, msig,
                             ema50, ema200, vwap, mkt["regime"])
        tier  = assign_tier(score)
        if tier == "SKIP": return None

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
                      liq.get("sweep_bear"))
        if not has_struct: return None
        if rsi < 28:       return None

        last_sh = structure.get("last_sh")
        if last_sh and price < last_sh * 0.97: return None

        ob    = detect_order_block(closes, highs, lows, volumes, side="SELL", lookback=40)
        score = score_signal("SELL", price, closes, highs, lows, volumes,
                             structure, liq, ob, rsi, macd, msig,
                             ema50, ema200, vwap, mkt["regime"])
        tier  = assign_tier(score)
        if tier == "SKIP": return None

        entry = round(last_sh * 0.998, 2) if (last_sh and price >= last_sh * 0.97) else price

        sl, tp1, tp2 = calc_sl_tp(entry, "SELL", atr, structure, "SWING")
        if tp1 >= entry or sl <= entry: return None
        sl_dist = sl - entry
        if sl_dist <= 0 or sl_dist / entry > 0.12: return None
        rr = (entry - tp1) / sl_dist

    if rr < MIN_RR["SWING"]: return None

    return {
        "ticker":    ticker,
        "pair":      ticker.replace(".JK", ""),
        "strategy":  "SWING",
        "side":      side,
        "timeframe": "1d",
        "entry":     entry,
        "current_price": price,   # diupdate ke live price di send_signal
        "tp1": tp1, "tp2": tp2, "sl": sl,
        "tier":      tier,
        "score":     score,
        "rr":        round(rr, 1),
        "rsi":       round(rsi, 1),
        "structure": structure,
        "regime":    mkt["regime"],
        "adx":       mkt["adx"],
        "conviction": calc_conviction(score),
    }


# ════════════════════════════════════════════════════════
#  TELEGRAM OUTPUT
# ════════════════════════════════════════════════════════

def send_signal(sig: dict):
    """Format dan kirim signal ke Telegram."""
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

    # Ambil harga live hanya saat signal benar-benar dikirim (Bug D fix)
    live = get_current_price(sig["ticker"])
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

    # Position sizing — pakai nilai dari env var PORTFOLIO_IDR & RISK_PCT
    ps = calc_position_sizing(entry, sl, side,
                               risk_pct=RISK_PCT,
                               portfolio_idr=PORTFOLIO_IDR)
    if ps:
        portfolio_str = f"Rp{PORTFOLIO_IDR/1_000_000:.0f}jt" if PORTFOLIO_IDR >= 1_000_000 else f"Rp{PORTFOLIO_IDR:,.0f}"
        ps_str = (f"\n━━━━━━━━━━━━━━━━━━\n"
                  f"💼 <b>Position Sizing</b> (modal {portfolio_str}, risk {RISK_PCT}%)\n"
                  f"  Maks risiko : Rp{ps['max_risk_idr']:,}\n"
                  f"  Est. lot    : <b>{ps['lot_estimate']} lot</b> ({ps['shares_estimate']:,} lembar)\n"
                  f"  Nilai posisi: Rp{ps['position_value']:,}\n"
                  f"  <i>Sesuaikan dengan kondisi aktual kamu.</i>")
    else:
        ps_str = ""

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
        f"Conviction : <b>{conviction}</b>"
        f"{ps_str}\n"
        f"<i>⚠️ Ini sinyal teknikal, bukan rekomendasi investasi.</i>\n"
        f"<i>Selalu pasang SL dan kelola risiko.</i>"
    )
    tg(msg)
    log(f"  ✅ SIGNAL {tier} {strategy} {side} {pair} RR:1:{rr} Score:{score}")


# ════════════════════════════════════════════════════════
#  MAIN RUNNER
# ════════════════════════════════════════════════════════

def run():
    """Fungsi utama — scan seluruh watchlist dan kirim sinyal terbaik."""
    global _candle_cache, _dedup_memory
    _candle_cache  = {}
    _dedup_memory  = set()

    now_wib = datetime.now(WIB)
    log(f"\n{'='*60}")
    log(f"🚀 SIGNAL BOT SAHAM IDX v2.0 — {now_wib.strftime('%Y-%m-%d %H:%M WIB')}")
    log(f"{'='*60}")

    # ── Step 1: Update outcome signal lama ───────────────
    log("📊 Mengupdate outcome signal sebelumnya...")
    update_signal_outcomes()

    # ── Step 2: Ambil win rate terkini ───────────────────
    wr = get_win_rate_summary()
    if wr["overall"] is not None:
        log(f"📈 Win Rate: {wr['overall']}% dari {wr['total_closed']} signal closed "
            f"(INTRADAY:{wr['intraday']}% | SWING:{wr['swing']}%)")

    # Cek kondisi IHSG
    ihsg = get_ihsg_regime()
    if ihsg["halt"]:
        msg = (f"🛑 <b>HALT — IHSG CRASH</b>\n"
               f"IHSG 5d: {ihsg['ihsg_5d']:+.1f}% (threshold: {IHSG_CRASH_BLOCK}%)\n"
               f"Bot dihentikan sementara. Semua signal ditunda.\n"
               f"<i>Scan berikutnya dalam 4 jam.</i>")
        tg(msg)
        log("🛑 HALT karena IHSG crash"); return

    allow_buy  = not ihsg["block_buy"]
    allow_sell = True   # IDX tidak ada short selling retail — SELL sebagai alert exit/hedging
    log(f"📊 IHSG 1d:{ihsg['ihsg_1d']:+.1f}% 5d:{ihsg['ihsg_5d']:+.1f}% | "
        f"BUY:{'aktif' if allow_buy else 'diblokir'}")

    signals  = []
    scanned  = 0
    skip_vol = 0

    for ticker in WATCHLIST:
        try:
            pair = ticker.replace(".JK", "")   # Supabase menyimpan tanpa .JK

            # Ambil harga dan volume dari data 1d
            data_1d = get_candles(ticker, "1d", 10)
            if data_1d is None:
                continue

            closes_1d, _, _, volumes_1d = data_1d
            price   = float(closes_1d[-1])

            # Bug B fix: rata-rata volume 5 hari terakhir, bukan 1 candle
            vol_5d  = float(np.mean(volumes_1d[-5:])) * price
            vol_idr = vol_5d

            if price <= 0:
                continue

            # Filter volume minimum
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

            time.sleep(0.5)   # rate limit yfinance — jangan flood

        except Exception as e:
            log(f"⚠️ [{ticker}]: {e}", "warn")
            continue

    buy_cand  = sum(1 for s in signals if s["side"] == "BUY")
    sell_cand = sum(1 for s in signals if s["side"] == "SELL")
    log(f"📊 Scanned: {scanned} | Vol filter: {skip_vol} | "
        f"Kandidat: {len(signals)} (BUY:{buy_cand} SELL:{sell_cand})")

    # ── Step 3: Health check heartbeat ───────────────────
    no_signal_flag = len(signals) == 0
    send_health_check(scanned, skip_vol, ihsg, wr, no_signal=no_signal_flag)

    if not signals:
        log("📭 Tidak ada signal"); return

    # Sort: tier terbaik dulu, lalu score tertinggi
    tier_order = {"S": 0, "A+": 1, "A": 2}
    signals.sort(key=lambda x: (tier_order.get(x["tier"], 9), -x["score"]))

    sent = 0
    for sig in signals:
        if sent >= MAX_SIGNALS_CYCLE: break
        send_signal(sig)
        save_signal(
            sig["pair"], sig["strategy"], sig["side"],   # Fix #1: pair bukan ticker (.JK)
            sig["entry"], sig["tp1"], sig["tp2"], sig["sl"],
            sig["tier"], sig["score"], sig["timeframe"]
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

    tg(f"🔍 <b>Scan Selesai — Signal Bot Saham IDX v2.0</b>\n"
       f"━━━━━━━━━━━━━━━━━━\n"
       f"Saham di-scan : <b>{scanned}</b>\n"
       f"IHSG 1d/5d    : <b>{ihsg['ihsg_1d']:+.1f}% / {ihsg['ihsg_5d']:+.1f}%</b>\n"
       f"━━━━━━━━━━━━━━━━━━\n"
       f"Signal terkirim : <b>{sent}</b>\n"
       f"  📈 INTRADAY BUY  : {intraday_buy}\n"
       f"  📉 INTRADAY SELL : {intraday_sell}\n"
       f"  🌊 SWING BUY     : {swing_buy}\n"
       f"  🌊 SWING SELL    : {swing_sell}"
       f"{wr_line}\n"
       f"<i>Scan berikutnya dalam 4 jam.</i>")

    log(f"\n✅ Done — {sent} signal terkirim "
        f"(INTRADAY: BUY:{intraday_buy} SELL:{intraday_sell} | "
        f"SWING: BUY:{swing_buy} SELL:{swing_sell})")


if __name__ == "__main__":
    run()

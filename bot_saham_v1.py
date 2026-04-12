"""
╔══════════════════════════════════════════════════════════════════╗
║           SIGNAL BOT SAHAM IDX — v1.0                          ║
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
║  Storage     : Supabase (dedup + tracking)                      ║
║  Scheduler   : GitHub Actions (cron)                            ║
║                                                                  ║
║  Harga ditampilkan dalam IDR (Rupiah)                           ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, json, time, math
import logging
import numpy as np
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

_missing = [k for k, v in {
    "SUPABASE_URL":   SUPABASE_URL,
    "SUPABASE_KEY":   SUPABASE_KEY,
    "TELEGRAM_TOKEN": TG_TOKEN,
    "CHAT_ID":        TG_CHAT_ID,
}.items() if not v]
if _missing:
    raise EnvironmentError(f"ENV belum diset: {', '.join(_missing)}")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ════════════════════════════════════════════════════════
#  WATCHLIST SAHAM IDX
#  Blue chip (LQ45) + Growth stock pilihan
#  Format yfinance untuk IDX: tambahkan .JK di belakang
# ════════════════════════════════════════════════════════

WATCHLIST = [
    # ── Blue Chip / LQ45 ──────────────────────────────
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
    "INDF.JK",   # Indofood
    "EXCL.JK",   # XL Axiata
    "SMGR.JK",   # Semen Indonesia
    "PGAS.JK",   # PGN
    "PTBA.JK",   # Bukit Asam
    "ANTM.JK",   # Aneka Tambang
    "INCO.JK",   # Vale Indonesia
    "ADRO.JK",   # Adaro Energy
    "ITMG.JK",   # Indo Tambangraya Megah
    # ── Growth Stock Pilihan ──────────────────────────
    "GOTO.JK",   # GoTo (Tokopedia/Gojek)
    "BUKA.JK",   # Bukalapak
    "EMTK.JK",   # Elang Mahkota Teknologi
    "MDKA.JK",   # Merdeka Copper Gold
    "ARTO.JK",   # Bank Jago
    "BRIS.JK",   # Bank Syariah Indonesia
    "ESSA.JK",   # ESSA Industries
    "TPIA.JK",   # Chandra Asri
    "DSSA.JK",   # Dian Swastatika Sentosa
    "CUAN.JK",   # Petrindo Jaya Kreasi
    "AMMN.JK",   # Amman Mineral
    "MBMA.JK",   # Merdeka Battery Materials
    "CBDK.JK",   # Cipta Bintang Dharma Kencana
    "PANI.JK",   # Pratama Abadi Nusa
]

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
        if isinstance(df.columns, __import__('pandas').MultiIndex):
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
    """Ambil harga terakhir dari yfinance."""
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
    """MACD line dan signal line."""
    if len(closes) < slow + signal:
        return 0.0, 0.0
    c = closes.astype(float)
    ema_fast = float(c[-fast:].mean())   # simplified untuk efisiensi
    ema_slow = float(c[-slow:].mean())
    macd_line = ema_fast - ema_slow
    signal_line = macd_line * 0.9        # approximation
    return round(macd_line, 6), round(signal_line, 6)


def calc_ema(closes, period: int) -> float:
    """EMA periode tertentu dari closes terakhir."""
    if len(closes) < period:
        return float(closes[-1])
    c     = closes.astype(float)
    alpha = 2.0 / (period + 1)
    ema   = float(c[-period:].mean())   # seed dengan SMA
    for price in c[-period:]:
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
        result["bos" if result["bias"] == "BULLISH" else "choch"] = "BULLISH"
    elif bear_break:
        result["bos" if result["bias"] == "BEARISH" else "choch"] = "BEARISH"

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
        vol_avg = float(np.mean(volumes[-10:-1]))
        if float(volumes[-1]) > vol_avg * 1.3: score += W["vol_confirm"]
        if ema_fast > ema_slow:                 score += W["ema_align"]
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
    else:
        if structure.get("bos")   == "BEARISH": score += W["bos"]
        if structure.get("choch") == "BEARISH": score += W["choch"]
        if liq.get("sweep_bear"):               score += W["liq_sweep"]
        if ob.get("valid"):                     score += W["order_block"]
        if macd < msig:                         score += W["macd_cross"]
        elif macd > msig:                       score += W["macd_soft"]
        if 40 < rsi < 70:                       score += W["rsi_zone"]
        if rsi >= 70:                           score += W["rsi_extreme"]
        vol_avg = float(np.mean(volumes[-10:-1]))
        if float(volumes[-1]) > vol_avg * 1.3: score += W["vol_confirm"]
        if ema_fast < ema_slow:                 score += W["ema_align"]
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
    """SL berbasis ATR + struktur. TP berbasis RR multiplier."""
    if strategy == "INTRADAY":
        sl_mult, tp1_r, tp2_r = INTRADAY_SL_ATR, INTRADAY_TP1_R, INTRADAY_TP2_R
    else:
        sl_mult, tp1_r, tp2_r = SWING_SL_ATR,    SWING_TP1_R,    SWING_TP2_R

    sl_dist = atr * sl_mult

    if side == "BUY":
        last_sl = structure.get("last_sl")
        if last_sl and last_sl < entry:
            sl = min(entry - sl_dist, last_sl * 0.998)
        else:
            sl = entry - sl_dist
        tp1 = entry + sl_dist * tp1_r
        tp2 = entry + sl_dist * tp2_r
    else:
        last_sh = structure.get("last_sh")
        if last_sh and last_sh > entry:
            sl = max(entry + sl_dist, last_sh * 1.002)
        else:
            sl = entry + sl_dist
        tp1 = entry - sl_dist * tp1_r
        tp2 = entry - sl_dist * tp2_r

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

        if isinstance(df.columns, __import__('pandas').MultiIndex):
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
        }).execute()
    except Exception as e:
        log(f"⚠️ save_signal [{pair}]: {e}", "warn")
    finally:
        _dedup_memory.add(_dedup_key(pair, strategy, side))


# ════════════════════════════════════════════════════════
#  SIGNAL STRATEGIES
# ════════════════════════════════════════════════════════

def check_intraday(ticker: str, price: float, ihsg: dict, side: str = "BUY") -> dict | None:
    """
    INTRADAY signal — timeframe 1h.
    Jam bursa IDX: 09:00–16:00 WIB (Senin–Jumat).
    """
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
        "current_price": price,
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
        "current_price": price,
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
    cur_price  = sig.get("current_price", entry)
    bos        = sig["structure"].get("bos") or sig["structure"].get("choch") or "—"
    regime     = sig.get("regime", "—")
    adx        = sig.get("adx", 0.0)
    conviction = sig.get("conviction", "OK 🟡")

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
    log(f"🚀 SIGNAL BOT SAHAM IDX v1.0 — {now_wib.strftime('%Y-%m-%d %H:%M WIB')}")
    log(f"{'='*60}")

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
            # Ambil harga dan volume dari data 1d
            data_1d = get_candles(ticker, "1d", 10)
            if data_1d is None:
                continue

            closes_1d, _, _, volumes_1d = data_1d
            price    = float(closes_1d[-1])
            vol_idr  = float(volumes_1d[-1]) * price   # estimasi volume dalam IDR

            if price <= 0:
                continue

            # Filter volume minimum
            if vol_idr < MIN_VOLUME_IDR:
                skip_vol += 1
                log(f"  ⏭ {ticker}: volume Rp{vol_idr/1e9:.1f}M < minimum — skip")
                continue

            scanned += 1
            log(f"  🔍 Scan {ticker} | Harga: Rp{price:,.0f} | Vol: Rp{vol_idr/1e9:.1f}M")

            # ── INTRADAY BUY ──────────────────────────────
            if allow_buy and not already_sent(ticker, "INTRADAY", "BUY"):
                sig = check_intraday(ticker, price, ihsg, side="BUY")
                if sig: signals.append(sig)

            # ── INTRADAY SELL ─────────────────────────────
            if allow_sell and not already_sent(ticker, "INTRADAY", "SELL"):
                sig = check_intraday(ticker, price, ihsg, side="SELL")
                if sig: signals.append(sig)

            # ── SWING BUY ────────────────────────────────
            if allow_buy and not already_sent(ticker, "SWING", "BUY"):
                sig = check_swing(ticker, price, ihsg, side="BUY")
                if sig: signals.append(sig)

            # ── SWING SELL ───────────────────────────────
            if allow_sell and not already_sent(ticker, "SWING", "SELL"):
                sig = check_swing(ticker, price, ihsg, side="SELL")
                if sig: signals.append(sig)

            time.sleep(0.5)   # rate limit yfinance — jangan flood

        except Exception as e:
            log(f"⚠️ [{ticker}]: {e}", "warn")
            continue

    buy_cand  = sum(1 for s in signals if s["side"] == "BUY")
    sell_cand = sum(1 for s in signals if s["side"] == "SELL")
    log(f"\n📊 Scanned: {scanned} | Vol filter: {skip_vol} | "
        f"Kandidat: {len(signals)} (BUY:{buy_cand} SELL:{sell_cand})")

    if not signals:
        tg(f"🔍 <b>Scan Selesai — Signal Bot Saham IDX v1.0</b>\n"
           f"━━━━━━━━━━━━━━━━━━\n"
           f"Saham di-scan : <b>{scanned}</b>\n"
           f"IHSG 1d       : <b>{ihsg['ihsg_1d']:+.1f}%</b>\n"
           f"BUY           : {'aktif' if allow_buy else 'diblokir (IHSG drop)'}\n"
           f"Tidak ada sinyal memenuhi kriteria saat ini.\n"
           f"<i>Bot akan scan lagi dalam 4 jam.</i>")
        log("📭 Tidak ada signal"); return

    # Sort: tier terbaik dulu, lalu score tertinggi
    tier_order = {"S": 0, "A+": 1, "A": 2}
    signals.sort(key=lambda x: (tier_order.get(x["tier"], 9), -x["score"]))

    sent = 0
    for sig in signals:
        if sent >= MAX_SIGNALS_CYCLE: break
        send_signal(sig)
        save_signal(
            sig["ticker"], sig["strategy"], sig["side"],
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

    tg(f"🔍 <b>Scan Selesai — Signal Bot Saham IDX v1.0</b>\n"
       f"━━━━━━━━━━━━━━━━━━\n"
       f"Saham di-scan : <b>{scanned}</b>\n"
       f"IHSG 1d/5d    : <b>{ihsg['ihsg_1d']:+.1f}% / {ihsg['ihsg_5d']:+.1f}%</b>\n"
       f"━━━━━━━━━━━━━━━━━━\n"
       f"Signal terkirim : <b>{sent}</b>\n"
       f"  📈 INTRADAY BUY  : {intraday_buy}\n"
       f"  📉 INTRADAY SELL : {intraday_sell}\n"
       f"  🌊 SWING BUY     : {swing_buy}\n"
       f"  🌊 SWING SELL    : {swing_sell}\n"
       f"<i>Scan berikutnya dalam 4 jam.</i>")

    log(f"\n✅ Done — {sent} signal terkirim "
        f"(INTRADAY: BUY:{intraday_buy} SELL:{intraday_sell} | "
        f"SWING: BUY:{swing_buy} SELL:{swing_sell})")


if __name__ == "__main__":
    run()

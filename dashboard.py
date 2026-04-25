# dashboard.py
from flask import Blueprint, jsonify, render_template
from datetime import datetime
import asyncio
import time

# Create the blueprint
dashboard_bp = Blueprint('dashboard', __name__, template_folder='templates')

# These will be set when init_dashboard() is called from main.py
_state_lock = None
_run_async = None
_fetch_account_balance = None
_get_current_spread = None
_get_positions_for_api = None
MY_ACC_ID = None
GOLD_SUFFIX = None
MAX_SPREAD_PIPS = None
daily_start_balance = None
cooldown_until = None
recent_signals = None
post_tp1_active_global = None
tp1_hit_tracking_global = None
tp_removed_global = None

# We'll reuse the connection helper and PIP_VALUE from main.py
# but since main.py is not imported, we rely on get_connection being callable.
# The actual get_connection is already accessible through the event loop.
# We'll need a helper to run async functions – we already have _run_async.

# We'll define a constant for the symbol used in market state
GOLD_SYMBOL = "XAUUSD"

def init_dashboard(state_lock, run_async, fetch_account_balance, get_current_spread,
                   get_positions_for_api, my_acc_id, gold_suffix, max_spread_pips,
                   daily_start_bal, cooldown, recent_sigs,
                   post_tp1_active, tp1_hit_tracking, tp_removed):
    global _state_lock, _run_async, _fetch_account_balance, _get_current_spread
    global _get_positions_for_api, MY_ACC_ID, GOLD_SUFFIX, MAX_SPREAD_PIPS
    global daily_start_balance, cooldown_until, recent_signals
    global post_tp1_active_global, tp1_hit_tracking_global, tp_removed_global, GOLD_SYMBOL

    _state_lock = state_lock
    _run_async = run_async
    _fetch_account_balance = fetch_account_balance
    _get_current_spread = get_current_spread
    _get_positions_for_api = get_positions_for_api
    MY_ACC_ID = my_acc_id
    GOLD_SUFFIX = gold_suffix
    MAX_SPREAD_PIPS = max_spread_pips
    daily_start_balance = daily_start_bal
    cooldown_until = cooldown
    recent_signals = recent_sigs
    post_tp1_active_global = post_tp1_active
    tp1_hit_tracking_global = tp1_hit_tracking
    tp_removed_global = tp_removed
    GOLD_SYMBOL = "XAUUSD" + GOLD_SUFFIX

# ------------------------------------------------------------------
# Helper to get MetaApi connection (reuses the same event loop)
# We assume the main loop is already running and get_connection is defined in main.py.
# Since we only have the event loop, we can use the fact that main.py's get_connection
# is already registered in the global scope of the running app. We'll access it via
# the imported module indirectly. For simplicity, we call the same async function
# through run_async, and we'll define a local async function that does the work.
# To make it clean, we'll add a small async function here that mimics the logic.
# ------------------------------------------------------------------

async def _get_connection_async(account_id):
    # This is a duplicate of the get_connection logic from main.py.
    # Since we don't have direct access to the connection cache, we can
    # rely on the fact that main.py already keeps connections alive.
    # However, for the dashboard we'll just use the one from main.
    # A hack: import the connections dict from main (but that would create
    # a circular dependency). Instead, we'll use the keep_connection_alive
    # already running and just call get_connection via the event loop.
    # The simplest path is to call the original get_connection from main.py
    # by using the fact it's accessible through the global namespace of the
    # running script. We'll dynamically fetch it.
    import main as main_module
    return await main_module.get_connection(account_id)

# ------------------------------------------------------------------
# Market state calculation (real-time indicators)
# ------------------------------------------------------------------
async def _compute_market_state():
    """Calculate current market indicators from 5‑minute candles."""
    try:
        conn = await _get_connection_async(MY_ACC_ID)
        # Fetch 100 candles of 5‑minute for XAUUSDm
        candles = await conn.get_candles(GOLD_SYMBOL, '5m', 100)
        if not candles or len(candles) < 50:
            return None

        closes = [c['close'] for c in candles]
        highs = [c['high'] for c in candles]
        lows = [c['low'] for c in candles]

        # EMAs
        def ema(data, length):
            alpha = 2 / (length + 1)
            ema_vals = [data[0]]
            for x in data[1:]:
                ema_vals.append(alpha * x + (1 - alpha) * ema_vals[-1])
            return ema_vals

        ema9_vals = ema(closes, 9)
        ema21_vals = ema(closes, 21)

        latest_ema9 = ema9_vals[-1]
        latest_ema21 = ema21_vals[-1]
        prev_ema9 = ema9_vals[-2]
        prev_ema21 = ema21_vals[-2]

        # EMA cross status
        ema_cross = "BULL" if latest_ema9 > latest_ema21 else "BEAR"

        # Simple VWAP approximation (from 5m typical price)
        typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
        volumes = [c['volume'] for c in candles]
        vwap = sum(t * v for t, v in zip(typical_prices, volumes)) / sum(volumes) if sum(volumes) > 0 else 0
        price_vs_vwap = "ABOVE" if closes[-1] > vwap else "BELOW"

        # RSI 14
        def rsi(data, period=14):
            gains = [max(data[i] - data[i-1], 0) for i in range(1, len(data))]
            losses = [max(data[i-1] - data[i], 0) for i in range(1, len(data))]
            avg_gain = sum(gains[:period]) / period
            avg_loss = sum(losses[:period]) / period
            rs_values = []
            for i in range(period, len(data)-1):
                avg_gain = (avg_gain * (period-1) + gains[i]) / period
                avg_loss = (avg_loss * (period-1) + losses[i]) / period
                rs = avg_gain / avg_loss if avg_loss != 0 else 100
                rs_values.append(100 - (100 / (1 + rs)))
            return rs_values[-1] if rs_values else 50.0

        rsi_val = rsi(closes)
        rsi5m_val = rsi_val  # Since we're already on 5m

        # MACD (12,26,9)
        def ema_series(data, length):
            return ema(data, length)

        ema12 = ema_series(closes, 12)
        ema26 = ema_series(closes, 26)
        macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        signal_line = ema_series(macd_line, 9)
        macd_status = "BULL" if macd_line[-1] > signal_line[-1] else "BEAR"

        # ADX (14)
        def dmi(highs, lows, closes, period=14):
            tr = []
            plus_dm = []
            minus_dm = []
            for i in range(1, len(highs)):
                h_diff = highs[i] - highs[i-1]
                l_diff = lows[i-1] - lows[i]
                tr.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
                plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
                minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
            atr = ema_series(tr, period)[-1]
            plus_di = (ema_series(plus_dm, period)[-1] / atr) * 100
            minus_di = (ema_series(minus_dm, period)[-1] / atr) * 100
            dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
            adx = ema_series([dx], period)[-1]
            return adx, plus_di, minus_di

        adx_val, plus_di, minus_di = dmi(highs, lows, closes)

        # Smart filter logic (replicate Pine script)
        ema50_vals = ema(closes, 50)
        ema50 = ema50_vals[-1]
        price = closes[-1]
        htf_bull = price > ema50
        htf_bear = price < ema50

        # Weak/strong based on ADX
        strong_trend = adx_val > 25
        weak_trend = adx_val < 20

        # Reversal detection (simplified: EMA slope flattening + price > EMA9)
        ema_slope = ema50_vals[-1] - ema50_vals[-4]  # 3‑bar slope
        slope_prev = ema50_vals[-2] - ema50_vals[-5] if len(ema50_vals) >= 5 else 0
        trend_weakening_bear = htf_bear and ema_slope > slope_prev and price > latest_ema9
        trend_weakening_bull = htf_bull and ema_slope < slope_prev and price < latest_ema9

        allow_buy = htf_bull or (htf_bear and trend_weakening_bear)
        allow_sell = htf_bear or (htf_bull and trend_weakening_bull)

        # Smart filter status string
        if allow_buy and allow_sell:
            smart_filter = "REVERSAL WINDOW" if (htf_bear and trend_weakening_bear) or (htf_bull and trend_weakening_bull) else "NEUTRAL"
        elif allow_buy:
            smart_filter = "BULL BIAS"
        elif allow_sell:
            smart_filter = "BEAR BIAS"
        else:
            smart_filter = "BLOCKED"

        # Bias scores (simplified: count bullish/bearish conditions)
        b_score = 0
        b_max = 0
        b_score += 1 if price > vwap else 0; b_max += 1
        b_score += 1 if rsi_val > 50 else 0; b_max += 1
        b_score += 1 if macd_line[-1] > signal_line[-1] else 0; b_max += 1
        b_score += 1 if latest_ema9 > latest_ema21 else 0; b_max += 1
        b_score += 1 if (adx_val > 25 and price > latest_ema9) else 0; b_max += 1
        b_score += 1 if rsi5m_val > 50 else 0; b_max += 1
        bull_pct = (b_score / b_max * 100) if b_max else 0

        r_score = 0
        r_max = 0
        r_score += 1 if price < vwap else 0; r_max += 1
        r_score += 1 if rsi_val < 50 else 0; r_max += 1
        r_score += 1 if macd_line[-1] < signal_line[-1] else 0; r_max += 1
        r_score += 1 if latest_ema9 < latest_ema21 else 0; r_max += 1
        r_score += 1 if (adx_val > 25 and price < latest_ema9) else 0; r_max += 1
        r_score += 1 if rsi5m_val < 50 else 0; r_max += 1
        bear_pct = (r_score / r_max * 100) if r_max else 0

        # Bias text
        diff = bull_pct - bear_pct
        if diff >= 40:
            bias_text = "STRONG BULL"
        elif diff <= -40:
            bias_text = "STRONG BEAR"
        elif diff > 0:
            bias_text = "MILD BULL"
        else:
            bias_text = "MILD BEAR"

        # Trend strength label
        if adx_val > 30:
            trend_strength = "STRONG"
        elif adx_val > 22:
            trend_strength = "MODERATE"
        else:
            trend_strength = "WEAK"

        # Logic dots (just color status for the 9 indicators)
        # We'll create a dict with dot colors for EMA, MACD, ADX Pwr, etc.
        dots = {
            "EMA": "red-glow" if ema_cross == "BEAR" else "green-glow",
            "MACD": "red-glow" if macd_status == "BEAR" else "green-glow",
            "ADX Pwr": "green-glow" if adx_val > 25 else "gray-dot",
            "Hard ADX": "gray-dot",  # not used
            "Vol Stat": "green-glow" if sum(volumes[-5:]) > sum(volumes[-10:-5]) else "gray-dot",
            "Trend Pwr": "green-glow" if trend_strength == "STRONG" else ("red-glow" if trend_strength == "WEAK" else "gray-dot"),
            "VWAP": "red-glow" if price < vwap else "green-glow",
            "Trend": "red-glow" if htf_bear else "green-glow",
            "Smart Filter": smart_filter
        }

        return {
            "bull_pct": round(bull_pct),
            "bear_pct": round(bear_pct),
            "adx": round(adx_val, 1),
            "bias": bias_text,
            "smart_filter": smart_filter,
            "trend": trend_strength,
            "ema_cross": ema_cross,
            "macd_status": macd_status,
            "rsi": round(rsi_val, 1),
            "dots": dots,
            "status": "LIVE"
        }
    except Exception as e:
        print(f"Market state error: {e}")
        return None

# ------------------------------------------------------------------
# API ENDPOINTS (updated)
# ------------------------------------------------------------------

@dashboard_bp.route('/api/status')
def api_status():
    with _state_lock:
        in_cooldown = cooldown_until is not None and datetime.utcnow() < cooldown_until
    return jsonify({
        "online": True,
        "cooldown": in_cooldown,
        "time_utc": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    })

@dashboard_bp.route('/api/account')
def api_account():
    balance = _run_async(_fetch_account_balance(MY_ACC_ID))
    with _state_lock:
        today_str = datetime.utcnow().strftime('%Y-%m-%d')
        start_bal = daily_start_balance.get(today_str, balance)
    if balance is None:
        balance = 0.0
    pnl_dollar = balance - start_bal
    pnl_pct = (pnl_dollar / start_bal * 100) if start_bal > 0 else 0.0
    return jsonify({
        "balance": balance,
        "daily_start_balance": start_bal,
        "daily_pnl_dollar": round(pnl_dollar, 2),
        "daily_pnl_percent": round(pnl_pct, 2)
    })

@dashboard_bp.route('/api/positions')
def api_positions():
    try:
        positions = _run_async(_get_positions_for_api(MY_ACC_ID))
        return jsonify(positions)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@dashboard_bp.route('/api/spread')
def api_spread():
    symbol = GOLD_SYMBOL
    spread_pips = _run_async(_get_current_spread(symbol))
    return jsonify({
        "symbol": symbol,
        "spread_pips": round(spread_pips, 2),
        "max_spread_pips": MAX_SPREAD_PIPS,
        "is_safe": spread_pips <= MAX_SPREAD_PIPS
    })

@dashboard_bp.route('/api/signals')
def api_signals():
    with _state_lock:
        return jsonify(list(recent_signals))

@dashboard_bp.route('/api/manager')
def api_manager():
    with _state_lock:
        groups = {}
        all_keys = set()
        all_keys.update(post_tp1_active_global)
        all_keys.update(tp1_hit_tracking_global)
        for key in all_keys:
            groups[key] = {
                "force_be_done": key in tp1_hit_tracking_global,
                "tp_removed": key in tp_removed_global,
                "trailing_active": key in post_tp1_active_global
            }
    return jsonify(groups)

# NEW: Real‑time market intelligence
@dashboard_bp.route('/api/market')
def api_market():
    # Run the async computation
    data = _run_async(_compute_market_state())
    if data is None:
        # Fallback to last signal if computation fails
        with _state_lock:
            if not recent_signals:
                return jsonify({
                    "bull_pct": 0,
                    "bear_pct": 0,
                    "adx": 0,
                    "bias": "NO SIGNALS",
                    "smart_filter": "OFF",
                    "trend": "WEAK",
                    "status": "WAITING"
                })
            last = recent_signals[-1]
            return jsonify({
                "bull_pct": float(last.get("bull_pct", 0)),
                "bear_pct": float(last.get("bear_pct", 0)),
                "adx": float(last.get("adx", 0)),
                "bias": last.get("bias_text", "NEUTRAL"),
                "smart_filter": last.get("smart_filter", "OFF"),
                "trend": last.get("trend_strength", "WEAK"),
                "status": "FALLBACK"
            })
    return jsonify(data)

@dashboard_bp.route('/api/performance')
def api_performance():
    with _state_lock:
        total = len(recent_signals)
        if total == 0:
            return jsonify({
                "total_trades": 0,
                "executed": 0,
                "blocked": 0,
                "error": 0,
                "win_rate": 0,
                "profit_factor": 0,
                "max_drawdown_percent": 0,
                "avg_rr": 0,
                "recovery_factor": 0
            })
        executed = sum(1 for s in recent_signals if s.get("status") == "EXECUTED")
        blocked = sum(1 for s in recent_signals if s.get("status", "").startswith("BLOCKED"))
        errors = total - executed - blocked
        return jsonify({
            "total_trades": total,
            "executed": executed,
            "blocked": blocked,
            "error": errors,
            "win_rate": 0,
            "profit_factor": 0,
            "max_drawdown_percent": 0,
            "avg_rr": 0,
            "recovery_factor": 0
        })

@dashboard_bp.route('/dashboard')
def dashboard_page():
    return render_template('dashboard.html')
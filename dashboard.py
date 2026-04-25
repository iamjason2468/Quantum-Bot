# dashboard.py
from flask import Blueprint, jsonify, render_template
from datetime import datetime
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
_get_connection_raw = None  # <-- This will hold the MetaApi connection function


def init_dashboard(state_lock, run_async, fetch_account_balance, get_current_spread,
                   get_positions_for_api, my_acc_id, gold_suffix, max_spread_pips,
                   daily_start_bal, cooldown, recent_sigs,
                   post_tp1_active, tp1_hit_tracking, tp_removed,
                   get_connection_raw=None):   # <-- receives the function
    """Call this from main.py to connect the dashboard to the bot's state."""
    global _state_lock, _run_async, _fetch_account_balance, _get_current_spread
    global _get_positions_for_api, MY_ACC_ID, GOLD_SUFFIX, MAX_SPREAD_PIPS
    global daily_start_balance, cooldown_until, recent_signals
    global post_tp1_active_global, tp1_hit_tracking_global, tp_removed_global
    global _get_connection_raw

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
    _get_connection_raw = get_connection_raw


# ------------------------------------------------------------------
# Market state calculation (real-time indicators)
# ------------------------------------------------------------------
async def _compute_market_state():
    """Calculate current market indicators from 5‑minute candles using existing MetaApi connection."""
    try:
        if _get_connection_raw is None:
            return None

        conn = await _get_connection_raw(MY_ACC_ID)
        symbol = "XAUUSD" + GOLD_SUFFIX

        # Fetch 100 candles of 5‑minute
        candles = await conn.get_candles(symbol, '5m', 100)
        if not candles or len(candles) < 50:
            return None

        closes = [c['close'] for c in candles]
        highs = [c['high'] for c in candles]
        lows = [c['low'] for c in candles]
        volumes = [c.get('volume', 1) for c in candles]

        # Helper: EMA
        def ema(data, length):
            alpha = 2 / (length + 1)
            result = [data[0]]
            for x in data[1:]:
                result.append(alpha * x + (1 - alpha) * result[-1])
            return result

        # EMA9 and EMA21
        ema9_vals = ema(closes, 9)
        ema21_vals = ema(closes, 21)
        latest_ema9 = ema9_vals[-1]
        latest_ema21 = ema21_vals[-1]
        ema_cross = "BULL" if latest_ema9 > latest_ema21 else "BEAR"

        # VWAP
        typical = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
        vwap = sum(t * v for t, v in zip(typical, volumes)) / sum(volumes) if sum(volumes) > 0 else closes[-1]
        price = closes[-1]

        # RSI 14
        gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
        avg_gain = sum(gains[:14]) / 14
        avg_loss = sum(losses[:14]) / 14
        if avg_loss == 0:
            rsi_val = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_val = 100 - (100 / (1 + rs))

        # MACD (12,26,9)
        ema12 = ema(closes, 12)
        ema26 = ema(closes, 26)
        macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        signal_line = ema(macd_line, 9)
        macd_bull = macd_line[-1] > signal_line[-1]

        # ADX (14) simplified
        tr_vals = []
        plus_dm = []
        minus_dm = []
        for i in range(1, len(highs)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            tr_vals.append(tr)
            up = highs[i] - highs[i-1]
            down = lows[i-1] - lows[i]
            plus_dm.append(up if up > down and up > 0 else 0)
            minus_dm.append(down if down > up and down > 0 else 0)
        atr_val = ema(tr_vals, 14)[-1] if len(tr_vals) >= 14 else 1
        plus_di = (ema(plus_dm, 14)[-1] / atr_val * 100) if atr_val > 0 else 0
        minus_di = (ema(minus_dm, 14)[-1] / atr_val * 100) if atr_val > 0 else 0
        dx_sum = abs(plus_di - minus_di)
        dx_den = plus_di + minus_di
        dx = dx_sum / dx_den * 100 if dx_den > 0 else 0
        adx_val = dx

        # Smart Filter
        ema50_vals = ema(closes, 50)
        ema50 = ema50_vals[-1]
        htf_bull = price > ema50
        htf_bear = price < ema50
        strong_trend = adx_val > 25
        weak_trend = adx_val < 20

        slope_now = ema50_vals[-1] - ema50_vals[-4] if len(ema50_vals) >= 4 else 0
        slope_prev = ema50_vals[-2] - ema50_vals[-5] if len(ema50_vals) >= 5 else 0

        weakening_bear = htf_bear and slope_now > slope_prev and price > latest_ema9
        weakening_bull = htf_bull and slope_now < slope_prev and price < latest_ema9

        allow_buy = htf_bull or (htf_bear and weakening_bear)
        allow_sell = htf_bear or (htf_bull and weakening_bull)

        if allow_buy and allow_sell:
            smart_filter = "REVERSAL WINDOW" if (weakening_bear or weakening_bull) else "NEUTRAL"
        elif allow_buy:
            smart_filter = "BULL BIAS"
        elif allow_sell:
            smart_filter = "BEAR BIAS"
        else:
            smart_filter = "BLOCKED"

        # Bias scores (simplified)
        b_score = sum([
            price > vwap,
            rsi_val > 50,
            macd_bull,
            latest_ema9 > latest_ema21,
            adx_val > 25 and price > latest_ema9,
            rsi_val > 50  # rsi5m same as RSI14 for simplicity
        ])
        r_score = sum([
            price < vwap,
            rsi_val < 50,
            not macd_bull,
            latest_ema9 < latest_ema21,
            adx_val > 25 and price < latest_ema9,
            rsi_val < 50
        ])
        bull_pct = round(b_score / 6 * 100)
        bear_pct = round(r_score / 6 * 100)

        diff = bull_pct - bear_pct
        if diff >= 40:
            bias_text = "STRONG BULL"
        elif diff <= -40:
            bias_text = "STRONG BEAR"
        elif diff > 0:
            bias_text = "MILD BULL"
        else:
            bias_text = "MILD BEAR"

        if adx_val > 30:
            trend_strength = "STRONG"
        elif adx_val > 22:
            trend_strength = "MODERATE"
        else:
            trend_strength = "WEAK"

        return {
            "bull_pct": bull_pct,
            "bear_pct": bear_pct,
            "adx": round(adx_val, 1),
            "bias": bias_text,
            "smart_filter": smart_filter,
            "trend": trend_strength,
            "ema_cross": ema_cross,
            "macd_bull": macd_bull,
            "rsi": round(rsi_val, 1),
            "vwap_bull": price > vwap,
            "status": "LIVE"
        }
    except Exception as e:
        print(f"Market state error: {e}")
        return None


# ------------------------------------------------------------------
# API ENDPOINTS
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
    symbol = "XAUUSD" + GOLD_SUFFIX
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


@dashboard_bp.route('/api/market')
def api_market():
    data = _run_async(_compute_market_state())
    if data is None:
        # Fallback to last signal
        with _state_lock:
            if not recent_signals:
                return jsonify({
                    "bull_pct": 0, "bear_pct": 0, "adx": 0,
                    "bias": "NO SIGNALS", "smart_filter": "OFF",
                    "trend": "WEAK", "status": "WAITING"
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
                "total_trades": 0, "executed": 0, "blocked": 0,
                "error": 0, "win_rate": 0, "profit_factor": 0,
                "max_drawdown_percent": 0, "avg_rr": 0, "recovery_factor": 0
            })
        executed = sum(1 for s in recent_signals if s.get("status") == "EXECUTED")
        blocked = sum(1 for s in recent_signals if s.get("status", "").startswith("BLOCKED"))
        errors = total - executed - blocked
        return jsonify({
            "total_trades": total, "executed": executed,
            "blocked": blocked, "error": errors,
            "win_rate": 0, "profit_factor": 0,
            "max_drawdown_percent": 0, "avg_rr": 0, "recovery_factor": 0
        })


@dashboard_bp.route('/dashboard')
def dashboard_page():
    return render_template('dashboard.html')
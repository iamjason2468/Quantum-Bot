# dashboard.py
from flask import Blueprint, jsonify, render_template, request
from datetime import datetime
import time
import asyncio

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
_get_connection_raw = None
_market_data = None          # live market state from TradingView (5m)
_equity_history = None       # deque of balance snapshots
_closed_trade_pnls = None    # deque of closed trade P&L values

# Cache for per‑timeframe market states (MetaApi‑computed)
_market_cache = {}
_market_cache_ttl = 60


def init_dashboard(state_lock, run_async, fetch_account_balance, get_current_spread,
                   get_positions_for_api, my_acc_id, gold_suffix, max_spread_pips,
                   daily_start_bal, cooldown, recent_sigs,
                   post_tp1_active, tp1_hit_tracking, tp_removed,
                   get_connection_raw=None, market_data=None, equity_history=None,
                   closed_trade_pnls=None):
    """Call this from main.py to connect the dashboard to the bot's state."""
    global _state_lock, _run_async, _fetch_account_balance, _get_current_spread
    global _get_positions_for_api, MY_ACC_ID, GOLD_SUFFIX, MAX_SPREAD_PIPS
    global daily_start_balance, cooldown_until, recent_signals
    global post_tp1_active_global, tp1_hit_tracking_global, tp_removed_global
    global _get_connection_raw, _market_data, _equity_history, _closed_trade_pnls

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
    _market_data = market_data
    _equity_history = equity_history
    _closed_trade_pnls = closed_trade_pnls


# ------------------------------------------------------------------
# Market state computation (reusable, with caching)
# ------------------------------------------------------------------
async def compute_market_state_for_tf(symbol, timeframe="5m"):
    """Fetch candles and calculate indicators for a given timeframe."""
    cache_key = f"{symbol}_{timeframe}"
    now = time.time()
    if cache_key in _market_cache and (now - _market_cache[cache_key].get("ts", 0)) < _market_cache_ttl:
        return _market_cache[cache_key]["data"]

    if _get_connection_raw is None:
        return None

    conn = await _get_connection_raw(MY_ACC_ID)
    candles = await conn.get_candles(symbol, timeframe, 100)
    if not candles or len(candles) < 50:
        return None

    closes = [c['close'] for c in candles]
    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]
    volumes = [c.get('volume', 1) for c in candles]

    def ema(data, length):
        alpha = 2 / (length + 1)
        res = [data[0]]
        for x in data[1:]:
            res.append(alpha * x + (1 - alpha) * res[-1])
        return res

    ema9_vals = ema(closes, 9)
    ema21_vals = ema(closes, 21)
    latest_ema9 = ema9_vals[-1]
    latest_ema21 = ema21_vals[-1]
    ema_cross = "BULL" if latest_ema9 > latest_ema21 else "BEAR"

    typical = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    vwap = sum(t * v for t, v in zip(typical, volumes)) / sum(volumes) if sum(volumes) > 0 else closes[-1]
    price = closes[-1]

    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:14]) / 14
    avg_loss = sum(losses[:14]) / 14
    rsi_val = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    signal_line = ema(macd_line, 9)
    macd_bull = macd_line[-1] > signal_line[-1]

    tr_vals, plus_dm, minus_dm = [], [], []
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
    adx_val = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) > 0 else 0

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

    b_score = sum([price > vwap, rsi_val > 50, macd_bull, latest_ema9 > latest_ema21,
                   adx_val > 25 and price > latest_ema9, rsi_val > 50])
    r_score = sum([price < vwap, rsi_val < 50, not macd_bull, latest_ema9 < latest_ema21,
                   adx_val > 25 and price < latest_ema9, rsi_val < 50])
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

    trend_strength = "STRONG" if adx_val > 30 else "MODERATE" if adx_val > 22 else "WEAK"

    result = {
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
    _market_cache[cache_key] = {"data": result, "ts": now}
    return result


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
    timeframe = request.args.get('tf', '5m')
    symbol = "XAUUSD" + GOLD_SUFFIX
    data = _run_async(compute_market_state_for_tf(symbol, timeframe))
    if data:
        return jsonify(data)
    if _market_data is not None:
        return jsonify(_market_data)
    return jsonify({
        "bull_pct": 0, "bear_pct": 0, "adx": 0,
        "bias": "NO DATA", "smart_filter": "OFF",
        "trend": "WEAK", "status": "WAITING"
    })


@dashboard_bp.route('/api/indicators')
def api_indicators():
    symbol = "XAUUSD" + GOLD_SUFFIX
    data = _run_async(compute_market_state_for_tf(symbol, "5m"))
    if not data:
        return jsonify({
            "ema": "neutral", "macd": "neutral", "adx_power": "neutral",
            "hard_adx": "off", "vol_stat": "neutral", "trend_power": "neutral",
            "vwap": "neutral", "trend": "neutral"
        })
    return jsonify({
        "ema": data.get("ema_cross", "BEAR").lower(),
        "macd": "bull" if data.get("macd_bull") else "bear",
        "adx_power": "strong" if data.get("adx", 0) > 25 else "moderate",
        "hard_adx": "off",
        "vol_stat": "high" if data.get("vwap_bull") else "low",
        "trend_power": data.get("trend", "WEAK").lower(),
        "vwap": "bull" if data.get("vwap_bull") else "bear",
        "trend": "bull" if data.get("bull_pct", 0) > 50 else "bear"
    })


@dashboard_bp.route('/api/equity_history')
def api_equity_history():
    if _equity_history is None:
        return jsonify([])
    return jsonify(list(_equity_history))


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

        # Real trade outcomes from closed trade P&Ls
        win_rate = 0
        profit_factor = 0
        avg_rr = 0
        net_profit = 0
        recovery_factor = 0

        if _closed_trade_pnls and len(_closed_trade_pnls) > 0:
            wins = [p for p in _closed_trade_pnls if p > 0]
            losses = [abs(p) for p in _closed_trade_pnls if p < 0]
            win_rate = round(len(wins) / len(_closed_trade_pnls) * 100, 1)

            gross_profit = sum(wins)
            gross_loss = sum(losses)
            if gross_loss > 0:
                profit_factor = round(gross_profit / gross_loss, 2)

            if len(losses) > 0 and len(wins) > 0:
                avg_win = sum(wins) / len(wins)
                avg_loss = sum(losses) / len(losses)
                avg_rr = round(avg_win / avg_loss, 2)

            net_profit = sum(_closed_trade_pnls)

        # Max drawdown from equity curve
        max_dd = 0
        if _equity_history and len(_equity_history) > 1:
            balances = [h['balance'] for h in _equity_history]
            peak = balances[0]
            for b in balances:
                if b > peak:
                    peak = b
                dd = (peak - b) / peak * 100 if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd

        # Recovery factor = net profit / max drawdown
        if max_dd > 0 and net_profit != 0 and len(_closed_trade_pnls) > 0:
            # Convert drawdown to dollar amount using initial balance
            if _equity_history and len(_equity_history) > 0:
                start_balance = _equity_history[0]['balance']
                max_dd_dollar = max_dd / 100 * start_balance
                if max_dd_dollar > 0:
                    recovery_factor = round(net_profit / max_dd_dollar, 2)

        return jsonify({
            "total_trades": total,
            "executed": executed,
            "blocked": blocked,
            "error": errors,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "max_drawdown_percent": round(max_dd, 2),
            "avg_rr": avg_rr,
            "recovery_factor": recovery_factor
        })


@dashboard_bp.route('/dashboard')
def dashboard_page():
    return render_template('dashboard.html')
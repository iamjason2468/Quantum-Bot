# dashboard.py – webhook only, no candle fetching
from flask import Blueprint, jsonify, render_template, request
from datetime import datetime

dashboard_bp = Blueprint('dashboard', __name__, template_folder='templates')

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
_market_data = None
_equity_history = None
_closed_trade_pnls = None


def init_dashboard(state_lock, run_async, fetch_account_balance, get_current_spread,
                   get_positions_for_api, my_acc_id, gold_suffix, max_spread_pips,
                   daily_start_bal, cooldown, recent_sigs,
                   post_tp1_active, tp1_hit_tracking, tp_removed,
                   get_connection_raw=None, market_data=None, equity_history=None,
                   closed_trade_pnls=None):
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
    # Return the latest market data from TradingView webhook
    if _market_data is not None and _market_data.get("status") != "WAITING":
        return jsonify(_market_data)
    return jsonify({
        "bull_pct": 0,
        "bear_pct": 0,
        "adx": 0,
        "bias": "NO DATA",
        "smart_filter": "OFF",
        "trend": "WEAK",
        "status": "WAITING"
    })


@dashboard_bp.route('/api/indicators')
def api_indicators():
    # If no data yet, return neutral defaults
    if _market_data is None:
        return jsonify({
            "ema": "neutral", "macd": "neutral", "adx_power": "neutral",
            "hard_adx": "off", "vol_stat": "neutral", "trend_power": "neutral",
            "vwap": "neutral", "trend": "neutral"
        })
    
    # ⭐ NOW PULLING EXACT VALUES FROM TRADINGVIEW (via main.py → _market_data)
    adx = _market_data.get("adx", 0)
    
    return jsonify({
        "ema": _market_data.get("ema", "neutral"),           # "bull" or "bear" from Pine Script
        "macd": _market_data.get("macd", "neutral"),         # "bull" or "bear" from Pine Script
        "adx_power": "strong" if adx > 25 else "moderate",
        "hard_adx": _market_data.get("hard_adx", "off"),     # "pass", "block", or "off" from Pine Script
        "vol_stat": _market_data.get("vol_stat", "neutral"), # "high" or "low" from Pine Script
        "trend_power": _market_data.get("trend", "weak").lower(),  # "strong", "moderate", "weak" from Pine Script
        "vwap": _market_data.get("vwap", "neutral"),         # "bull" or "bear" from Pine Script
        "trend": "bull" if "BULL" in _market_data.get("bias", "NEUTRAL").upper() else "bear"
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

        win_rate = 0
        profit_factor = 0
        avg_rr = 0
        net_profit = 0
        recovery_factor = 0

        if _closed_trade_pnls and len(_closed_trade_pnls) > 0:
            wins = [p for p in _closed_trade_pnls if p > 0]
            losses = [abs(p) for p in _closed_trade_pnls if p < 0]
            if len(_closed_trade_pnls) > 0:
                win_rate = round(len(wins) / len(_closed_trade_pnls) * 100, 1)
            gross_profit = sum(wins)
            gross_loss = sum(losses)
            if gross_loss > 0:
                profit_factor = round(gross_profit / gross_loss, 2)
            if len(losses) > 0 and len(wins) > 0:
                avg_rr = round((sum(wins) / len(wins)) / (sum(losses) / len(losses)), 2)
            net_profit = sum(_closed_trade_pnls)

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

        if max_dd > 0 and net_profit != 0 and _equity_history and len(_equity_history) > 0:
            max_dd_dollar = max_dd / 100 * _equity_history[0]['balance']
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
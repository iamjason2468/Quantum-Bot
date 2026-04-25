# dashboard.py
from flask import Blueprint, jsonify, render_template
from datetime import datetime

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


def init_dashboard(state_lock, run_async, fetch_account_balance, get_current_spread,
                   get_positions_for_api, my_acc_id, gold_suffix, max_spread_pips,
                   daily_start_bal, cooldown, recent_sigs,
                   post_tp1_active, tp1_hit_tracking, tp_removed):
    """Call this from main.py to connect the dashboard to the bot's state."""
    global _state_lock, _run_async, _fetch_account_balance, _get_current_spread
    global _get_positions_for_api, MY_ACC_ID, GOLD_SUFFIX, MAX_SPREAD_PIPS
    global daily_start_balance, cooldown_until, recent_signals
    global post_tp1_active_global, tp1_hit_tracking_global, tp_removed_global

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


# ------------------------------------------------------------------
# NEW: Market Intelligence (for Elite 11 card)
# ------------------------------------------------------------------
@dashboard_bp.route('/api/market')
def api_market():
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
            "status": last.get("status", "?")
        })


# ------------------------------------------------------------------
# NEW: Performance Stats (for Performance Breakdown)
# ------------------------------------------------------------------
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
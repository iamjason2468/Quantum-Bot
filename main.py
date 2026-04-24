import os
import asyncio
import logging
import json
import threading
import time
from datetime import datetime, timedelta
from collections import deque
from flask import Flask, request, jsonify
from metaapi_cloud_sdk import MetaApi

# ------------------------------------------------------------------
# FLASK APP INITIALIZATION
# ------------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# ENVIRONMENT VARIABLES
# ------------------------------------------------------------------
TOKEN = os.getenv('META_API_TOKEN')
MY_ACC_ID = os.getenv('MY_ACCOUNT_ID')
FRIEND_ACC_ID = os.getenv('FRIEND_ACCOUNT_ID')
GOLD_SUFFIX = os.getenv('GOLD_SUFFIX', 'm')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', 'quantum-gold-2026')

LOT_MULTIPLIER = float(os.getenv('LOT_MULTIPLIER', '0.15'))
GOLD_BE_ATR = float(os.getenv('GOLD_BE_ATR', '0.5'))
GOLD_TRAIL_START = float(os.getenv('GOLD_TRAIL_START', '1.0'))
GOLD_TRAIL_DIST = float(os.getenv('GOLD_TRAIL_DIST', '0.4'))
MIN_DISTANCE_PIPS_GOLD = float(os.getenv('MIN_DISTANCE_PIPS_GOLD', '10'))
MAX_LOSS_LOOKBACK_MINUTES = int(os.getenv('MAX_LOSS_LOOKBACK_MINUTES', '60'))
MAX_LOSS_PCT = float(os.getenv('MAX_LOSS_PCT', '2.0'))
COOLDOWN_MINUTES = int(os.getenv('COOLDOWN_MINUTES', '30'))
GOLD_FORCE_BE_AT_TP1 = os.getenv('GOLD_FORCE_BE_AT_TP1', 'true').lower() == 'true'
GOLD_TP1_BE_BUFFER_PCT = float(os.getenv('GOLD_TP1_BE_BUFFER_PCT', '0.2'))
GOLD_REMOVE_TP_AFTER_TP1 = os.getenv('GOLD_REMOVE_TP_AFTER_TP1', 'true').lower() == 'true'

if not TOKEN:
    logger.error("❌ META_API_TOKEN is not set")
if not MY_ACC_ID:
    logger.error("❌ MY_ACCOUNT_ID is not set")

# ------------------------------------------------------------------
# TRACKING STATE
# ------------------------------------------------------------------
last_trade_price = {}
trade_history = deque()
cooldown_until = None
window_start_balance = {}

# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------
PIP_VALUE = 0.1

def get_pip_value(symbol=""):
    return PIP_VALUE

def get_risk_multipliers(symbol=""):
    return (GOLD_BE_ATR, GOLD_TRAIL_START, GOLD_TRAIL_DIST)

def can_trade(symbol, current_price, direction):
    key = f"{symbol}_{direction}"
    if key not in last_trade_price:
        return True
    last_price = last_trade_price[key]
    distance_pips = abs(current_price - last_price) / PIP_VALUE
    if distance_pips >= MIN_DISTANCE_PIPS_GOLD:
        return True
    else:
        logger.info(f"⛔ Signal blocked: Only {distance_pips:.1f} pips from last {direction} trade. Need {MIN_DISTANCE_PIPS_GOLD}")
        return False

def record_trade_result(pnl_amount):
    trade_history.append((datetime.utcnow(), pnl_amount))
    cutoff = datetime.utcnow() - timedelta(minutes=MAX_LOSS_LOOKBACK_MINUTES)
    while trade_history and trade_history[0][0] < cutoff:
        trade_history.popleft()
    for key in list(window_start_balance.keys()):
        key_dt = datetime.strptime(key, '%Y-%m-%d %H:%M')
        if key_dt < cutoff:
            del window_start_balance[key]

def get_rolling_pnl_pct(current_balance):
    if not trade_history or current_balance <= 0:
        return 0.0
    window_start = trade_history[0][0]
    key = window_start.strftime('%Y-%m-%d %H:%M')
    if key not in window_start_balance:
        window_start_balance[key] = current_balance - sum(pnl for _, pnl in trade_history)
    start_bal = window_start_balance[key]
    total_pnl = sum(pnl for _, pnl in trade_history)
    return (total_pnl / start_bal) * 100 if start_bal > 0 else 0.0

def is_in_cooldown():
    global cooldown_until
    if cooldown_until is None:
        return False
    if datetime.utcnow() < cooldown_until:
        remaining = (cooldown_until - datetime.utcnow()).seconds // 60
        logger.info(f"⏸️ In cooldown: {remaining} minutes remaining")
        return True
    else:
        cooldown_until = None
        logger.info("✅ Cooldown ended — resuming trading")
        return False

def check_rolling_loss_limit(current_balance):
    global cooldown_until
    if is_in_cooldown():
        return False
    rolling_pnl_pct = get_rolling_pnl_pct(current_balance)
    if rolling_pnl_pct <= -MAX_LOSS_PCT:
        logger.warning(f"🛑 Rolling loss limit reached: {rolling_pnl_pct:.2f}% (Limit: {MAX_LOSS_PCT}%)")
        cooldown_until = datetime.utcnow() + timedelta(minutes=COOLDOWN_MINUTES)
        logger.warning(f"⏸️ Entering cooldown for {COOLDOWN_MINUTES} minutes")
        return False
    return True

# ------------------------------------------------------------------
# METAAPI INITIALIZATION
# ------------------------------------------------------------------
_metaapi_instance = None
_metaapi_lock = threading.Lock()

def get_metaapi():
    global _metaapi_instance
    if _metaapi_instance is None:
        with _metaapi_lock:
            if _metaapi_instance is None:
                _metaapi_instance = MetaApi(TOKEN, {'region': 'london'})
                logger.info("🔧 MetaApi initialized with region: london")
    return _metaapi_instance

# ------------------------------------------------------------------
# CONNECTION CACHE & KEEPER
# ------------------------------------------------------------------
connections = {}
connection_lock = asyncio.Lock()

async def get_connection(account_id):
    """Replacement for the old get_connection – ensures a live connection."""
    async with connection_lock:
        if account_id in connections:
            conn = connections[account_id]
            try:
                await asyncio.wait_for(conn.get_account_information(), timeout=5.0)
                return conn
            except Exception:
                del connections[account_id]

        logger.info(f"🔌 Establishing new connection for account {account_id}")
        api = get_metaapi()
        account = await api.metatrader_account_api.get_account(account_id)
        if account.state != "DEPLOYED":
            logger.info(f"⏳ Deploying account {account_id}...")
            await account.deploy()
            await account.wait_connected()
        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized()
        connections[account_id] = connection
        return connection

async def keep_connection_alive(account_id):
    """Background task that maintains a live connection."""
    while True:
        try:
            await get_connection(account_id)
            await asyncio.sleep(10)
        except Exception as e:
            logger.warning(f"Connection keeper error: {e}, retrying in 5s")
            await asyncio.sleep(5)

# ------------------------------------------------------------------
# ADAPTIVE LOT SIZING
# ------------------------------------------------------------------
def get_adaptive_lot(account_balance):
    if account_balance < 100:
        base_lot = 0.01
    elif account_balance < 500:
        base_lot = 0.02
    elif account_balance < 1000:
        base_lot = 0.05
    else:
        base_lot = 0.10
    final_lot = base_lot * LOT_MULTIPLIER
    return max(0.01, round(final_lot, 2))

async def fetch_account_balance(account_id):
    try:
        connection = await get_connection(account_id)
        account_info = await connection.get_account_information()
        return account_info.get('balance', 0.0)
    except Exception as e:
        logger.error(f"❌ Could not fetch balance: {e}")
        return None

# ------------------------------------------------------------------
# CLOSE OPPOSITE POSITIONS
# ------------------------------------------------------------------
async def close_opposite_positions(account_id, symbol, new_direction):
    try:
        connection = await get_connection(account_id)
        positions = await connection.get_positions()
        closed_count = 0
        for pos in positions:
            if pos['symbol'] == symbol:
                pos_type = pos['type']
                pos_direction = "BUY" if pos_type == 'POSITION_TYPE_BUY' else "SELL"
                if pos_direction != new_direction:
                    logger.info(f"🔄 Closing opposite position {pos['id']} ({pos_direction} {pos['volume']} lots)")
                    await connection.close_position(pos['id'])
                    closed_count += 1
        if closed_count > 0:
            logger.info(f"✅ Closed {closed_count} opposite position(s)")
        return closed_count
    except Exception as e:
        logger.error(f"❌ Error closing opposite positions: {e}")
        return 0

# ------------------------------------------------------------------
# PLACE TRADE
# ------------------------------------------------------------------
async def place_single_trade(account_id, action, symbol, volume, entry, sl, tp1, tp2, tp3, tp4, include_tp4, be_buffer, use_adaptive=True):
    try:
        connection = await get_connection(account_id)
        await close_opposite_positions(account_id, symbol, action.upper())

        final_volume = volume
        if use_adaptive:
            balance = await fetch_account_balance(account_id)
            if balance is not None:
                final_volume = get_adaptive_lot(balance)
                logger.info(f"📊 Balance: ${balance:.2f} → Lot: {final_volume} (Gold)")

        tp_levels = [tp1, tp2, tp3]
        if include_tp4:
            tp_levels.append(tp4)
            logger.info(f"🔥 Trend Rider active: TP4 = {tp4}")

        num_tps = len(tp_levels)
        volume_per_tp = final_volume / num_tps
        volume_per_tp = max(0.01, round(volume_per_tp, 2))

        results = []
        for idx, tp in enumerate(tp_levels):
            if action.lower() == "buy":
                result = await connection.create_market_buy_order(
                    symbol, volume_per_tp,
                    stop_loss=sl,
                    take_profit=tp
                )
            else:
                result = await connection.create_market_sell_order(
                    symbol, volume_per_tp,
                    stop_loss=sl,
                    take_profit=tp
                )

            pos_id = None
            if isinstance(result, list) and len(result) > 0:
                result = result[0]
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except:
                    result = {}
            if isinstance(result, dict):
                pos_id = result.get('positionId')

            results.append({"tp": tp, "volume": volume_per_tp, "positionId": pos_id})
            logger.info(f"   ➕ TP{idx+1} @ {tp} | Vol: {volume_per_tp} | PosID: {pos_id}")

        last_trade_price[f"{symbol}_{action.upper()}"] = entry
        logger.info(f"✅ {action.upper()} total {final_volume} {symbol} split into {num_tps} positions")
        return {"status": "success", "positions": results}

    except Exception as e:
        logger.error(f"❌ Trade error: {e}")
        return {"error": str(e)}

# ------------------------------------------------------------------
# ATR CACHE
# ------------------------------------------------------------------
atr_cache = {}
ATR_CACHE_TTL = 60

async def get_symbol_atr(connection, symbol):
    now = time.time()
    if symbol in atr_cache and (now - atr_cache[symbol]['timestamp']) < ATR_CACHE_TTL:
        return atr_cache[symbol]['value']

    try:
        candles = await connection.get_candles(symbol, '1m', 15)
        if len(candles) < 15:
            return 0.01
        tr_values = []
        for i in range(1, len(candles)):
            high = candles[i]['high']
            low = candles[i]['low']
            prev_close = candles[i-1]['close']
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_values.append(tr)
        atr = sum(tr_values) / len(tr_values)
        atr_cache[symbol] = {'value': atr, 'timestamp': now}
        return atr
    except Exception as e:
        logger.warning(f"ATR fetch failed for {symbol}: {e}")
        return 0.01

# ------------------------------------------------------------------
# POSITION MANAGER (unchanged logic)
# ------------------------------------------------------------------
async def position_manager_loop(account_id):
    logger.info(f"🔄 Position manager started for account {account_id}")
    breakeven_done = set()
    trail_updated = {}
    tp1_hit_tracking = set()
    tp_removed = set()

    while True:
        try:
            connection = await get_connection(account_id)
            positions = await connection.get_positions()
            if not positions:
                await asyncio.sleep(5)
                continue

            trade_groups = {}
            for pos in positions:
                symbol = pos['symbol']
                entry = pos['openPrice']
                key = f"{symbol}_{entry}"
                if key not in trade_groups:
                    trade_groups[key] = []
                trade_groups[key].append(pos)

            for pos in positions:
                symbol = pos['symbol']
                position_id = pos['id']
                entry = pos['openPrice']
                current_sl = pos.get('stopLoss', 0)
                current_price = pos['currentPrice']
                pos_type = pos['type']
                take_profit = pos.get('takeProfit', 0)

                be_trigger_mult, trail_start_mult, trail_dist_mult = get_risk_multipliers()

                if pos_type == 'POSITION_TYPE_BUY':
                    profit_pips = (current_price - entry) / PIP_VALUE
                    tp1_price = min([p.get('takeProfit', 0) for p in trade_groups.get(f"{symbol}_{entry}", []) if p.get('takeProfit', 0) > 0] or [999999])
                else:
                    profit_pips = (entry - current_price) / PIP_VALUE
                    tp1_price = max([p.get('takeProfit', 0) for p in trade_groups.get(f"{symbol}_{entry}", []) if p.get('takeProfit', 0) > 0] or [0])

                atr = await get_symbol_atr(connection, symbol)
                atr_pips = atr / PIP_VALUE

                trade_key = f"{symbol}_{entry}"
                if GOLD_REMOVE_TP_AFTER_TP1 and trade_key not in tp1_hit_tracking:
                    tp1_distance = abs(tp1_price - current_price)
                    tp1_threshold = abs(tp1_price - entry) * GOLD_TP1_BE_BUFFER_PCT
                    if tp1_distance <= tp1_threshold:
                        logger.info(f"🪙 TP1 approaching! Removing TPs from remaining positions")
                        for group_pos in trade_groups.get(trade_key, []):
                            if group_pos['id'] not in tp_removed and group_pos.get('takeProfit', 0) != tp1_price:
                                try:
                                    await connection.update_position(group_pos['id'], {'takeProfit': 0})
                                    logger.info(f"🔓 Removed TP from position {group_pos['id']} — trail only now")
                                    tp_removed.add(group_pos['id'])
                                except Exception as e:
                                    logger.warning(f"Could not remove TP: {e}")
                        tp1_hit_tracking.add(trade_key)

                if GOLD_FORCE_BE_AT_TP1 and trade_key not in tp1_hit_tracking:
                    tp1_distance = abs(tp1_price - current_price)
                    tp1_threshold = abs(tp1_price - entry) * GOLD_TP1_BE_BUFFER_PCT
                    if tp1_distance <= tp1_threshold:
                        for group_pos in trade_groups.get(trade_key, []):
                            if group_pos['id'] not in breakeven_done:
                                buffer = 2 * PIP_VALUE
                                if pos_type == 'POSITION_TYPE_BUY':
                                    new_sl = entry + buffer
                                else:
                                    new_sl = entry - buffer
                                if (pos_type == 'POSITION_TYPE_BUY' and new_sl > group_pos.get('stopLoss', 0)) or \
                                   (pos_type == 'POSITION_TYPE_SELL' and new_sl < group_pos.get('stopLoss', 0)):
                                    await connection.update_position(group_pos['id'], {'stopLoss': new_sl})
                                    logger.info(f"🎯 Force BE: Moved SL of {group_pos['id']} to {new_sl}")
                                    breakeven_done.add(group_pos['id'])
                        tp1_hit_tracking.add(trade_key)

                sl_distance_pips = abs(entry - current_sl) / PIP_VALUE if current_sl else float('inf')
                be_trigger_pips = min(atr_pips * be_trigger_mult, sl_distance_pips * 0.7)
                if profit_pips >= be_trigger_pips and position_id not in breakeven_done:
                    buffer = 2 * PIP_VALUE
                    if pos_type == 'POSITION_TYPE_BUY':
                        new_sl = entry + buffer
                    else:
                        new_sl = entry - buffer
                    if (pos_type == 'POSITION_TYPE_BUY' and new_sl > current_sl) or \
                       (pos_type == 'POSITION_TYPE_SELL' and new_sl < current_sl):
                        await connection.update_position(position_id, {'stopLoss': new_sl})
                        logger.info(f"🎯 Breakeven: Moved SL of {position_id} to {new_sl} (profit: {profit_pips:.1f} pips)")
                        breakeven_done.add(position_id)

                trail_start_pips = atr_pips * trail_start_mult
                trail_distance_pips = atr_pips * trail_dist_mult
                if profit_pips >= trail_start_pips:
                    trail_distance_price = trail_distance_pips * PIP_VALUE
                    if pos_type == 'POSITION_TYPE_BUY':
                        new_sl = current_price - trail_distance_price
                        if new_sl > current_sl:
                            if position_id not in trail_updated or abs(new_sl - trail_updated[position_id]) > PIP_VALUE * 0.5:
                                await connection.update_position(position_id, {'stopLoss': new_sl})
                                logger.info(f"📈 Trailing: SL of {position_id} moved to {new_sl}")
                                trail_updated[position_id] = new_sl
                    else:
                        new_sl = current_price + trail_distance_price
                        if new_sl < current_sl:
                            if position_id not in trail_updated or abs(new_sl - trail_updated[position_id]) > PIP_VALUE * 0.5:
                                await connection.update_position(position_id, {'stopLoss': new_sl})
                                logger.info(f"📉 Trailing: SL of {position_id} moved to {new_sl}")
                                trail_updated[position_id] = new_sl

            await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"❌ Position manager error: {e}")
            async with connection_lock:
                if account_id in connections:
                    del connections[account_id]
            await asyncio.sleep(10)

# ------------------------------------------------------------------
# PERSISTENT EVENT LOOP
# ------------------------------------------------------------------
_async_loop = None
_loop_thread = None

def get_async_loop():
    global _async_loop, _loop_thread
    if _async_loop is None:
        _async_loop = asyncio.new_event_loop()
        def run_loop():
            asyncio.set_event_loop(_async_loop)
            _async_loop.run_forever()
        _loop_thread = threading.Thread(target=run_loop, daemon=True)
        _loop_thread.start()
        logger.info("🔄 Persistent async event loop started")
    return _async_loop

def run_async(coro):
    loop = get_async_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=45)

def start_position_manager(account_id):
    loop = get_async_loop()
    asyncio.run_coroutine_threadsafe(keep_connection_alive(account_id), loop)
    asyncio.run_coroutine_threadsafe(position_manager_loop(account_id), loop)
    logger.info(f"🧵 Position manager & connection keeper scheduled for account {account_id}")

# ------------------------------------------------------------------
# ENDPOINTS
# ------------------------------------------------------------------
@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok"}), 200

@app.route('/', methods=['GET'])
def root():
    return jsonify({"service": "Quantum Bot Gold V.09.1", "status": "online"}), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if not data or data.get('passphrase') != WEBHOOK_SECRET:
            logger.warning("🔐 Unauthorized webhook attempt blocked")
            return jsonify({"status": "unauthorized"}), 401

        logger.info(f"📨 Received signal: {data}")

        user = data.get("user_id", "ME")
        target_id = MY_ACC_ID if user.upper() == "ME" else FRIEND_ACC_ID
        if not target_id:
            return jsonify({"status": "error", "message": "No valid account ID"}), 400

        raw_symbol = data.get('symbol', 'XAUUSD')
        clean_symbol = raw_symbol.split(':')[-1]
        final_symbol = clean_symbol + GOLD_SUFFIX

        action = data.get('action', 'buy').lower()
        entry = float(data.get('entry', 0))

        if not can_trade(final_symbol, entry, action.upper()):
            return jsonify({"status": "blocked", "reason": "min_distance"}), 200

        balance = run_async(fetch_account_balance(target_id))
        if balance is not None and not check_rolling_loss_limit(balance):
            return jsonify({"status": "blocked", "reason": "rolling_loss_limit"}), 200

        volume = float(data.get('volume', 0.01))
        use_adaptive = data.get('adaptive', True)
        sl = float(data.get('sl', 0))
        tp1 = float(data.get('tp1', 0))
        tp2 = float(data.get('tp2', 0))
        tp3 = float(data.get('tp3', 0))
        tp4 = float(data.get('tp4', 0)) if 'tp4' in data else 0.0
        include_tp4 = 'tp4' in data
        be_buffer = float(data.get('be_buffer', 0.0))

        logger.info(f"🪙 {final_symbol} | Entry: {entry}, SL: {sl}, TP1: {tp1}, TP2: {tp2}, TP3: {tp3}")
        if include_tp4:
            logger.info(f"🪙 TP4: {tp4}")

        result = run_async(
            place_single_trade(
                target_id, action, final_symbol, volume, entry,
                sl, tp1, tp2, tp3, tp4, include_tp4, be_buffer, use_adaptive
            )
        )

        return jsonify({
            "status": "success",
            "target_account": user,
            "symbol": final_symbol,
            "action": action,
            "result": result
        }), 200

    except Exception as e:
        logger.error(f"❌ Webhook error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Starting Quantum Bot Gold V.09.1 on port {port}")
    get_async_loop()
    if MY_ACC_ID:
        start_position_manager(MY_ACC_ID)
    if FRIEND_ACC_ID and FRIEND_ACC_ID != MY_ACC_ID:
        start_position_manager(FRIEND_ACC_ID)
    app.run(host="0.0.0.0", port=port)
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
SYMBOL_SUFFIX = os.getenv('SYMBOL_SUFFIX', 'm')
GOLD_SUFFIX = os.getenv('GOLD_SUFFIX', 'm')

# Lot size control
LOT_MULTIPLIER = float(os.getenv('LOT_MULTIPLIER', '0.3'))

# Risk parameters
FOREX_BE_ATR = float(os.getenv('FOREX_BE_ATR', '1.0'))
FOREX_TRAIL_START = float(os.getenv('FOREX_TRAIL_START', '1.5'))
FOREX_TRAIL_DIST = float(os.getenv('FOREX_TRAIL_DIST', '0.5'))

GOLD_BE_ATR = float(os.getenv('GOLD_BE_ATR', '0.5'))
GOLD_TRAIL_START = float(os.getenv('GOLD_TRAIL_START', '1.0'))
GOLD_TRAIL_DIST = float(os.getenv('GOLD_TRAIL_DIST', '0.5'))

# Safety filters
MIN_DISTANCE_PIPS_GOLD = float(os.getenv('MIN_DISTANCE_PIPS_GOLD', '40'))
MIN_DISTANCE_PIPS_FOREX = float(os.getenv('MIN_DISTANCE_PIPS_FOREX', '8'))

# Rolling loss limit (scalper-friendly)
MAX_LOSS_LOOKBACK_MINUTES = int(os.getenv('MAX_LOSS_LOOKBACK_MINUTES', '120'))
MAX_LOSS_PCT = float(os.getenv('MAX_LOSS_PCT', '3.0'))
COOLDOWN_MINUTES = int(os.getenv('COOLDOWN_MINUTES', '30'))

# Gold-specific: force BE when near TP1
GOLD_FORCE_BE_AT_TP1 = os.getenv('GOLD_FORCE_BE_AT_TP1', 'true').lower() == 'true'
GOLD_TP1_BE_BUFFER_PCT = float(os.getenv('GOLD_TP1_BE_BUFFER_PCT', '0.2'))

if not TOKEN:
    logger.error("❌ META_API_TOKEN is not set")
if not MY_ACC_ID:
    logger.error("❌ MY_ACCOUNT_ID is not set")

logger.info(f"📊 Lot Multiplier: {LOT_MULTIPLIER}x")
logger.info(f"🛡️ Rolling Loss Limit: {MAX_LOSS_PCT}% over {MAX_LOSS_LOOKBACK_MINUTES}min (Cooldown: {COOLDOWN_MINUTES}min)")
logger.info(f"📏 Min Distance: Gold {MIN_DISTANCE_PIPS_GOLD} pips, Forex {MIN_DISTANCE_PIPS_FOREX} pips")

# ------------------------------------------------------------------
# TRACKING STATE
# ------------------------------------------------------------------
last_trade_price = {}          # symbol_direction -> price
trade_history = deque()        # (timestamp, pnl_amount)
cooldown_until = None          # datetime when cooldown ends

# ------------------------------------------------------------------
# HELPER FUNCTIONS
# ------------------------------------------------------------------
def is_gold(symbol):
    symbol_upper = symbol.upper()
    return 'XAU' in symbol_upper or 'GOLD' in symbol_upper

def get_pip_value(symbol):
    if is_gold(symbol):
        return 0.01
    elif 'JPY' in symbol.upper():
        return 0.01
    else:
        return 0.0001

def get_risk_multipliers(symbol):
    if is_gold(symbol):
        return (GOLD_BE_ATR, GOLD_TRAIL_START, GOLD_TRAIL_DIST)
    else:
        return (FOREX_BE_ATR, FOREX_TRAIL_START, FOREX_TRAIL_DIST)

def can_trade(symbol, current_price, direction):
    key = f"{symbol}_{direction}"
    if key not in last_trade_price:
        return True
    last_price = last_trade_price[key]
    pip_value = get_pip_value(symbol)
    min_distance = MIN_DISTANCE_PIPS_GOLD if is_gold(symbol) else MIN_DISTANCE_PIPS_FOREX
    distance_pips = abs(current_price - last_price) / pip_value
    if distance_pips >= min_distance:
        return True
    else:
        logger.info(f"⛔ Signal blocked: Only {distance_pips:.1f} pips from last {direction} trade. Need {min_distance}")
        return False

def record_trade_result(pnl_amount):
    """Record a trade result for rolling loss calculation."""
    trade_history.append((datetime.utcnow(), pnl_amount))
    cutoff = datetime.utcnow() - timedelta(minutes=MAX_LOSS_LOOKBACK_MINUTES)
    while trade_history and trade_history[0][0] < cutoff:
        trade_history.popleft()

def get_rolling_pnl_pct(current_balance):
    """Return rolling P&L as percentage of current balance."""
    if not trade_history or current_balance <= 0:
        return 0.0
    total_pnl = sum(pnl for _, pnl in trade_history)
    return (total_pnl / current_balance) * 100

def is_in_cooldown():
    """Return True if currently in cooldown period."""
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
    """Return False if loss limit exceeded and should pause trading."""
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
# LAZY METAAPI INITIALIZATION
# ------------------------------------------------------------------
_metaapi_instance = None
_metaapi_lock = threading.Lock()

def get_metaapi():
    global _metaapi_instance
    if _metaapi_instance is None:
        with _metaapi_lock:
            if _metaapi_instance is None:
                _metaapi_instance = MetaApi(TOKEN)
    return _metaapi_instance

# ------------------------------------------------------------------
# CONNECTION CACHE
# ------------------------------------------------------------------
connections = {}
connection_lock = asyncio.Lock()

async def get_connection(account_id):
    async with connection_lock:
        if account_id in connections:
            conn = connections[account_id]
            try:
                await asyncio.wait_for(conn.get_account_information(), timeout=5.0)
                return conn
            except Exception as e:
                logger.warning(f"⚠️ Existing connection dead for {account_id}: {e}")
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

# ------------------------------------------------------------------
# ADAPTIVE LOT SIZING
# ------------------------------------------------------------------
def get_adaptive_lot(account_balance, symbol=""):
    if is_gold(symbol):
        if account_balance < 100:
            base_lot = 0.01
        elif account_balance < 500:
            base_lot = 0.02
        elif account_balance < 1000:
            base_lot = 0.05
        else:
            base_lot = 0.10
    else:
        if account_balance < 50:
            base_lot = 0.01
        elif account_balance < 200:
            base_lot = 0.01
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
                final_volume = get_adaptive_lot(balance, symbol)
                logger.info(f"📊 Balance: ${balance:.2f} → Lot: {final_volume} ({symbol})")

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

        # Update last trade price for distance filter
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
            return 0.01 if is_gold(symbol) else 0.0001
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
        return 0.01 if is_gold(symbol) else 0.0001

# ------------------------------------------------------------------
# POSITION MANAGER
# ------------------------------------------------------------------
async def position_manager_loop(account_id):
    logger.info(f"🔄 Position manager started for account {account_id}")
    breakeven_done = set()
    trail_updated = {}
    tp1_hit_tracking = set()

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

                pip_value = get_pip_value(symbol)
                be_trigger_mult, trail_start_mult, trail_dist_mult = get_risk_multipliers(symbol)

                if pos_type == 'POSITION_TYPE_BUY':
                    profit_pips = (current_price - entry) / pip_value
                    tp1_price = min([p.get('takeProfit', 0) for p in trade_groups.get(f"{symbol}_{entry}", []) if p.get('takeProfit', 0) > 0] or [0])
                else:
                    profit_pips = (entry - current_price) / pip_value
                    tp1_price = max([p.get('takeProfit', 0) for p in trade_groups.get(f"{symbol}_{entry}", []) if p.get('takeProfit', 0) > 0] or [999999])

                atr = await get_symbol_atr(connection, symbol)
                atr_pips = atr / pip_value

                # Gold force BE near TP1
                trade_key = f"{symbol}_{entry}"
                if is_gold(symbol) and GOLD_FORCE_BE_AT_TP1:
                    if trade_key not in tp1_hit_tracking:
                        tp1_distance = abs(tp1_price - current_price)
                        tp1_threshold = abs(tp1_price - entry) * GOLD_TP1_BE_BUFFER_PCT
                        if tp1_distance <= tp1_threshold:
                            logger.info(f"🪙 Gold TP1 approaching! Price: {current_price}, TP1: {tp1_price}")
                            for group_pos in trade_groups.get(trade_key, []):
                                if group_pos['id'] not in breakeven_done:
                                    buffer = 2 * pip_value
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

                # Breakeven with cap at 70% of SL distance
                sl_distance_pips = abs(entry - current_sl) / pip_value if current_sl else float('inf')
                be_trigger_pips = min(
                    atr_pips * be_trigger_mult,
                    sl_distance_pips * 0.7
                )
                if profit_pips >= be_trigger_pips and position_id not in breakeven_done:
                    buffer = 2 * pip_value
                    if pos_type == 'POSITION_TYPE_BUY':
                        new_sl = entry + buffer
                    else:
                        new_sl = entry - buffer
                    if (pos_type == 'POSITION_TYPE_BUY' and new_sl > current_sl) or \
                       (pos_type == 'POSITION_TYPE_SELL' and new_sl < current_sl):
                        await connection.update_position(position_id, {'stopLoss': new_sl})
                        logger.info(f"🎯 Breakeven: Moved SL of {position_id} ({symbol}) to {new_sl} (profit: {profit_pips:.1f} pips)")
                        breakeven_done.add(position_id)

                # Trailing
                trail_start_pips = atr_pips * trail_start_mult
                trail_distance_pips = atr_pips * trail_dist_mult
                if profit_pips >= trail_start_pips:
                    trail_distance_price = trail_distance_pips * pip_value
                    if pos_type == 'POSITION_TYPE_BUY':
                        new_sl = current_price - trail_distance_price
                        if new_sl > current_sl:
                            if position_id not in trail_updated or abs(new_sl - trail_updated[position_id]) > pip_value * 0.5:
                                await connection.update_position(position_id, {'stopLoss': new_sl})
                                logger.info(f"📈 Trailing: SL of {position_id} ({symbol}) moved to {new_sl}")
                                trail_updated[position_id] = new_sl
                    else:
                        new_sl = current_price + trail_distance_price
                        if new_sl < current_sl:
                            if position_id not in trail_updated or abs(new_sl - trail_updated[position_id]) > pip_value * 0.5:
                                await connection.update_position(position_id, {'stopLoss': new_sl})
                                logger.info(f"📉 Trailing: SL of {position_id} ({symbol}) moved to {new_sl}")
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
    return future.result(timeout=30)

def start_position_manager(account_id):
    loop = get_async_loop()
    asyncio.run_coroutine_threadsafe(position_manager_loop(account_id), loop)
    logger.info(f"🧵 Position manager scheduled for account {account_id}")

# ------------------------------------------------------------------
# ENDPOINTS
# ------------------------------------------------------------------
@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok"}), 200

@app.route('/', methods=['GET'])
def root():
    return jsonify({"service": "Quantum Bot V.07", "status": "online"}), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No JSON data"}), 400
        logger.info(f"📨 Received signal: {data}")

        user = data.get("user_id", "ME")
        target_id = MY_ACC_ID if user.upper() == "ME" else FRIEND_ACC_ID
        if not target_id:
            return jsonify({"status": "error", "message": "No valid account ID"}), 400

        raw_symbol = data.get('symbol', 'EURUSD')
        clean_symbol = raw_symbol.split(':')[-1]

        if is_gold(clean_symbol):
            suffix = GOLD_SUFFIX if GOLD_SUFFIX else 'm'
            final_symbol = clean_symbol + suffix
            logger.info(f"🪙 Gold detected: {clean_symbol} → {final_symbol}")
        else:
            final_symbol = clean_symbol + SYMBOL_SUFFIX
            logger.info(f"💱 Forex detected: {clean_symbol} → {final_symbol}")

        action = data.get('action', 'buy').lower()
        entry = float(data.get('entry', 0))

        # Safety checks
        if not can_trade(final_symbol, entry, action.upper()):
            return jsonify({"status": "blocked", "reason": "min_distance"}), 200

        connection = run_async(get_connection(target_id))
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

        logger.info(f"📊 {final_symbol} | Entry: {entry}, SL: {sl}, TP1: {tp1}, TP2: {tp2}, TP3: {tp3}")
        if include_tp4:
            logger.info(f"📊 TP4: {tp4}")

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
    logger.info(f"🚀 Starting Quantum Bot V.07 on port {port}")
    get_async_loop()
    if MY_ACC_ID:
        start_position_manager(MY_ACC_ID)
    if FRIEND_ACC_ID and FRIEND_ACC_ID != MY_ACC_ID:
        start_position_manager(FRIEND_ACC_ID)
    app.run(host="0.0.0.0", port=port)
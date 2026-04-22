import os
import asyncio
import logging
import json
import threading
import time
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
GOLD_SUFFIX = os.getenv('GOLD_SUFFIX', 'm')  # Default to 'm' for Exness

# Risk management settings (can be overridden per symbol)
BREAKEVEN_TRIGGER_ATR_MULT = 1.0
TRAILING_START_ATR_MULT = 1.5
TRAILING_DISTANCE_ATR_MULT = 0.5

# Gold-specific settings (tighter because gold moves faster)
GOLD_BREAKEVEN_TRIGGER_ATR_MULT = 0.75
GOLD_TRAILING_START_ATR_MULT = 1.0
GOLD_TRAILING_DISTANCE_ATR_MULT = 0.5

if not TOKEN:
    logger.error("❌ META_API_TOKEN is not set")
if not MY_ACC_ID:
    logger.error("❌ MY_ACCOUNT_ID is not set")

# ------------------------------------------------------------------
# HELPER: Detect if symbol is Gold
# ------------------------------------------------------------------
def is_gold(symbol):
    """Return True if symbol is Gold/XAUUSD."""
    symbol_upper = symbol.upper()
    return 'XAU' in symbol_upper or 'GOLD' in symbol_upper

# ------------------------------------------------------------------
# HELPER: Get pip value for symbol
# ------------------------------------------------------------------
def get_pip_value(symbol):
    """Return pip value based on symbol type."""
    if is_gold(symbol):
        return 0.01  # Gold: 1 pip = $0.01 movement
    elif 'JPY' in symbol.upper():
        return 0.01  # JPY pairs: 1 pip = 0.01
    else:
        return 0.0001  # Standard forex: 1 pip = 0.0001

# ------------------------------------------------------------------
# HELPER: Get risk multipliers for symbol
# ------------------------------------------------------------------
def get_risk_multipliers(symbol):
    """Return (be_trigger, trail_start, trail_dist) for symbol."""
    if is_gold(symbol):
        return (GOLD_BREAKEVEN_TRIGGER_ATR_MULT, 
                GOLD_TRAILING_START_ATR_MULT, 
                GOLD_TRAILING_DISTANCE_ATR_MULT)
    else:
        return (BREAKEVEN_TRIGGER_ATR_MULT, 
                TRAILING_START_ATR_MULT, 
                TRAILING_DISTANCE_ATR_MULT)

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
# GLOBAL CONNECTION CACHE (with health check)
# ------------------------------------------------------------------
connections = {}
connection_lock = asyncio.Lock()

async def get_connection(account_id):
    async with connection_lock:
        if account_id in connections:
            conn = connections[account_id]
            try:
                # Test if connection is still alive (5 second timeout)
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
    # Gold typically uses smaller lots due to higher value
    if is_gold(symbol):
        if account_balance < 100:
            return 0.01
        elif account_balance < 500:
            return 0.02
        elif account_balance < 1000:
            return 0.05
        else:
            return 0.10
    else:
        # Forex
        if account_balance < 50:
            return 0.01
        elif account_balance < 200:
            return 0.01
        elif account_balance < 1000:
            return 0.05
        else:
            return 0.10

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
# PLACE TRADE WITH MULTIPLE TPs (Split Orders)
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

            tracking_id = f"bot_{symbol}_{int(time.time() * 1000)}_{idx}"
            results.append({"tp": tp, "volume": volume_per_tp, "positionId": pos_id, "trackingId": tracking_id})
            logger.info(f"   ➕ TP{idx+1} @ {tp} | Vol: {volume_per_tp} | PosID: {pos_id}")

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
        logger.info(f"📊 ATR for {symbol}: {atr}")
        return atr
    except Exception as e:
        logger.warning(f"ATR fetch failed for {symbol}: {e}")
        return 0.01 if is_gold(symbol) else 0.0001

# ------------------------------------------------------------------
# POSITION MANAGER (with connection recovery)
# ------------------------------------------------------------------
async def position_manager_loop(account_id):
    logger.info(f"🔄 Position manager started for account {account_id}")
    breakeven_done = set()
    trail_updated = {}

    while True:
        try:
            connection = await get_connection(account_id)
            positions = await connection.get_positions()
            if not positions:
                await asyncio.sleep(5)
                continue

            for pos in positions:
                symbol = pos['symbol']
                position_id = pos['id']
                entry = pos['openPrice']
                current_sl = pos.get('stopLoss', 0)
                current_price = pos['currentPrice']
                pos_type = pos['type']

                # Get symbol-specific settings
                pip_value = get_pip_value(symbol)
                be_trigger_mult, trail_start_mult, trail_dist_mult = get_risk_multipliers(symbol)

                if pos_type == 'POSITION_TYPE_BUY':
                    profit_pips = (current_price - entry) / pip_value
                else:
                    profit_pips = (entry - current_price) / pip_value

                atr = await get_symbol_atr(connection, symbol)
                atr_pips = atr / pip_value

                # Breakeven
                be_trigger_pips = atr_pips * be_trigger_mult
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
            # Clear dead connection from cache
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
# HEALTH CHECK ENDPOINTS
# ------------------------------------------------------------------
@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok"}), 200

@app.route('/', methods=['GET'])
def root():
    return jsonify({"service": "Quantum Bot V.05", "status": "online"}), 200

# ------------------------------------------------------------------
# WEBHOOK ENDPOINT (with better error logging and auto symbol detection)
# ------------------------------------------------------------------
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
        
        # Apply correct suffix based on symbol type with auto-detection
        if is_gold(clean_symbol):
            # Gold: Use GOLD_SUFFIX if set, otherwise default to 'm' for Exness
            suffix = GOLD_SUFFIX if GOLD_SUFFIX else 'm'
            final_symbol = clean_symbol + suffix
            logger.info(f"🪙 Gold detected: {clean_symbol} → {final_symbol}")
        else:
            # Forex: Use SYMBOL_SUFFIX
            final_symbol = clean_symbol + SYMBOL_SUFFIX
            logger.info(f"💱 Forex detected: {clean_symbol} → {final_symbol}")

        action = data.get('action', 'buy').lower()
        volume = float(data.get('volume', 0.01))
        use_adaptive = data.get('adaptive', True)
        entry = float(data.get('entry', 0))
        sl = float(data.get('sl', 0))
        tp1 = float(data.get('tp1', 0))
        tp2 = float(data.get('tp2', 0))
        tp3 = float(data.get('tp3', 0))
        tp4 = float(data.get('tp4', 0)) if 'tp4' in data else 0.0
        include_tp4 = 'tp4' in data
        be_buffer = float(data.get('be_buffer', 0.0))

        logger.info(f"📊 {final_symbol} | Entry: {entry}, SL: {sl}, TP1: {tp1}, TP2: {tp2}, TP3: {tp3}")
        if include_tp4:
            logger.info(f"📊 TP4 (Trend Rider): {tp4}")

        try:
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
        except Exception as trade_error:
            logger.error(f"❌ Trade execution error: {trade_error}", exc_info=True)
            return jsonify({"status": "error", "message": str(trade_error)}), 500

    except Exception as e:
        logger.error(f"❌ Webhook error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# ------------------------------------------------------------------
# MAIN ENTRY POINT
# ------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Starting Quantum Bot V.05 (Gold + Forex Auto-Detect) on port {port}")

    # Start persistent event loop
    get_async_loop()

    # Start position managers
    if MY_ACC_ID:
        start_position_manager(MY_ACC_ID)
    if FRIEND_ACC_ID and FRIEND_ACC_ID != MY_ACC_ID:
        start_position_manager(FRIEND_ACC_ID)

    app.run(host="0.0.0.0", port=port)
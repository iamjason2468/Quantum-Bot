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
SYMBOL_SUFFIX = os.getenv('SYMBOL_SUFFIX', 'm')  # e.g., 'm' for Exness

# Risk management settings (can be moved to env vars)
BREAKEVEN_TRIGGER_ATR_MULT = 1.0   # Move SL to breakeven when profit >= 1x ATR
TRAILING_START_ATR_MULT = 1.5      # Start trailing when profit >= 1.5x ATR
TRAILING_DISTANCE_ATR_MULT = 0.5   # Trail distance = 0.5x ATR

if not TOKEN:
    logger.error("❌ META_API_TOKEN is not set")
if not MY_ACC_ID:
    logger.error("❌ MY_ACCOUNT_ID is not set")

# ------------------------------------------------------------------
# GLOBAL METAAPI CONNECTION CACHE
# ------------------------------------------------------------------
metaapi = MetaApi(TOKEN)
connections = {}
connection_lock = asyncio.Lock()

async def get_connection(account_id):
    """Return a cached, synchronized RPC connection."""
    async with connection_lock:
        if account_id in connections:
            conn = connections[account_id]
            if conn.connected:
                return conn
            else:
                del connections[account_id]

        logger.info(f"🔌 Establishing new connection for account {account_id}")
        account = await metaapi.metatrader_account_api.get_account(account_id)
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
def get_adaptive_lot(account_balance):
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
                final_volume = get_adaptive_lot(balance)
                logger.info(f"📊 Balance: ${balance:.2f} → Lot: {final_volume}")

        tp_levels = [tp1, tp2, tp3]
        if include_tp4:
            tp_levels.append(tp4)
            logger.info(f"🔥 Trend Rider active: TP4 = {tp4}")

        num_tps = len(tp_levels)
        volume_per_tp = final_volume / num_tps
        volume_per_tp = max(0.01, round(volume_per_tp, 2))  # safe rounding

        results = []
        for idx, tp in enumerate(tp_levels):
            # Unique clientId to identify bot trades
            client_id = f"bot_{symbol}_{int(time.time() * 1000)}_{idx}"

            if action.lower() == "buy":
                result = await connection.create_market_buy_order(
                    symbol, volume_per_tp,
                    stop_loss=sl,
                    take_profit=tp,
                    client_id=client_id
                )
            else:
                result = await connection.create_market_sell_order(
                    symbol, volume_per_tp,
                    stop_loss=sl,
                    take_profit=tp,
                    client_id=client_id
                )

            # Safely extract position ID
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

            results.append({"tp": tp, "volume": volume_per_tp, "positionId": pos_id, "clientId": client_id})
            logger.info(f"   ➕ TP{idx+1} @ {tp} | Vol: {volume_per_tp} | PosID: {pos_id} | clientId: {client_id}")

        logger.info(f"✅ {action.upper()} total {final_volume} {symbol} split into {num_tps} positions")
        return {"status": "success", "positions": results}

    except Exception as e:
        logger.error(f"❌ Trade error: {e}")
        return {"error": str(e)}

# ------------------------------------------------------------------
# ATR CACHE (refreshed every 60 seconds)
# ------------------------------------------------------------------
atr_cache = {}          # symbol -> {'value': float, 'timestamp': float}
ATR_CACHE_TTL = 60      # seconds

async def get_symbol_atr(connection, symbol):
    """Return ATR for symbol, using cache if fresh."""
    now = time.time()
    if symbol in atr_cache and (now - atr_cache[symbol]['timestamp']) < ATR_CACHE_TTL:
        return atr_cache[symbol]['value']

    try:
        candles = await connection.get_candles(symbol, '1m', 15)
        if len(candles) < 15:
            return 0.0001
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
        return 0.0001  # fallback

# ------------------------------------------------------------------
# POSITION MANAGER (Breakeven, Trailing Stop)
# ------------------------------------------------------------------
async def position_manager_loop(account_id):
    """Runs indefinitely to manage open positions."""
    logger.info(f"🔄 Position manager started for account {account_id}")

    # State tracking to avoid repeated modifications
    breakeven_done = set()       # position IDs that already moved to BE
    trail_updated = {}           # position_id -> last_trail_price

    while True:
        try:
            connection = await get_connection(account_id)
            positions = await connection.get_positions()
            if not positions:
                await asyncio.sleep(5)
                continue

            for pos in positions:
                # Only manage positions created by this bot
                client_id = pos.get('clientId', '')
                if not client_id.startswith('bot_'):
                    continue

                symbol = pos['symbol']
                position_id = pos['id']
                entry = pos['openPrice']
                current_sl = pos.get('stopLoss', 0)
                current_price = pos['currentPrice']
                pos_type = pos['type']

                # Determine pip value (forex)
                pip_value = 0.0001 if 'JPY' not in symbol else 0.01

                # Calculate profit in pips
                if pos_type == 'POSITION_TYPE_BUY':
                    profit_pips = (current_price - entry) / pip_value
                else:
                    profit_pips = (entry - current_price) / pip_value

                # Get ATR
                atr = await get_symbol_atr(connection, symbol)
                atr_pips = atr / pip_value

                # --- BREAKEVEN LOGIC (with buffer and one-time execution) ---
                be_trigger_pips = atr_pips * BREAKEVEN_TRIGGER_ATR_MULT
                if profit_pips >= be_trigger_pips and position_id not in breakeven_done:
                    buffer = 2 * pip_value   # small buffer to avoid spread whipsaw
                    if pos_type == 'POSITION_TYPE_BUY':
                        new_sl = entry + buffer
                    else:
                        new_sl = entry - buffer

                    # Only move if it improves the SL
                    if (pos_type == 'POSITION_TYPE_BUY' and new_sl > current_sl) or \
                       (pos_type == 'POSITION_TYPE_SELL' and new_sl < current_sl):
                        await connection.update_position(position_id, {'stopLoss': new_sl})
                        logger.info(f"🎯 Breakeven: Moved SL of {position_id} to {new_sl} (profit: {profit_pips:.1f} pips)")
                        breakeven_done.add(position_id)

                # --- TRAILING STOP LOGIC ---
                trail_start_pips = atr_pips * TRAILING_START_ATR_MULT
                trail_distance_pips = atr_pips * TRAILING_DISTANCE_ATR_MULT
                if profit_pips >= trail_start_pips:
                    trail_distance_price = trail_distance_pips * pip_value
                    if pos_type == 'POSITION_TYPE_BUY':
                        new_sl = current_price - trail_distance_price
                        # Only update if new SL is higher than current SL
                        if new_sl > current_sl:
                            # Avoid updating if change is less than 0.5 pips
                            if position_id not in trail_updated or abs(new_sl - trail_updated[position_id]) > pip_value * 0.5:
                                await connection.update_position(position_id, {'stopLoss': new_sl})
                                logger.info(f"📈 Trailing: SL of {position_id} moved to {new_sl}")
                                trail_updated[position_id] = new_sl
                    else:
                        new_sl = current_price + trail_distance_price
                        if new_sl < current_sl:
                            if position_id not in trail_updated or abs(new_sl - trail_updated[position_id]) > pip_value * 0.5:
                                await connection.update_position(position_id, {'stopLoss': new_sl})
                                logger.info(f"📉 Trailing: SL of {position_id} moved to {new_sl}")
                                trail_updated[position_id] = new_sl

            await asyncio.sleep(5)  # Check every 5 seconds

        except Exception as e:
            logger.error(f"❌ Position manager error: {e}")
            await asyncio.sleep(10)

# ------------------------------------------------------------------
# BACKGROUND THREAD FOR POSITION MANAGER
# ------------------------------------------------------------------
def start_position_manager(account_id):
    """Launch the position manager in a separate thread."""
    def run_manager():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(position_manager_loop(account_id))
    thread = threading.Thread(target=run_manager, daemon=True)
    thread.start()
    logger.info(f"🧵 Position manager thread started for account {account_id}")

# ------------------------------------------------------------------
# ASYNC WRAPPER FOR FLASK
# ------------------------------------------------------------------
def run_async(coro):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()

# ------------------------------------------------------------------
# HEALTH CHECK ENDPOINTS
# ------------------------------------------------------------------
@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok"}), 200

@app.route('/', methods=['GET'])
def root():
    return jsonify({"service": "Quantum Bot V.04", "status": "online"}), 200

# ------------------------------------------------------------------
# WEBHOOK ENDPOINT
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
        final_symbol = clean_symbol + SYMBOL_SUFFIX

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

        logger.info(f"📊 Entry: {entry}, SL: {sl}, TP1: {tp1}, TP2: {tp2}, TP3: {tp3}")
        if include_tp4:
            logger.info(f"📊 TP4 (Trend Rider): {tp4}")

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
        logger.error(f"❌ Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ------------------------------------------------------------------
# MAIN ENTRY POINT
# ------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Starting Quantum Bot V.04 on port {port}")

    # Start position manager(s) in background threads
    if MY_ACC_ID:
        start_position_manager(MY_ACC_ID)
    if FRIEND_ACC_ID and FRIEND_ACC_ID != MY_ACC_ID:
        start_position_manager(FRIEND_ACC_ID)

    app.run(host="0.0.0.0", port=port)
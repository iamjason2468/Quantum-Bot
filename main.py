import os
import asyncio
import logging
import json
import threading
import time
from datetime import datetime, timedelta
from collections import deque
from flask import Flask, request, jsonify, render_template_string
from metaapi_cloud_sdk import MetaApi

# ------------------------------------------------------------------
# FLASK APP INITIALIZATION
# ------------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------
def clamp(v, lo, hi):
    return max(lo, min(v, hi))

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
MAX_SPREAD_PIPS = float(os.getenv('MAX_SPREAD_PIPS', '5.0'))

if not TOKEN:
    logger.error("❌ META_API_TOKEN is not set")
if not MY_ACC_ID:
    logger.error("❌ MY_ACCOUNT_ID is not set")

logger.info(f"📊 Lot Multiplier: {LOT_MULTIPLIER}x")
logger.info(f"🛡️ Daily Loss Limit: {MAX_LOSS_PCT}% (Cooldown: {COOLDOWN_MINUTES}min)")
logger.info(f"📏 Min Distance: Gold {MIN_DISTANCE_PIPS_GOLD} pips")
logger.info(f"🪙 Gold BE: {GOLD_BE_ATR} ATR | Trail Start: {GOLD_TRAIL_START} | Trail Dist: {GOLD_TRAIL_DIST}")
logger.info(f"🪙 Max Spread: {MAX_SPREAD_PIPS} pips")

# ------------------------------------------------------------------
# PERSISTENT STATE
# ------------------------------------------------------------------
STATE_FILE = "bot_state.json"
state_lock = threading.Lock()

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
                return data.get("cooldown_until", None), data.get("daily_start_balance", {})
        except:
            pass
    return None, {}

def save_state(cooldown_until, daily_start_balance):
    with open(STATE_FILE, 'w') as f:
        json.dump({
            "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
            "daily_start_balance": daily_start_balance
        }, f)

# ------------------------------------------------------------------
# TRACKING STATE
# ------------------------------------------------------------------
last_trade_price = {}
daily_start_balance = {}
cooldown_until = None

# Load saved state
loaded_cooldown, loaded_daily_bal = load_state()
if loaded_cooldown:
    try:
        cooldown_until = datetime.fromisoformat(loaded_cooldown)
    except:
        pass
daily_start_balance = loaded_daily_bal

# Recent signals deque (for dashboard)
recent_signals = deque(maxlen=10)

# Position manager state exposed globally (for dashboard)
breakeven_done_global = set()
trail_updated_global = {}
post_tp1_active_global = set()
tp1_hit_tracking_global = set()
tp_removed_global = set()

# ------------------------------------------------------------------
# PIP & RISK HELPERS
# ------------------------------------------------------------------
PIP_VALUE = 0.1  # 1 pip = $0.10 for XAUUSD

def get_pip_value(symbol=""):
    return PIP_VALUE

def get_risk_multipliers(symbol=""):
    return (GOLD_BE_ATR, GOLD_TRAIL_START, GOLD_TRAIL_DIST)

def can_trade(symbol, current_price, direction):
    with state_lock:
        key = f"{symbol}_{direction}"
        if key not in last_trade_price:
            return True
        last_price = last_trade_price[key]
        min_distance = MIN_DISTANCE_PIPS_GOLD
        distance_pips = abs(current_price - last_price) / PIP_VALUE
        if distance_pips >= min_distance:
            return True
        else:
            logger.info(f"⛔ Signal blocked: Only {distance_pips:.1f} pips from last {direction} trade. Need {min_distance}")
            return False

# ------------------------------------------------------------------
# DAILY LOSS LIMIT
# ------------------------------------------------------------------
def check_daily_loss_limit(current_balance):
    global cooldown_until, daily_start_balance
    with state_lock:
        today_str = datetime.utcnow().strftime('%Y-%m-%d')
        if cooldown_until and datetime.utcnow() < cooldown_until:
            remaining = (cooldown_until - datetime.utcnow()).seconds // 60
            logger.info(f"⏸️ In cooldown: {remaining} minutes remaining")
            return False
        if cooldown_until and datetime.utcnow() >= cooldown_until:
            cooldown_until = None
            logger.info("✅ Cooldown ended — resuming trading")

        if today_str not in daily_start_balance:
            daily_start_balance = {today_str: current_balance}
            logger.info(f"📅 New trading day – start balance: ${current_balance:.2f}")
            save_state(cooldown_until, daily_start_balance)
        start_bal = daily_start_balance[today_str]
        drawdown_pct = ((current_balance - start_bal) / start_bal) * 100 if start_bal > 0 else 0

        if drawdown_pct <= -MAX_LOSS_PCT:
            logger.warning(f"🛑 Daily loss limit reached: {drawdown_pct:.2f}% (Limit: {MAX_LOSS_PCT}%)")
            cooldown_until = datetime.utcnow() + timedelta(minutes=COOLDOWN_MINUTES)
            save_state(cooldown_until, daily_start_balance)
            logger.warning(f"⏸️ Entering cooldown for {COOLDOWN_MINUTES} minutes")
            return False
        return True

# ------------------------------------------------------------------
# SPREAD MONITOR
# ------------------------------------------------------------------
async def get_current_spread(symbol):
    """Return spread in pips for the given symbol. Returns large number on error."""
    try:
        conn = await get_connection(MY_ACC_ID)
        # Use get_symbol_price to fetch current bid/ask
        price = await conn.get_symbol_price(symbol)
        if price and 'bid' in price and 'ask' in price:
            bid = price['bid']
            ask = price['ask']
            spread_price = ask - bid
            spread_pips = spread_price / PIP_VALUE
            return spread_pips
        else:
            logger.warning(f"Could not fetch spread for {symbol}")
            return 999.0  # large safe value (block)
    except Exception as e:
        logger.warning(f"Spread check failed: {e}")
        return 999.0  # block on error to be safe

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

async def keep_connection_alive(account_id):
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
        if include_tp4 and tp4 > 0:
            tp_levels.append(tp4)
            logger.info(f"🔥 Trend Rider active: TP4 = {tp4}")

        num_tps = len(tp_levels)
        volume_per_tp = final_volume / num_tps
        volume_per_tp = max(0.01, round(volume_per_tp, 2))

        if num_tps > 1:
            sum_first = volume_per_tp * (num_tps - 1)
            last_volume = round(final_volume - sum_first, 2)
            if last_volume < 0.01:
                last_volume = 0.01
            volumes = [volume_per_tp] * (num_tps - 1) + [last_volume]
        else:
            volumes = [volume_per_tp]

        total_placed = sum(volumes)
        if abs(total_placed - final_volume) > 0.02:
            logger.warning(f"⚠️ Volume mismatch: intended {final_volume}, placed {total_placed}")

        results = []
        for idx, tp in enumerate(tp_levels):
            if action.lower() == "buy":
                result = await connection.create_market_buy_order(
                    symbol, volumes[idx],
                    stop_loss=sl,
                    take_profit=tp
                )
            else:
                result = await connection.create_market_sell_order(
                    symbol, volumes[idx],
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

            results.append({"tp": tp, "volume": volumes[idx], "positionId": pos_id})
            logger.info(f"   ➕ TP{idx+1} @ {tp} | Vol: {volumes[idx]} | PosID: {pos_id}")

        with state_lock:
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
# TREND QUALITY (Efficiency Ratio only)
# ------------------------------------------------------------------
def calc_efficiency_ratio(high, low, close, length=20):
    if len(high) < length + 1 or len(low) < length + 1:
        return 0.5
    change = abs(close[-1] - close[-length-1])
    volatility = sum(abs(close[i] - close[i-1]) for i in range(-length, 0))
    if volatility == 0:
        return 0.0
    return change / volatility

async def get_trend_quality(connection, symbol):
    try:
        candles = await connection.get_candles(symbol, '1m', 21)
        if candles and len(candles) >= 21:
            highs = [c['high'] for c in candles]
            lows = [c['low'] for c in candles]
            closes = [c['close'] for c in candles]
            er = calc_efficiency_ratio(highs, lows, closes, 20)
        else:
            er = 0.5
    except Exception as e:
        logger.warning(f"Could not compute ER: {e}")
        er = 0.5
    return clamp(er, 0.1, 1.0)

# ------------------------------------------------------------------
# POSITION MANAGER
# ------------------------------------------------------------------
async def position_manager_loop(account_id):
    global breakeven_done_global, trail_updated_global, post_tp1_active_global, tp1_hit_tracking_global, tp_removed_global

    logger.info(f"🔄 Position manager started for account {account_id}")
    breakeven_done = set()
    trail_updated = {}
    tp1_hit_tracking = set()
    tp_removed = set()
    post_tp1_active = set()
    quality_cache = {}

    while True:
        try:
            connection = await get_connection(account_id)
            positions = await connection.get_positions()
            if not positions:
                breakeven_done.clear()
                trail_updated.clear()
                tp1_hit_tracking.clear()
                tp_removed.clear()
                post_tp1_active.clear()
                # Update globals
                with state_lock:
                    breakeven_done_global = breakeven_done.copy()
                    trail_updated_global = trail_updated.copy()
                    post_tp1_active_global = post_tp1_active.copy()
                    tp1_hit_tracking_global = tp1_hit_tracking.copy()
                    tp_removed_global = tp_removed.copy()
                await asyncio.sleep(5)
                continue

            trade_groups = {}
            active_trade_keys = set()
            for pos in positions:
                symbol = pos['symbol']
                exact_entry = pos['openPrice']
                group_entry = round(exact_entry, 0) if 'XAU' in symbol.upper() else exact_entry
                key = f"{symbol}_{group_entry}"
                active_trade_keys.add(key)
                if key not in trade_groups:
                    trade_groups[key] = []
                trade_groups[key].append(pos)

            # Cleanup
            for key in list(tp1_hit_tracking):
                if key not in active_trade_keys:
                    tp1_hit_tracking.discard(key)
            for key in list(post_tp1_active):
                if key not in active_trade_keys:
                    post_tp1_active.discard(key)

            for symbol in set(pos['symbol'] for pos in positions):
                if symbol not in quality_cache or (time.time() - quality_cache[symbol].get('ts', 0) > 60):
                    try:
                        quality = await get_trend_quality(connection, symbol)
                        quality_cache[symbol] = {'value': quality, 'ts': time.time()}
                    except:
                        quality = 1.0
                else:
                    quality = quality_cache[symbol]['value']

            for pos in positions:
                symbol = pos['symbol']
                position_id = pos['id']
                entry = pos['openPrice']
                current_sl = pos.get('stopLoss', 0)
                current_price = pos['currentPrice']
                pos_type = pos['type']
                take_profit = pos.get('takeProfit', 0)

                be_base, trail_start_base, trail_dist_base = get_risk_multipliers()
                scale = clamp(quality, 0.3, 1.0)
                be_trigger_mult = be_base * scale
                trail_start_mult = trail_start_base * scale
                trail_dist_mult = trail_dist_base * scale

                group_entry = round(entry, 0)
                trade_key = f"{symbol}_{group_entry}"

                if trade_key in post_tp1_active:
                    trail_start_mult = 0.0

                if pos_type == 'POSITION_TYPE_BUY':
                    profit_pips = (current_price - entry) / PIP_VALUE
                    tp1_price = min([p.get('takeProfit', 0) for p in trade_groups.get(trade_key, []) if p.get('takeProfit', 0) > 0] or [999999])
                else:
                    profit_pips = (entry - current_price) / PIP_VALUE
                    tp1_price = max([p.get('takeProfit', 0) for p in trade_groups.get(trade_key, []) if p.get('takeProfit', 0) > 0] or [0])

                atr = await get_symbol_atr(connection, symbol)
                atr_pips = atr / PIP_VALUE

                if GOLD_FORCE_BE_AT_TP1 and GOLD_REMOVE_TP_AFTER_TP1 and trade_key not in tp1_hit_tracking:
                    tp1_distance = abs(tp1_price - current_price)
                    tp1_threshold = abs(tp1_price - entry) * GOLD_TP1_BE_BUFFER_PCT
                    if tp1_distance <= tp1_threshold:
                        logger.info(f"🪙 TP1 approaching! Removing TPs, forcing BE, and activating immediate trailing")
                        for group_pos in trade_groups.get(trade_key, []):
                            if group_pos['id'] not in tp_removed and group_pos.get('takeProfit', 0) != tp1_price:
                                try:
                                    await connection.update_position(group_pos['id'], {'takeProfit': 0})
                                    logger.info(f"🔓 Removed TP from position {group_pos['id']} — trail only now")
                                    tp_removed.add(group_pos['id'])
                                except Exception as e:
                                    logger.warning(f"Could not remove TP: {e}")
                            if group_pos['id'] not in breakeven_done:
                                buffer = 3 * PIP_VALUE
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
                        post_tp1_active.add(trade_key)

                if position_id not in breakeven_done:
                    sl_distance_pips = abs(entry - current_sl) / PIP_VALUE if current_sl else float('inf')
                    be_trigger_pips = min(
                        atr_pips * be_trigger_mult,
                        sl_distance_pips * 0.7
                    )
                    if profit_pips >= be_trigger_pips:
                        buffer = 3 * PIP_VALUE
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

            # Update globals
            with state_lock:
                breakeven_done_global = breakeven_done.copy()
                trail_updated_global = trail_updated.copy()
                post_tp1_active_global = post_tp1_active.copy()
                tp1_hit_tracking_global = tp1_hit_tracking.copy()
                tp_removed_global = tp_removed.copy()

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
    return jsonify({"service": "Quantum Bot Gold V.10.1", "status": "online"}), 200

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

        # Store signal info early for logging
        signal_time = datetime.utcnow().strftime('%H:%M:%S')
        signal_record = {
            "time": signal_time,
            "symbol": final_symbol,
            "action": action.upper(),
            "entry": entry,
            "status": "PENDING",
            "reason": ""
        }

        # Min distance check
        if not can_trade(final_symbol, entry, action.upper()):
            signal_record["status"] = "BLOCKED_MIN_DIST"
            signal_record["reason"] = "min_distance"
            with state_lock:
                recent_signals.append(signal_record)
            return jsonify({"status": "blocked", "reason": "min_distance"}), 200

        # Spread check
        spread_pips = run_async(get_current_spread(final_symbol))
        if spread_pips > MAX_SPREAD_PIPS:
            logger.info(f"🚫 Spread too wide: {spread_pips:.1f} pips (max {MAX_SPREAD_PIPS})")
            signal_record["status"] = "BLOCKED_SPREAD"
            signal_record["reason"] = f"spread_{spread_pips:.1f}pips"
            with state_lock:
                recent_signals.append(signal_record)
            return jsonify({"status": "blocked", "reason": "spread_too_wide", "spread_pips": spread_pips}), 200

        # Balance check
        balance = run_async(fetch_account_balance(target_id))
        if balance is None:
            logger.error("❌ Cannot fetch account balance – aborting trade")
            signal_record["status"] = "ERROR"
            signal_record["reason"] = "balance_fetch_failed"
            with state_lock:
                recent_signals.append(signal_record)
            return jsonify({"status": "error", "message": "Cannot fetch account balance"}), 500
        if not check_daily_loss_limit(balance):
            signal_record["status"] = "BLOCKED_LOSS_LIMIT"
            signal_record["reason"] = "daily_loss_limit"
            with state_lock:
                recent_signals.append(signal_record)
            return jsonify({"status": "blocked", "reason": "daily_loss_limit"}), 200

        volume = float(data.get('volume', 0.01))
        use_adaptive = data.get('adaptive', True)
        sl = float(data.get('sl', 0))
        tp1 = float(data.get('tp1', 0))
        tp2 = float(data.get('tp2', 0))
        tp3 = float(data.get('tp3', 0))
        tp4 = float(data.get('tp4', 0)) if 'tp4' in data else 0.0
        include_tp4 = 'tp4' in data and tp4 > 0
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

        if "error" in result:
            signal_record["status"] = "ERROR"
            signal_record["reason"] = result.get("error", "unknown")
        else:
            signal_record["status"] = "EXECUTED"

        with state_lock:
            recent_signals.append(signal_record)

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
# DASHBOARD API ENDPOINTS
# ------------------------------------------------------------------

@app.route('/api/status', methods=['GET'])
def api_status():
    with state_lock:
        in_cooldown = cooldown_until is not None and datetime.utcnow() < cooldown_until
    return jsonify({
        "online": True,
        "cooldown": in_cooldown,
        "time_utc": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    })

@app.route('/api/account', methods=['GET'])
def api_account():
    balance = run_async(fetch_account_balance(MY_ACC_ID))
    with state_lock:
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

@app.route('/api/positions', methods=['GET'])
def api_positions():
    try:
        positions = run_async(get_positions_for_api(MY_ACC_ID))
        return jsonify(positions)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

async def get_positions_for_api(account_id):
    try:
        conn = await get_connection(account_id)
        positions = await conn.get_positions()
        result = []
        for pos in positions:
            result.append({
                "positionId": pos['id'],
                "type": "BUY" if pos['type'] == 'POSITION_TYPE_BUY' else "SELL",
                "symbol": pos['symbol'],
                "volume": pos['volume'],
                "open_price": pos['openPrice'],
                "current_price": pos.get('currentPrice', 0),
                "sl": pos.get('stopLoss', 0),
                "tp": pos.get('takeProfit', 0),
                "profit_dollar": pos.get('profit', 0)
            })
        return result
    except Exception as e:
        return []

@app.route('/api/spread', methods=['GET'])
def api_spread():
    symbol = "XAUUSD" + GOLD_SUFFIX
    spread_pips = run_async(get_current_spread(symbol))
    return jsonify({
        "symbol": symbol,
        "spread_pips": round(spread_pips, 2),
        "max_spread_pips": MAX_SPREAD_PIPS,
        "is_safe": spread_pips <= MAX_SPREAD_PIPS
    })

@app.route('/api/signals', methods=['GET'])
def api_signals():
    with state_lock:
        return jsonify(list(recent_signals))

@app.route('/api/manager', methods=['GET'])
def api_manager():
    with state_lock:
        groups = {}
        # Collect all keys
        all_keys = set()
        all_keys.update(post_tp1_active_global)
        all_keys.update(tp1_hit_tracking_global)
        # We don't have a direct way to list all groups, so we infer from the sets
        for key in all_keys:
            groups[key] = {
                "force_be_done": key in tp1_hit_tracking_global,
                "tp_removed": key in tp_removed_global,
                "trailing_active": key in post_tp1_active_global
            }
    return jsonify(groups)

# ------------------------------------------------------------------
# DASHBOARD HTML (wrapped in {% raw %} to protect JavaScript)
# ------------------------------------------------------------------

DASHBOARD_HTML = """{% raw %}<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Quantum Gold | Master Terminal</title>
    <style>
        :root {
            --bg: #050505; --card-bg: #0f0f12; --border: #1f1f23;
            --text-main: #ffffff; --text-dim: #88888e;
            --accent-gold: #FFD700;
            --accent-green: #00ffa3; --accent-red: #ff4d4d;
            --accent-blue: #00d1ff; --accent-orange: #ff9f00;
            --terminal-green: #00ff41;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background-color: var(--bg); color: var(--text-main); font-family: 'Inter', sans-serif; padding: 15px; overflow-x: hidden; }
        header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .logo { font-weight: 800; letter-spacing: -1px; font-size: 1.1rem; }
        .logo span { color: var(--accent-gold); }
        .status-container {
            display: flex; align-items: center; gap: 10px;
            background: rgba(255,215,0,0.05); padding: 6px 12px;
            border-radius: 20px; border: 1px solid var(--border);
        }
        .pulse-dot {
            width: 8px; height: 8px; border-radius: 50%;
            background: var(--accent-gold);
            box-shadow: 0 0 10px var(--accent-gold);
            animation: breath 2s infinite ease-in-out;
        }
        .pulse-stale { background: var(--accent-red) !important; box-shadow: 0 0 10px var(--accent-red) !important; }
        @keyframes breath {
            0%, 100% { opacity: 0.5; transform: scale(0.9); }
            50% { opacity: 1; transform: scale(1.1); }
        }
        .nav-tabs { display: flex; gap: 8px; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 10px; overflow-x: auto; }
        .tab { padding: 10px 15px; border-radius: 8px; cursor: pointer; color: var(--text-dim); font-size: 0.75rem; font-weight: 700; transition: 0.2s; text-transform: uppercase; white-space: nowrap; }
        .tab.active { background: var(--card-bg); color: var(--accent-gold); border: 1px solid var(--border); }
        .page { display: none; animation: fadeIn 0.3s ease; }
        .page.active { display: block; }
        .dashboard-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 15px; }
        .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 16px; padding: 20px; }
        .wide-card { grid-column: span 2; }
        .card-label { color: var(--text-dim); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
        .spread-badge { font-size: 0.65rem; padding: 2px 6px; border-radius: 4px; background: rgba(255,255,255,0.05); color: var(--text-dim); border: 1px solid var(--border); }
        .spread-high { color: var(--accent-red) !important; border-color: var(--accent-red) !important; background: rgba(255, 77, 77, 0.1) !important; }
        .curve-container { margin: 10px 0; background: rgba(0,0,0,0.4); border-radius: 12px; padding: 15px; }
        .sparkline { stroke: var(--accent-gold); stroke-width: 2.5; fill: transparent; filter: drop-shadow(0 0 8px rgba(255,215,0,0.4)); }
        .stat-row { display: flex; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid #16161a; }
        .stat-name { color: var(--text-dim); font-size: 0.85rem; }
        .stat-value-sm { font-weight: 700; font-size: 0.9rem; }
        .red-glow { background: var(--accent-red); box-shadow: 0 0 6px var(--accent-red); }
        .green-glow { background: var(--accent-green); box-shadow: 0 0 6px var(--accent-green); }
        .gray-dot { background: #333; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.85rem; }
        th { text-align: left; color: var(--text-dim); padding-bottom: 10px; border-bottom: 1px solid var(--border); }
        td { padding: 12px 0; border-bottom: 1px solid #16161a; }
        .price-tag { font-family: monospace; font-weight: bold; }
        .console { background: #000; border: 1px solid #222; border-radius: 12px; height: 380px; overflow-y: auto; padding: 15px; font-family: monospace; font-size: 0.75rem; color: var(--terminal-green); line-height: 1.5; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
        @media (max-width: 768px) { .wide-card { grid-column: span 1; } }
    </style>
</head>
<body>

    <header>
        <div class="logo">QUANTUM<span>GOLD</span></div>
        <div class="status-container">
            <div id="statusPulse" class="pulse-dot"></div>
            <div style="display:flex; flex-direction:column;">
                <span id="statusText" style="font-size: 10px; font-weight: 900; color:var(--accent-gold)">ENGINE LIVE</span>
                <span style="font-size: 9px; color:var(--text-dim)" id="clock">00:00:00 UTC</span>
            </div>
        </div>
    </header>

    <div class="nav-tabs">
        <div class="tab active" onclick="switchTab('main-page')">Monitor</div>
        <div class="tab" onclick="switchTab('analytics-page')">Analytics</div>
        <div class="tab" onclick="switchTab('logs-page')">Logs</div>
    </div>

    <div id="main-page" class="page active">
        <div class="dashboard-grid">
            <div class="card">
                <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                    <div class="card-label">Equity Account</div>
                    <div id="spreadDisplay" class="spread-badge">Spread: --</div>
                </div>
                <div style="font-size: 2rem; font-weight: 800;" id="balance">$---.--</div>
                <div id="dailyPnL" style="color:var(--accent-green); font-size: 0.85rem; font-weight: 700;">Loading...</div>
            </div>

            <div class="card">
                <div class="card-label" style="text-align: center;">System Status</div>
                <div id="manager-info" style="font-size:0.7rem; color:var(--text-dim); margin-top:10px;">Loading...</div>
            </div>

            <div class="card wide-card">
                <div class="card-label">Active Positions</div>
                <table id="positions-table">
                    <thead><tr><th>Ticket</th><th>Entry / Now</th><th>SL / TP</th><th>PnL</th></tr></thead>
                    <tbody><tr><td colspan="4" style="color:var(--text-dim)">Loading...</td></tr></tbody>
                </table>
            </div>
        </div>
    </div>

    <div id="analytics-page" class="page">
        <div class="dashboard-grid">
            <div class="card wide-card">
                <div class="card-label">Performance Summary</div>
                <div id="analytics-content" style="color:var(--text-dim)">Loading...</div>
            </div>
            <div class="card wide-card">
                <div class="card-label">Manager Groups</div>
                <div id="manager-groups" style="font-size:0.7rem; color:var(--text-dim)">Loading...</div>
            </div>
        </div>
    </div>

    <div id="logs-page" class="page">
        <div class="card">
            <div class="card-label">Signal Log</div>
            <div class="console" id="logConsole">
                <div style="color:#555">Loading signals...</div>
            </div>
        </div>
    </div>

    <script>
        const API_BASE = window.location.origin;

        function switchTab(id) {
            document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.getElementById(id).classList.add('active');
            event.currentTarget.classList.add('active');
        }

        function updateTime() {
            var now = new Date();
            document.getElementById('clock').innerText = now.toISOString().split('T')[1].split('.')[0] + ' UTC';
        }

        async function fetchStatus() {
            try {
                const res = await fetch(API_BASE + '/api/status');
                const data = await res.json();
                const dot = document.getElementById('statusPulse');
                const txt = document.getElementById('statusText');
                if (data.online) {
                    dot.classList.remove('pulse-stale');
                    txt.innerText = 'ENGINE LIVE';
                    txt.style.color = 'var(--accent-gold)';
                } else {
                    dot.classList.add('pulse-stale');
                    txt.innerText = 'OFFLINE';
                    txt.style.color = 'var(--accent-red)';
                }
                updateTime();
            } catch(e) { console.error(e); }
        }

        async function fetchAccount() {
            try {
                const res = await fetch(API_BASE + '/api/account');
                const data = await res.json();
                document.getElementById('balance').innerText = '$' + data.balance.toFixed(2);
                const pnlEl = document.getElementById('dailyPnL');
                const pnlText = (data.daily_pnl_dollar >= 0 ? '+' : '') + '$' + data.daily_pnl_dollar.toFixed(2) + ' Today (' + data.daily_pnl_percent.toFixed(2) + '%)';
                pnlEl.innerText = pnlText;
                pnlEl.style.color = data.daily_pnl_dollar >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
            } catch(e) { console.error(e); }
        }

        async function fetchPositions() {
            try {
                const res = await fetch(API_BASE + '/api/positions');
                const positions = await res.json();
                const tbody = document.querySelector('#positions-table tbody');
                tbody.innerHTML = '';
                if (positions.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="4" style="color:var(--text-dim)">No open positions</td></tr>';
                    return;
                }
                positions.forEach(pos => {
                    const row = tbody.insertRow();
                    const typeColor = pos.type === 'BUY' ? 'var(--accent-green)' : 'var(--accent-red)';
                    const profitColor = pos.profit_dollar >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
                    row.innerHTML = `<td>#${pos.positionId}<br><b style="color:${typeColor}">${pos.type} ${pos.volume}</b></td>
                        <td><span class="price-tag">${pos.open_price}</span><br><small style="color:var(--text-dim)">${pos.current_price}</small></td>
                        <td><span style="color:var(--accent-red)">${pos.sl}</span> / <span style="color:var(--accent-green)">${pos.tp === 0 ? 'TRAIL' : pos.tp}</span></td>
                        <td style="color:${profitColor}; font-weight:bold;">${pos.profit_dollar >= 0 ? '+' : ''}$${pos.profit_dollar.toFixed(2)}</td>`;
                });
            } catch(e) { console.error(e); }
        }

        async function fetchSpread() {
            try {
                const res = await fetch(API_BASE + '/api/spread');
                const data = await res.json();
                const display = document.getElementById('spreadDisplay');
                display.innerText = `Spread: ${data.spread_pips.toFixed(1)}`;
                if (!data.is_safe) {
                    display.classList.add('spread-high');
                } else {
                    display.classList.remove('spread-high');
                }
            } catch(e) { console.error(e); }
        }

        async function fetchSignals() {
            try {
                const res = await fetch(API_BASE + '/api/signals');
                const signals = await res.json();
                const consoleEl = document.getElementById('logConsole');
                consoleEl.innerHTML = '';
                if (signals.length === 0) {
                    consoleEl.innerHTML = '<div style="color:#555">No signals yet</div>';
                    return;
                }
                signals.reverse().forEach(sig => {
                    const div = document.createElement('div');
                    div.className = 'log-entry';
                    let color = '#888';
                    if (sig.status === 'EXECUTED') color = 'var(--accent-green)';
                    else if (sig.status.indexOf('BLOCKED') === 0) color = 'var(--accent-red)';
                    else if (sig.status === 'ERROR') color = 'var(--accent-orange)';
                    div.innerHTML = `<span style="color:#555">${sig.time}</span> [${sig.status}] ${sig.action} ${sig.symbol} @ ${sig.entry} ${sig.reason ? '('+sig.reason+')' : ''}`;
                    consoleEl.appendChild(div);
                });
            } catch(e) { console.error(e); }
        }

        async function fetchManager() {
            try {
                const res = await fetch(API_BASE + '/api/manager');
                const data = await res.json();
                const managerInfo = document.getElementById('manager-info');
                const managerGroups = document.getElementById('manager-groups');
                const keys = Object.keys(data);
                if (keys.length === 0) {
                    managerInfo.innerHTML = '<div style="color:var(--text-dim)">No active trade groups</div>';
                    if (managerGroups) managerGroups.innerHTML = '<div style="color:var(--text-dim)">No active trade groups</div>';
                    return;
                }
                let html = '';
                keys.forEach(key => {
                    const val = data[key];
                    html += `<div style="margin:2px 0">${key}: Force BE:${val.force_be_done} | TP Removed:${val.tp_removed} | Trail:${val.trailing_active}</div>`;
                });
                managerInfo.innerHTML = html;
                if (managerGroups) managerGroups.innerHTML = html;
            } catch(e) { console.error(e); }
        }

        function refreshAll() {
            fetchStatus();
            fetchAccount();
            fetchPositions();
            fetchSpread();
            fetchSignals();
            fetchManager();
        }

        updateTime();
        setInterval(updateTime, 1000);
        refreshAll();
        setInterval(refreshAll, 10000);
    </script>
</body>
</html>{% endraw %}"""

@app.route('/dashboard', methods=['GET'])
def dashboard():
    return render_template_string(DASHBOARD_HTML)

# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Starting Quantum Bot Gold V.10.1 on port {port}")
    get_async_loop()
    if MY_ACC_ID:
        start_position_manager(MY_ACC_ID)
    if FRIEND_ACC_ID and FRIEND_ACC_ID != MY_ACC_ID:
        start_position_manager(FRIEND_ACC_ID)
    app.run(host="0.0.0.0", port=port)
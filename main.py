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
from dashboard import dashboard_bp, init_dashboard

# ------------------------------------------------------------------
# FLASK APP INITIALIZATION
# ------------------------------------------------------------------
app = Flask(__name__)
app.register_blueprint(dashboard_bp)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------
def clamp(v, lo, hi):
    return max(lo, min(v, hi))

# ------------------------------------------------------------------
# ENVIRONMENT VARIABLES (Hybrid Strategy defaults)
# ------------------------------------------------------------------
TOKEN = os.getenv('META_API_TOKEN')
MY_ACC_ID = os.getenv('MY_ACCOUNT_ID')
FRIEND_ACC_ID = os.getenv('FRIEND_ACCOUNT_ID')
GOLD_SUFFIX = os.getenv('GOLD_SUFFIX', 'm')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', 'quantum-gold-2026')
MAX_SPREAD_PIPS = float(os.getenv('MAX_SPREAD_PIPS', '5.0'))

LOT_MULTIPLIER = float(os.getenv('LOT_MULTIPLIER', '0.15'))
# --- OPTIMIZED FOR GOLD HYBRID STRATEGY ---
GOLD_BE_ATR = float(os.getenv('GOLD_BE_ATR', '0.8'))          # Move to BE after a solid move
GOLD_TRAIL_START = float(os.getenv('GOLD_TRAIL_START', '1.0'))
GOLD_TRAIL_DIST = float(os.getenv('GOLD_TRAIL_DIST', '1.2'))  # Room for the "Lion" to wiggle
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
recent_signals = deque(maxlen=10)

# Current market intelligence (updated via separate webhook)
current_market_data = {
    "bull_pct": 0,
    "bear_pct": 0,
    "adx": 0,
    "bias": "NEUTRAL",
    "smart_filter": "OFF",
    "trend": "WEAK",
    "ema": "neutral",
    "macd": "neutral",
    "vwap": "neutral",
    "vol_stat": "low",
    "hard_adx": "off",
    "status": "WAITING"
}

# Equity history for sparkline (stored every 5 seconds)
equity_history = deque(maxlen=288)

# Closed trade P&L for performance metrics
closed_trade_pnls = deque(maxlen=100)

# Track balance before a trade group opens to later compute P&L
trade_entry_balance = None
trade_group_open = False

breakeven_done_global = set()
trail_updated_global = {}
post_tp1_active_global = set()
tp1_hit_tracking_global = set()
tp_removed_global = set()

loaded_cooldown, loaded_daily_bal = load_state()
if loaded_cooldown:
    try:
        cooldown_until = datetime.fromisoformat(loaded_cooldown)
    except:
        pass
daily_start_balance = loaded_daily_bal

# ------------------------------------------------------------------
# PIP & RISK HELPERS
# ------------------------------------------------------------------
PIP_VALUE = 0.1

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
# METAAPI INITIALIZATION
# ------------------------------------------------------------------
_metaapi_instance = None
_metaapi_lock = threading.Lock()

def get_metaapi():
    global _metaapi_instance
    if _metaapi_instance is None:
        with _metaapi_lock:
            if _metaapi_instance is None:
                _metaapi_instance = MetaApi(TOKEN)
                logger.info("🔧 MetaApi initialized (auto region detection)")
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
# SPREAD & POSITIONS HELPERS
# ------------------------------------------------------------------
async def get_current_spread(symbol):
    try:
        conn = await get_connection(MY_ACC_ID)
        price = await conn.get_symbol_price(symbol)
        if price and 'bid' in price and 'ask' in price:
            bid = price['bid']
            ask = price['ask']
            spread_price = ask - bid
            spread_pips = spread_price / PIP_VALUE
            return spread_pips
        else:
            return 999.0
    except Exception as e:
        logger.warning(f"Spread check failed: {e}")
        return 999.0

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
# PLACE TRADE (Hybrid Strategy – TP3/TP4 naked)
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

        # TP3 and TP4 are sent as 0.0 by the Pine Script (hybrid strategy)
        # Only TP1 and TP2 are hard TPs.
        tp_levels = [tp1, tp2]
        if include_tp4 and tp4 > 0:
            tp_levels.append(tp4)   # Trend Rider is also naked, but add it for volume split
        else:
            tp_levels.append(0.0)   # placeholder for TP4 to keep volume split even

        num_tps = len(tp_levels)   # will be 3 (TP1, TP2, TP4 placeholder) or 4 if trend rider active
        volume_per_tp = final_volume / num_tps
        volume_per_tp = max(0.01, round(volume_per_tp, 2))

        # Adjust last volume to match total
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
        # TP1 position (hard TP)
        result = await connection.create_market_buy_order(symbol, volumes[0], stop_loss=sl, take_profit=tp1) if action.lower() == "buy" else \
                 await connection.create_market_sell_order(symbol, volumes[0], stop_loss=sl, take_profit=tp1)
        results.append(extract_position_id(result, tp1, volumes[0]))

        # TP2 position (hard TP)
        result = await connection.create_market_buy_order(symbol, volumes[1], stop_loss=sl, take_profit=tp2) if action.lower() == "buy" else \
                 await connection.create_market_sell_order(symbol, volumes[1], stop_loss=sl, take_profit=tp2)
        results.append(extract_position_id(result, tp2, volumes[1]))

        # TP3 runner (naked – TP = 0)
        result = await connection.create_market_buy_order(symbol, volumes[2], stop_loss=sl, take_profit=0.0) if action.lower() == "buy" else \
                 await connection.create_market_sell_order(symbol, volumes[2], stop_loss=sl, take_profit=0.0)
        results.append(extract_position_id(result, "Runner", volumes[2]))

        # TP4 Trend Rider (naked, only if active)
        if include_tp4 and tp4 > 0:
            result = await connection.create_market_buy_order(symbol, volumes[3], stop_loss=sl, take_profit=0.0) if action.lower() == "buy" else \
                     await connection.create_market_sell_order(symbol, volumes[3], stop_loss=sl, take_profit=0.0)
            results.append(extract_position_id(result, "TrendRider", volumes[3]))
            logger.info(f"🔥 Trend Rider active – Naked Runner placed.")

        with state_lock:
            last_trade_price[f"{symbol}_{action.upper()}"] = entry
        logger.info(f"✅ {action.upper()} total {final_volume} {symbol} – TP1/TP2 hard, Runners naked.")
        return {"status": "success", "positions": results}

    except Exception as e:
        logger.error(f"❌ Trade error: {e}")
        return {"error": str(e)}

def extract_position_id(result, tp_label, vol):
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
    logger.info(f"   ➕ {tp_label} @ {tp_label if isinstance(tp_label, str) else f'TP {tp_label}'} | Vol: {vol} | PosID: {pos_id}")
    return {"tp": tp_label, "volume": vol, "positionId": pos_id}

# ------------------------------------------------------------------
# BACKGROUND TRADE EXECUTION (prevents webhook timeout)
# ------------------------------------------------------------------
def process_trade_async(target_id, action, final_symbol, volume, entry, sl, tp1, tp2, tp3, tp4, include_tp4, be_buffer, use_adaptive):
    try:
        result = run_async(
            place_single_trade(
                target_id, action, final_symbol, volume, entry,
                sl, tp1, tp2, tp3, tp4, include_tp4, be_buffer, use_adaptive
            )
        )
        logger.info(f"Trade result: {result}")
    except Exception as e:
        logger.error(f"Async trade failed: {e}")

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
# TREND QUALITY
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
# IMPROVED POSITION MANAGER (HYBRID SAFETY VALVE)
# ------------------------------------------------------------------
async def position_manager_loop(account_id):
    global breakeven_done_global, trail_updated_global, post_tp1_active_global, tp1_hit_tracking_global, tp_removed_global
    global trade_entry_balance, trade_group_open

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
                # --- trade group just closed ---
                if trade_group_open and trade_entry_balance is not None:
                    current_balance = await fetch_account_balance(account_id)
                    if current_balance is not None:
                        pnl = current_balance - trade_entry_balance
                        closed_trade_pnls.append(pnl)
                        logger.info(f"📊 Trade group closed – P&L: ${pnl:.2f}")
                    trade_group_open = False

                breakeven_done.clear()
                trail_updated.clear()
                tp1_hit_tracking.clear()
                tp_removed.clear()
                post_tp1_active.clear()
                with state_lock:
                    breakeven_done_global = breakeven_done.copy()
                    trail_updated_global = trail_updated.copy()
                    post_tp1_active_global = post_tp1_active.copy()
                    tp1_hit_tracking_global = tp1_hit_tracking.copy()
                    tp_removed_global = tp_removed.copy()
                await asyncio.sleep(5)
                continue

            # --- trade group just opened ---
            if not trade_group_open:
                trade_entry_balance = await fetch_account_balance(account_id)
                trade_group_open = True

            trade_groups = {}
            active_trade_keys = set()
            for pos in positions:
                symbol = pos['symbol']
                exact_entry = pos['openPrice']
                group_entry = round(exact_entry, 2) if 'XAU' in symbol.upper() else exact_entry
                key = f"{symbol}_{group_entry}"
                active_trade_keys.add(key)
                if key not in trade_groups:
                    trade_groups[key] = []
                trade_groups[key].append(pos)

            # Clean stale keys
            for key in list(tp1_hit_tracking):
                if key not in active_trade_keys:
                    tp1_hit_tracking.discard(key)
            for key in list(post_tp1_active):
                if key not in active_trade_keys:
                    post_tp1_active.discard(key)

            # Update trend quality cache
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

                group_entry = round(entry, 2) if 'XAU' in symbol.upper() else entry
                trade_key = f"{symbol}_{group_entry}"

                if trade_key in post_tp1_active:
                    trail_start_mult = 0.0

                # Determine direction and TP values for the group
                is_buy = pos_type == 'POSITION_TYPE_BUY'
                position_tps = [p.get('takeProfit', 0) for p in trade_groups.get(trade_key, []) if p.get('takeProfit', 0) > 0]

                # Sort TPs appropriately
                if is_buy:
                    position_tps.sort()  # ascending: TP1, TP2, ...
                    tp1_price = position_tps[0] if len(position_tps) > 0 else None
                    tp2_price = position_tps[1] if len(position_tps) > 1 else None
                else:
                    position_tps.sort(reverse=True)  # descending: TP1, TP2, ...
                    tp1_price = position_tps[0] if len(position_tps) > 0 else None
                    tp2_price = position_tps[1] if len(position_tps) > 1 else None

                atr = await get_symbol_atr(connection, symbol)
                atr_pips = atr / PIP_VALUE

                # --------------------------------------------------------------
                # DYNAMIC BE BUFFER: survive Gold's spread
                # --------------------------------------------------------------
                be_buffer_price = max(1.5, atr * 0.3)

                # --------------------------------------------------------------
                # SAFETY VALVE: TP1/TP2 zone – strip TP only from runners
                # --------------------------------------------------------------
                if GOLD_REMOVE_TP_AFTER_TP1 and trade_key not in tp1_hit_tracking:
                    # Check proximity to TP1 or TP2
                    zone_triggered = False
                    target_tp = None
                    if tp1_price is not None:
                        tp1_dist = abs(tp1_price - current_price)
                        tp1_thresh = abs(tp1_price - entry) * GOLD_TP1_BE_BUFFER_PCT
                        if tp1_dist <= tp1_thresh:
                            zone_triggered = True
                            target_tp = tp1_price
                    if not zone_triggered and tp2_price is not None:
                        tp2_dist = abs(tp2_price - current_price)
                        tp2_thresh = abs(tp2_price - entry) * GOLD_TP1_BE_BUFFER_PCT
                        if tp2_dist <= tp2_thresh:
                            zone_triggered = True
                            target_tp = tp2_price

                    if zone_triggered and target_tp is not None:
                        logger.info(f"🎯 TP zone reached near {target_tp}. Activating hybrid safety valve.")
                        # Identify which TP values are "hard" (tp1_price, tp2_price)
                        hard_tps = set()
                        if tp1_price is not None: hard_tps.add(tp1_price)
                        if tp2_price is not None: hard_tps.add(tp2_price)

                        for group_pos in trade_groups.get(trade_key, []):
                            pos_tp = group_pos.get('takeProfit', 0)
                            # Only remove TP from positions that are NOT hard TP1/TP2
                            if pos_tp not in hard_tps and pos_tp != 0:
                                try:
                                    await connection.update_position(group_pos['id'], {'takeProfit': 0})
                                    logger.info(f"🔓 Stripped TP from runner {group_pos['id']} (was {pos_tp})")
                                    tp_removed.add(group_pos['id'])
                                except Exception as e:
                                    logger.warning(f"Could not strip TP: {e}")

                            # Force BE on all positions in the group
                            if group_pos['id'] not in breakeven_done:
                                new_be_sl = entry + be_buffer_price if is_buy else entry - be_buffer_price
                                if (is_buy and new_be_sl > group_pos.get('stopLoss', 0)) or \
                                   (not is_buy and new_be_sl < group_pos.get('stopLoss', 0)):
                                    await connection.update_position(group_pos['id'], {'stopLoss': new_be_sl})
                                    logger.info(f"🎯 Force BE: Moved SL of {group_pos['id']} to {new_be_sl}")
                                    breakeven_done.add(group_pos['id'])

                        tp1_hit_tracking.add(trade_key)
                        post_tp1_active.add(trade_key)

                # --- TP1 ZONE FORCE BREAKEVEN (legacy hybrid trigger, kept for safety) ---
                elif GOLD_FORCE_BE_AT_TP1 and trade_key not in tp1_hit_tracking and tp1_price is not None:
                    tp1_distance = abs(tp1_price - current_price)
                    tp1_threshold = abs(tp1_price - entry) * GOLD_TP1_BE_BUFFER_PCT
                    if tp1_distance <= tp1_threshold:
                        logger.info(f"🎯 TP1 Zone Reached. Switching to Aggressive Trend Rider Mode.")
                        for group_pos in trade_groups.get(trade_key, []):
                            # Remove TP from runners only (keep TP1)
                            if group_pos.get('takeProfit', 0) != tp1_price:
                                try:
                                    await connection.update_position(group_pos['id'], {'takeProfit': 0})
                                    logger.info(f"🔓 Removed TP from {group_pos['id']} — trail only now")
                                    tp_removed.add(group_pos['id'])
                                except Exception as e:
                                    logger.warning(f"Could not remove TP: {e}")

                            if group_pos['id'] not in breakeven_done:
                                new_be_sl = entry + be_buffer_price if is_buy else entry - be_buffer_price
                                if (is_buy and new_be_sl > group_pos.get('stopLoss', 0)) or \
                                   (not is_buy and new_be_sl < group_pos.get('stopLoss', 0)):
                                    await connection.update_position(group_pos['id'], {'stopLoss': new_be_sl})
                                    logger.info(f"🎯 Force BE: Moved SL of {group_pos['id']} to {new_be_sl}")
                                    breakeven_done.add(group_pos['id'])

                        tp1_hit_tracking.add(trade_key)
                        post_tp1_active.add(trade_key)

                # --- STANDARD BREAKEVEN (if not done yet) ---
                if position_id not in breakeven_done:
                    sl_distance_pips = abs(entry - current_sl) / PIP_VALUE if current_sl else float('inf')
                    be_trigger_pips = min(
                        atr_pips * be_trigger_mult,
                        sl_distance_pips * 0.7
                    )
                    if is_buy:
                        profit_pips = (current_price - entry) / PIP_VALUE
                    else:
                        profit_pips = (entry - current_price) / PIP_VALUE

                    if profit_pips >= be_trigger_pips:
                        new_sl = entry + be_buffer_price if is_buy else entry - be_buffer_price
                        if (is_buy and new_sl > current_sl) or (not is_buy and new_sl < current_sl):
                            await connection.update_position(position_id, {'stopLoss': new_sl})
                            logger.info(f"🎯 Breakeven: Moved SL of {position_id} to {new_sl} (profit: {profit_pips:.1f} pips)")
                            breakeven_done.add(position_id)

                # --- TRAILING STOP (with instant trail and overextension filter) ---
                trail_start_pips = atr_pips * trail_start_mult
                trail_distance_pips = atr_pips * trail_dist_mult

                if trade_key in post_tp1_active:
                    trail_start_pips = 0.0

                if quality > 0.8:
                    trail_distance_pips *= 0.8

                if profit_pips >= trail_start_pips:
                    trail_distance_price = trail_distance_pips * PIP_VALUE
                    if is_buy:
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

            with state_lock:
                breakeven_done_global = breakeven_done.copy()
                trail_updated_global = trail_updated.copy()
                post_tp1_active_global = post_tp1_active.copy()
                tp1_hit_tracking_global = tp1_hit_tracking.copy()
                tp_removed_global = tp_removed.copy()

            balance = await fetch_account_balance(account_id)
            if balance is not None:
                equity_history.append({"time": datetime.utcnow().isoformat(), "balance": balance})

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

# ======================================================================
# INITIALIZE DASHBOARD
# ======================================================================
init_dashboard(
    state_lock, run_async, fetch_account_balance, get_current_spread,
    get_positions_for_api, MY_ACC_ID, GOLD_SUFFIX, MAX_SPREAD_PIPS,
    daily_start_balance, cooldown_until, recent_signals,
    post_tp1_active_global, tp1_hit_tracking_global, tp_removed_global,
    get_connection, current_market_data, equity_history,
    closed_trade_pnls
)

# ------------------------------------------------------------------
# ENDPOINTS
# ------------------------------------------------------------------
@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok"}), 200

@app.route('/', methods=['GET'])
def root():
    return jsonify({"service": "Quantum Bot Gold V.11", "status": "online"}), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    global current_market_data
    
    logger.info("=" * 60)
    logger.info("🔥 WEBHOOK RECEIVED - RAW DUMP")
    logger.info(f"Method: {request.method}")
    logger.info(f"Path: {request.path}")
    logger.info(f"Content-Type: {request.content_type}")
    logger.info(f"Content-Length: {request.content_length}")
    logger.info(f"Headers: {dict(request.headers)}")
    
    raw_body = request.get_data(as_text=True)
    logger.info(f"Raw Body (first 1000 chars): {raw_body[:1000]}")
    logger.info("=" * 60)
    
    try:
        try:
            data = json.loads(raw_body)
            logger.info(f"✅ JSON parsed successfully: {json.dumps(data, indent=2)[:500]}")
        except json.JSONDecodeError as e:
            logger.error(f"❌ JSON parse failed: {e} | Raw body: {raw_body[:200]}")
            return jsonify({"status": "error", "message": "Invalid JSON"}), 400
        except Exception as e:
            logger.error(f"❌ Unexpected parse error: {e}")
            return jsonify({"status": "error", "message": str(e)}), 400
        
        if not data:
            logger.warning("📨 Received empty webhook payload")
            return jsonify({"status": "error", "message": "Empty payload"}), 400

        # Check if this is a market intelligence update (dashboard heartbeat)
        if data.get("type") == "market_intel":
            if data.get('passphrase') != WEBHOOK_SECRET:
                logger.warning("🔐 Unauthorized market intelligence update blocked")
                return jsonify({"status": "unauthorized"}), 401
            
            current_market_data.update({
                "bull_pct": float(data.get("bull_pct", 0)),
                "bear_pct": float(data.get("bear_pct", 0)),
                "adx": float(data.get("adx", 0)),
                "bias": data.get("bias", "NEUTRAL"),
                "smart_filter": data.get("smart_filter", "OFF"),
                "trend": data.get("trend", "WEAK"),
                "ema": data.get("ema", "neutral"),
                "macd": data.get("macd", "neutral"),
                "vwap": data.get("vwap", "neutral"),
                "vol_stat": data.get("vol_stat", "low"),
                "hard_adx": data.get("hard_adx", "off"),
                "status": "ACTIVE"
            })
            
            logger.info(f"📊 Dashboard updated: Bias={current_market_data['bias']}, "
                       f"EMA={current_market_data['ema']}, MACD={current_market_data['macd']}, "
                       f"VWAP={current_market_data['vwap']}")
            return jsonify({"status": "dashboard_updated"}), 200
        
        # If not market_intel, process as trade signal
        if data.get('passphrase') != WEBHOOK_SECRET:
            logger.warning("🔐 Unauthorized webhook attempt blocked")
            return jsonify({"status": "unauthorized"}), 401

        logger.info(f"📨 Processing TRADE SIGNAL: {json.dumps(data)}")

        user = data.get("user_id", "ME")
        target_id = MY_ACC_ID if user.upper() == "ME" else FRIEND_ACC_ID
        if not target_id:
            return jsonify({"status": "error", "message": "No valid account ID"}), 400

        raw_symbol = data.get('symbol', 'XAUUSD')
        clean_symbol = raw_symbol.split(':')[-1]
        final_symbol = clean_symbol + GOLD_SUFFIX

        action = data.get('action', 'buy').lower()
        entry = float(data.get('entry', 0))

        signal_time = datetime.utcnow().strftime('%H:%M:%S')
        signal_record = {
            "time": signal_time,
            "symbol": final_symbol,
            "action": action.upper(),
            "entry": entry,
            "status": "PENDING",
            "reason": "",
            "bull_pct": data.get("bull_pct", 0),
            "bear_pct": data.get("bear_pct", 0),
            "adx": data.get("adx", 0),
            "bias_text": data.get("bias_text", "NEUTRAL"),
            "smart_filter": data.get("smart_filter", "OFF"),
            "trend_strength": data.get("trend_strength", "WEAK")
        }

        if not can_trade(final_symbol, entry, action.upper()):
            signal_record["status"] = "BLOCKED_MIN_DIST"
            signal_record["reason"] = "min_distance"
            with state_lock:
                recent_signals.append(signal_record)
            return jsonify({"status": "blocked", "reason": "min_distance"}), 200

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

        logger.info(f"🪙 TRADE: {final_symbol} | {action.upper()} | Entry: {entry} | SL: {sl} | TP1: {tp1} | TP2: {tp2} | Runners: naked")
        if include_tp4:
            logger.info(f"🪙 Trend Rider runner included")

        # Fire and forget trade execution in a background thread to avoid blocking the webhook
        threading.Thread(
            target=process_trade_async,
            args=(target_id, action, final_symbol, volume, entry, sl, tp1, tp2, tp3, tp4, include_tp4, be_buffer, use_adaptive),
            daemon=True
        ).start()

        signal_record["status"] = "PROCESSING"
        with state_lock:
            recent_signals.append(signal_record)

        return jsonify({
            "status": "processing",
            "target_account": user,
            "symbol": final_symbol,
            "action": action
        }), 200

    except Exception as e:
        logger.error(f"❌ Webhook error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# ------------------------------------------------------------------
# /market_webhook – Legacy endpoint (kept for backward compatibility)
# ------------------------------------------------------------------
@app.route('/market_webhook', methods=['POST'])
def market_webhook():
    global current_market_data
    try:
        raw_data = request.get_data(as_text=True)
        logger.info(f"📥 Received market webhook raw: {raw_data[:200]}")

        data = json.loads(raw_data)

        if not data:
            return jsonify({"status": "error", "message": "No JSON"}), 400

        current_market_data.update({
            "bull_pct": float(data.get("bull_pct", 0)),
            "bear_pct": float(data.get("bear_pct", 0)),
            "adx": float(data.get("adx", 0)),
            "bias": data.get("bias", "NEUTRAL"),
            "smart_filter": data.get("smart_filter", "OFF"),
            "trend": data.get("trend", "WEAK"),
            "ema": data.get("ema", "neutral"),
            "macd": data.get("macd", "neutral"),
            "vwap": data.get("vwap", "neutral"),
            "vol_stat": data.get("vol_stat", "low"),
            "hard_adx": data.get("hard_adx", "off"),
            "status": data.get("status", "WAITING")
        })
        logger.info(f"📊 Market data updated via legacy endpoint: {current_market_data}")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Market webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ------------------------------------------------------------------
# DASHBOARD API ROUTES
# ------------------------------------------------------------------
@app.route('/api/market', methods=['GET'])
def get_market_api():
    return jsonify(current_market_data)

@app.route('/api/indicators', methods=['GET'])
def get_indicators_api():
    return jsonify({
        "ema": current_market_data.get("ema", "neutral"),
        "macd": current_market_data.get("macd", "neutral"),
        "vwap": current_market_data.get("vwap", "neutral"),
        "vol_stat": current_market_data.get("vol_stat", "low"),
        "trend": current_market_data.get("trend", "neutral"),
        "adx_power": "green" if current_market_data.get("adx", 0) > 25 else "red",
        "trend_power": "green" if current_market_data.get("trend") == "STRONG" else "red",
        "hard_adx": current_market_data.get("hard_adx", "off")
    })

@app.route('/api/status', methods=['GET'])
def get_status_api():
    return jsonify({
        "online": True,
        "time_utc": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    })

@app.route('/api/equity_history', methods=['GET'])
def get_equity_history():
    return jsonify(list(equity_history))

# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Starting Quantum Bot Gold V.11 on port {port}")
    get_async_loop()

    if MY_ACC_ID:
        start_position_manager(MY_ACC_ID)
    if FRIEND_ACC_ID and FRIEND_ACC_ID != MY_ACC_ID:
        start_position_manager(FRIEND_ACC_ID)
    app.run(host="0.0.0.0", port=port)
import os
import asyncio
import logging
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

if not TOKEN:
    logger.error("❌ META_API_TOKEN is not set")
if not MY_ACC_ID:
    logger.error("❌ MY_ACCOUNT_ID is not set")

# ------------------------------------------------------------------
# ADAPTIVE LOT SIZING
# ------------------------------------------------------------------
def get_adaptive_lot(account_balance):
    if account_balance < 50:
        return 0.001
    elif account_balance < 200:
        return 0.01
    elif account_balance < 1000:
        return 0.05
    else:
        return 0.10

async def fetch_account_balance(account_id):
    api = MetaApi(TOKEN)
    try:
        account = await api.metatrader_account_api.get_account(account_id)
        if account.state != "DEPLOYED":
            await account.deploy()
            await account.wait_connected()
        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized()
        account_info = await connection.get_account_information()
        return account_info.get('balance', 0.0)
    except Exception as e:
        logger.error(f"❌ Could not fetch balance: {e}")
        return None

# ------------------------------------------------------------------
# CLOSE OPPOSITE POSITIONS (FIXED)
# ------------------------------------------------------------------
async def close_opposite_positions(account_id, symbol, new_direction):
    api = MetaApi(TOKEN)
    closed_count = 0
    try:
        account = await api.metatrader_account_api.get_account(account_id)
        if account.state != "DEPLOYED":
            await account.deploy()
            await account.wait_connected()
        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized()
        positions = await connection.get_positions()
        for pos in positions:
            if pos['symbol'] == symbol:
                # FIX: pos['type'] is a STRING, not a list
                pos_type = pos['type']
                if pos_type == 'POSITION_TYPE_BUY':
                    pos_direction = "BUY"
                else:
                    pos_direction = "SELL"
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
# PLACE SINGLE TRADE WITH MULTIPLE TPs
# ------------------------------------------------------------------
async def place_single_trade(account_id, action, symbol, volume, entry, sl, tp1, tp2, tp3, tp4, include_tp4, be_buffer, use_adaptive=True):
    api = MetaApi(TOKEN)
    try:
        account = await api.metatrader_account_api.get_account(account_id)
        if account.state != "DEPLOYED":
            logger.info(f"⏳ Deploying account...")
            await account.deploy()
            await account.wait_connected()
        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized()

        # Close opposite positions
        await close_opposite_positions(account_id, symbol, action.upper())

        final_volume = volume
        if use_adaptive:
            balance = await fetch_account_balance(account_id)
            if balance is not None:
                final_volume = get_adaptive_lot(balance)
                logger.info(f"📊 Balance: ${balance:.2f} → Lot: {final_volume}")

        tp_list = [tp1, tp2, tp3]
        if include_tp4:
            tp_list.append(tp4)
            logger.info(f"🔥 Trend Rider active: TP4 = {tp4}")

        # Place order
        if action.lower() == "buy":
            result = await connection.create_market_buy_order(
                symbol, final_volume,
                stop_loss=sl,
                take_profit=tp_list
            )
        else:
            result = await connection.create_market_sell_order(
                symbol, final_volume,
                stop_loss=sl,
                take_profit=tp_list
            )

        position_id = result.get('positionId')
        logger.info(f"✅ {action.upper()} {final_volume} {symbol} with {len(tp_list)} TPs. Position ID: {position_id}")

        return result

    except Exception as e:
        logger.error(f"❌ Trade error: {e}")
        return {"error": str(e)}

# ------------------------------------------------------------------
# HEALTH CHECK ENDPOINTS
# ------------------------------------------------------------------
@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok", "message": "Railway server is running"}), 200

@app.route('/', methods=['GET'])
def root():
    return jsonify({
        "service": "Quantum Bot V.04",
        "status": "online",
        "endpoints": {"/ping": "GET", "/webhook": "POST"}
    }), 200

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
        final_symbol = clean_symbol + "m"

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

        result = asyncio.run(
            place_single_trade(target_id, action, final_symbol, volume, entry, sl, tp1, tp2, tp3, tp4, include_tp4, be_buffer, use_adaptive)
        )
        return jsonify({
            "status": "success",
            "target_account": user,
            "symbol": final_symbol,
            "action": action,
            "result": str(result)
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
    logger.info(f"📡 MetaApi Token: {'✅' if TOKEN else '❌'}")
    logger.info(f"👤 My Account ID: {'✅' if MY_ACC_ID else '❌'}")
    app.run(host="0.0.0.0", port=port)
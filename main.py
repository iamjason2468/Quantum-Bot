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
# MONITORING TASK FOR BREAK EVEN
# ------------------------------------------------------------------
async def monitor_position(account_id, trade2_id, trade3_id, entry_price, sl_price, tp1_price, symbol, direction):
    api = MetaApi(TOKEN)
    connection = None
    try:
        account = await api.metatrader_account_api.get_account(account_id)
        if account.state != "DEPLOYED":
            await account.deploy()
            await account.wait_connected()
        connection = account.get_rpc_connection()
        await connection.connect()
        logger.info(f"👁️ Started monitoring for BE on trades {trade2_id}, {trade3_id}")
        be_activated = False
        while True:
            await asyncio.sleep(2)
            try:
                price = await connection.get_symbol_price(symbol)
                current_price = price['bid'] if direction == "BUY" else price['ask']
                if not be_activated:
                    if (direction == "BUY" and current_price >= tp1_price) or \
                       (direction == "SELL" and current_price <= tp1_price):
                        logger.info(f"🎯 TP1 reached! Moving SL to Break Even (Entry: {entry_price})")
                        try:
                            await connection.modify_position(trade2_id, stop_loss=entry_price)
                            logger.info(f"✅ Trade 2 SL moved to BE: {entry_price}")
                        except Exception as e:
                            logger.warning(f"⚠️ Could not modify Trade 2 SL: {e}")
                        try:
                            await connection.modify_position(trade3_id, stop_loss=entry_price)
                            logger.info(f"✅ Trade 3 SL moved to BE: {entry_price}")
                        except Exception as e:
                            logger.warning(f"⚠️ Could not modify Trade 3 SL: {e}")
                        be_activated = True
                        break
            except Exception as e:
                logger.warning(f"⚠️ Monitoring error: {e}")
                continue
    except Exception as e:
        logger.error(f"❌ Monitor task failed: {e}")
    finally:
        if connection:
            await connection.close()
        logger.info(f"👁️ Monitoring ended for trades {trade2_id}, {trade3_id}")

# ------------------------------------------------------------------
# PLACE 3 SEPARATE TRADES (SCALING OUT)
# ------------------------------------------------------------------
async def place_scaled_trades(account_id, action, symbol, volume, entry, sl, tp1, tp2, tp3, use_adaptive=True):
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

        final_volume = volume
        if use_adaptive:
            balance = await fetch_account_balance(account_id)
            if balance is not None:
                final_volume = get_adaptive_lot(balance)
                logger.info(f"📊 Balance: ${balance:.2f} → Total Lot: {final_volume}")
        trade1_size = round(final_volume * 0.33, 3)
        trade2_size = round(final_volume * 0.33, 3)
        trade3_size = round(final_volume * 0.34, 3)
        logger.info(f"📦 Scaling out: {trade1_size} / {trade2_size} / {trade3_size} lots")

        symbols = await connection.get_symbols()
        if symbol not in symbols:
            logger.error(f"❌ Symbol {symbol} not found")
            return {"error": f"Symbol {symbol} not found"}

        trade1_result = None
        trade2_result = None
        trade3_result = None

        # Place Trade 1 (TP1) – NO comment argument
        if action.lower() == "buy":
            trade1_result = await connection.create_market_buy_order(
                symbol, trade1_size, stop_loss=sl, take_profit=tp1
            )
        else:
            trade1_result = await connection.create_market_sell_order(
                symbol, trade1_size, stop_loss=sl, take_profit=tp1
            )
        logger.info(f"✅ Trade 1 (TP1) placed: {trade1_result.get('positionId')}")

        # Place Trade 2 (TP2)
        if action.lower() == "buy":
            trade2_result = await connection.create_market_buy_order(
                symbol, trade2_size, stop_loss=sl, take_profit=tp2
            )
        else:
            trade2_result = await connection.create_market_sell_order(
                symbol, trade2_size, stop_loss=sl, take_profit=tp2
            )
        logger.info(f"✅ Trade 2 (TP2) placed: {trade2_result.get('positionId')}")

        # Place Trade 3 (TP3)
        if action.lower() == "buy":
            trade3_result = await connection.create_market_buy_order(
                symbol, trade3_size, stop_loss=sl, take_profit=tp3
            )
        else:
            trade3_result = await connection.create_market_sell_order(
                symbol, trade3_size, stop_loss=sl, take_profit=tp3
            )
        logger.info(f"✅ Trade 3 (TP3) placed: {trade3_result.get('positionId')}")

        # Start monitoring for Break Even
        if trade2_result and trade3_result:
            trade2_id = trade2_result.get('positionId')
            trade3_id = trade3_result.get('positionId')
            asyncio.create_task(
                monitor_position(account_id, trade2_id, trade3_id, entry, sl, tp1, symbol, action.upper())
            )
            logger.info(f"🔍 Break Even monitor started for trades {trade2_id}, {trade3_id}")

        return {
            "trade1": trade1_result,
            "trade2": trade2_result,
            "trade3": trade3_result
        }

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
        "service": "Quantum Bot V.03 - Scaled Trading",
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

        logger.info(f"📊 Entry: {entry}, SL: {sl}, TP1: {tp1}, TP2: {tp2}, TP3: {tp3}")

        if data.get('test', False):
            logger.info(f"🧪 Test signal - no trade placed")
            return jsonify({"status": "test_received"}), 200

        result = asyncio.run(
            place_scaled_trades(target_id, action, final_symbol, volume, entry, sl, tp1, tp2, tp3, use_adaptive)
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
    logger.info(f"🚀 Starting Quantum Bot V.03 on port {port}")
    logger.info(f"📡 MetaApi Token: {'✅' if TOKEN else '❌'}")
    logger.info(f"👤 My Account ID: {'✅' if MY_ACC_ID else '❌'}")
    app.run(host="0.0.0.0", port=port)
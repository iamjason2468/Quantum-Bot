import os
import asyncio
import logging
from flask import Flask, request, jsonify
from metaapi_cloud_sdk import MetaApi

# ------------------------------------------------------------------
# FLASK APP INITIALIZATION
# ------------------------------------------------------------------
app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# ENVIRONMENT VARIABLES
# ------------------------------------------------------------------
TOKEN = os.getenv('META_API_TOKEN')
MY_ACC_ID = os.getenv('MY_ACCOUNT_ID')
FRIEND_ACC_ID = os.getenv('FRIEND_ACCOUNT_ID')

# Validate required environment variables
if not TOKEN:
    logger.error("❌ META_API_TOKEN is not set in environment variables")
if not MY_ACC_ID:
    logger.error("❌ MY_ACCOUNT_ID is not set in environment variables")

# ------------------------------------------------------------------
# ADAPTIVE LOT SIZING (Tiers – Faster Graduation)
# ------------------------------------------------------------------
def get_adaptive_lot(account_balance):
    """
    Returns lot size based on account balance.
    Tiers:
        < $50    → 0.001 (nano)
        $50–200  → 0.01  (micro)
        $200–1000→ 0.05  (mini)
        > $1000  → 0.10  (standard)
    """
    if account_balance < 50:
        return 0.001
    elif account_balance < 200:
        return 0.01
    elif account_balance < 1000:
        return 0.05
    else:
        return 0.10

async def fetch_account_balance(account_id):
    """Retrieve current balance of the MT5 account."""
    api = MetaApi(TOKEN)
    try:
        account = await api.metatrader_account_api.get_account(account_id)
        if account.state != "DEPLOYED":
            logger.info(f"⏳ Deploying account {account_id}...")
            await account.deploy()
            await account.wait_connected()
        
        connection = account.get_streaming_connection()
        await connection.connect()
        await connection.wait_synchronized()
        account_info = await connection.get_account_information()
        balance = account_info.get('balance', 0.0)
        logger.info(f"📊 Account {account_id} balance: ${balance:.2f}")
        return balance
    except Exception as e:
        logger.error(f"❌ Could not fetch balance for {account_id}: {e}")
        return None

async def place_trade(account_id, action, symbol, volume, use_adaptive=True):
    """
    Places a market order.
    If use_adaptive is True, overrides volume with tier‑based lot size.
    """
    api = MetaApi(TOKEN)
    try:
        account = await api.metatrader_account_api.get_account(account_id)
        
        # Ensure account is deployed
        if account.state != "DEPLOYED":
            logger.info(f"⏳ Deploying account {account_id}...")
            await account.deploy()
            await account.wait_connected()
        
        connection = account.get_streaming_connection()
        await connection.connect()
        await connection.wait_synchronized()

        final_volume = volume
        if use_adaptive:
            balance = await fetch_account_balance(account_id)
            if balance is not None:
                final_volume = get_adaptive_lot(balance)
                logger.info(f"📊 Balance: ${balance:.2f} → Lot: {final_volume}")
            else:
                logger.warning("⚠️ Using fallback volume from webhook.")

        # Get symbol specification
        spec = await connection.get_symbol_specification(symbol)
        if not spec:
            logger.error(f"❌ Symbol {symbol} not found on account {account_id}")
            return {"error": f"Symbol {symbol} not found"}

        # Place the order
        if action.lower() == "buy":
            result = await connection.create_market_buy_order(symbol, final_volume)
        else:
            result = await connection.create_market_sell_order(symbol, final_volume)

        logger.info(f"✅ {action.upper()} {final_volume} {symbol} on Account {account_id}")
        logger.info(f"   Order details: {result}")
        return result
    except Exception as e:
        logger.error(f"❌ Trade error for Account {account_id}: {e}")
        return {"error": str(e)}

# ------------------------------------------------------------------
# HEALTH CHECK ENDPOINTS (for connectivity testing)
# ------------------------------------------------------------------
@app.route('/ping', methods=['GET'])
def ping():
    """Simple endpoint to verify the server is reachable."""
    logger.info("🏓 Ping received")
    return jsonify({
        "status": "ok", 
        "message": "Railway server is running"
    }), 200

@app.route('/test-webhook', methods=['POST'])
def test_webhook():
    """Test endpoint that echoes back the received payload without trading."""
    try:
        data = request.json
        logger.info(f"🧪 Test webhook received: {data}")
        return jsonify({
            "status": "test_received",
            "received_payload": data,
            "message": "Webhook connection successful! (No trade executed)"
        }), 200
    except Exception as e:
        logger.error(f"❌ Error in test webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/', methods=['GET'])
def root():
    """Root endpoint for basic connectivity check."""
    return jsonify({
        "service": "Quantum Bot - TradingView to MT5 Bridge",
        "status": "online",
        "endpoints": {
            "/ping": "GET - Health check",
            "/test-webhook": "POST - Test webhook without trading",
            "/webhook": "POST - Live trading webhook"
        }
    }), 200

# ------------------------------------------------------------------
# WEBHOOK ENDPOINT (Live Trading)
# ------------------------------------------------------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    """Main webhook endpoint for TradingView signals."""
    try:
        data = request.json
        
        if not data:
            logger.warning("⚠️ Received empty request")
            return jsonify({"status": "error", "message": "No JSON data received"}), 400
        
        logger.info(f"📨 Received signal: {data}")

        # Determine which account to trade
        user = data.get("user_id", "ME")  # default to "ME" if not provided
        target_id = MY_ACC_ID if user.upper() == "ME" else FRIEND_ACC_ID

        if not target_id:
            logger.error("❌ No valid account ID configured")
            return jsonify({"status": "error", "message": "No valid account ID"}), 400

        # Symbol cleaning (remove broker prefix, add 'm' for Exness Standard)
        raw_symbol = data.get('symbol', 'EURUSD')
        clean_symbol = raw_symbol.split(':')[-1]
        final_symbol = clean_symbol + "m"  # Remove + "m" if not needed for your broker

        # Action (buy/sell)
        action = data.get('action', 'buy').lower()
        if action not in ['buy', 'sell']:
            logger.error(f"❌ Invalid action: {action}")
            return jsonify({"status": "error", "message": "Invalid action"}), 400

        # Volume fallback (will be overridden if adaptive is on)
        volume = float(data.get('volume', 0.01))

        # Adaptive mode flag (set to True by default)
        use_adaptive = data.get('adaptive', True)
        
        # Check if this is a test signal
        if data.get('test', False):
            logger.info(f"🧪 Test signal detected - no trade will be placed")
            return jsonify({
                "status": "test_received",
                "target_account": user,
                "symbol": final_symbol,
                "action": action,
                "message": "Test signal received - no trade executed"
            }), 200

        # Execute asynchronously
        result = asyncio.run(place_trade(target_id, action, final_symbol, volume, use_adaptive))
        
        return jsonify({
            "status": "success", 
            "message": "Trade request processed",
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
    logger.info(f"🚀 Starting Quantum Bot server on port {port}")
    logger.info(f"📡 MetaApi Token: {'✅ Configured' if TOKEN else '❌ Missing'}")
    logger.info(f"👤 My Account ID: {'✅ Configured' if MY_ACC_ID else '❌ Missing'}")
    logger.info(f"👥 Friend Account ID: {'✅ Configured' if FRIEND_ACC_ID else '⚠️ Not set (optional)'}")
    app.run(host="0.0.0.0", port=port)
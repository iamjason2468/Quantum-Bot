import os
import asyncio
from flask import Flask, request
from metaapi_cloud_sdk import MetaApi

app = Flask(__name__)

TOKEN = os.getenv('META_API_TOKEN')
MY_ACC_ID = os.getenv('MY_ACCOUNT_ID')
FRIEND_ACC_ID = os.getenv('FRIEND_ACCOUNT_ID')

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
        connection = account.get_streaming_connection()
        await connection.connect()
        await connection.wait_synchronized()
        account_info = await connection.get_account_information()
        balance = account_info.get('balance', 0.0)
        return balance
    except Exception as e:
        print(f"❌ Could not fetch balance for {account_id}: {e}")
        return None

async def place_trade(account_id, action, symbol, volume, use_adaptive=True):
    """
    Places a market order.
    If use_adaptive is True, overrides volume with tier‑based lot size.
    """
    api = MetaApi(TOKEN)
    try:
        account = await api.metatrader_account_api.get_account(account_id)
        connection = account.get_streaming_connection()
        await connection.connect()
        await connection.wait_synchronized()

        final_volume = volume
        if use_adaptive:
            balance = await fetch_account_balance(account_id)
            if balance is not None:
                final_volume = get_adaptive_lot(balance)
                print(f"📊 Balance: ${balance:.2f} → Lot: {final_volume}")
            else:
                print("⚠️ Using fallback volume from webhook.")

        if action.lower() == "buy":
            result = await connection.create_market_buy_order(symbol, final_volume)
        else:
            result = await connection.create_market_sell_order(symbol, final_volume)

        print(f"✅ {action.upper()} {final_volume} {symbol} on Account {account_id}")
        return result
    except Exception as e:
        print(f"❌ Trade error for Account {account_id}: {e}")
        return None

# ------------------------------------------------------------------
# WEBHOOK ENDPOINT
# ------------------------------------------------------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json

    # Determine which account to trade
    user = data.get("user_id", "ME")   # default to "ME" if not provided
    target_id = MY_ACC_ID if user.upper() == "ME" else FRIEND_ACC_ID

    if not target_id:
        return {"status": "No valid account ID"}, 400

    # Symbol cleaning (remove broker prefix, add 'm' for Exness Standard)
    raw_symbol = data.get('symbol', 'EURUSD')
    clean_symbol = raw_symbol.split(':')[-1]
    final_symbol = clean_symbol + "m"   # Remove + "m" if not needed

    # Action (buy/sell)
    action = data.get('action', 'buy').lower()
    if action not in ['buy', 'sell']:
        return {"status": "Invalid action"}, 400

    # Volume fallback (will be overridden if adaptive is on)
    volume = float(data.get('volume', 0.01))

    # Adaptive mode flag (set to True by default)
    use_adaptive = data.get('adaptive', True)

    # Execute asynchronously
    asyncio.run(place_trade(target_id, action, final_symbol, volume, use_adaptive))

    return {"status": "Trade request processed"}, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
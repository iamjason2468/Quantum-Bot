import os
import asyncio
from flask import Flask, request
from metaapi_cloud_sdk import MetaApi

app = Flask(__name__)

# We use os.getenv to keep your keys secret and safe!
TOKEN = os.getenv('META_API_TOKEN')
MY_ACC_ID = os.getenv('MY_ACCOUNT_ID')
FRIEND_ACC_ID = os.getenv('FRIEND_ACCOUNT_ID')

async def place_trade(account_id, action, symbol, volume):
    api = MetaApi(TOKEN)
    try:
        account = await api.metatrader_account_api.get_account(account_id)
        connection = account.get_streaming_connection()
        await connection.connect()
        await connection.wait_synchronized()
        
        if action.lower() == "buy":
            result = await connection.create_market_buy_order(symbol, volume)
        else:
            result = await connection.create_market_sell_order(symbol, volume)
        
        print(f"✅ Success for Account {account_id}")
    except Exception as e:
        print(f"❌ Error for Account {account_id}: {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    user = data.get("user_id")
    
    # Decide which account to use
    target_id = None
    if user == "ME":
        target_id = MY_ACC_ID
    elif user == "FRIEND" and FRIEND_ACC_ID:
        target_id = FRIEND_ACC_ID
    
    if target_id:
        asyncio.run(place_trade(target_id, data['action'], data['symbol'], float(data['volume'])))
        return {"status": "Trade Sent"}, 200
    return {"status": "Account Not Ready"}, 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

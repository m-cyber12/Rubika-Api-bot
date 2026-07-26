import os
import asyncio
import threading
import random
from rubpy import Client, filters
from rubpy.types import Updates
import google.generativeai as genai
from flask import Flask

genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-pro')

# --- Restore Rubika session from env var (so login persists across redeploys) ---
SESSION_FILE = "my_rubika_account.rp"
session_b64 = os.environ.get("SESSION_B64")
if session_b64 and not os.path.exists(SESSION_FILE):
    import base64
    with open(SESSION_FILE, "wb") as f:
        f.write(base64.b64decode(session_b64))
    print("Session file restored from SESSION_B64.")

client = Client(name="my_rubika_account")

# --- Keep-alive web server (for Render + UptimeRobot) ---
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

async def test_connection():
    try:
        async with Client(name="my_rubika_account") as test_client:
            result = await test_client.send_message('me', 'Bot deployed successfully - connectivity test ✅')
            print(f"TEST SEND OK: {result}")
    except Exception as e:
        print(f"TEST SEND FAILED: {e}")

asyncio.run(test_connection())

@client.on_message_updates()
async def reply_to_pv(update: Updates):
    print(f"RAW UPDATE RECEIVED: {update}")
    user_text = getattr(update, "text", None)
    if not user_text:
        return
    print(f"New message: {user_text}")
    try:
        await asyncio.sleep(random.uniform(5, 8))
        response = model.generate_content(user_text)
        await update.reply(response.text)
        print("Reply sent!")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    print("Bot is running...")
    client.run(phone_number=os.environ.get("RUBIKA_PHONE"))
    

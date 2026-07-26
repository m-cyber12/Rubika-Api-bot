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

client = Client(name="my_rubika_account")

# --- Keep-alive web server (for Render + UptimeRobot) ---
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

@client.on_message_updates(filters.text)
async def reply_to_pv(update: Updates):
    user_text = update.text
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
    client.run()

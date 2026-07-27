import os
import asyncio
import threading
import random
import logging
from rubpy import Client, filters
from rubpy.types import Updates
import google.generativeai as genai
from flask import Flask

logging.basicConfig(level=logging.WARNING)

genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

# --- شخصیت ربات: اینجا اسم و لحن دلخواهت رو تنظیم کن ---
OWNER_NAME = "آقای حسن‌پور"
BOT_PERSONA = f"""
تو دستیار شخصی {OWNER_NAME} هستی که روی اکانت روبیکای اون فعالیت می‌کنی.
با لحن صمیمی و دوستانه و به فارسی جواب بده.
جواب‌ها کوتاه و طبیعی باشن، مثل یه پیام معمولی تو چت، نه رسمی و خشک.
"""

TRIGGER_WORD = "دستیار"  # 👈 توی گروه‌ها فقط وقتی این کلمه توی پیام باشه جواب می‌ده

model = genai.GenerativeModel('gemini-flash-latest', system_instruction=BOT_PERSONA)

# --- حافظه مکالمه: برای هر چت جداگانه نگه داشته می‌شه، تا وقتی ربات ری‌استارت نشه ---
chat_histories = {}
MAX_TURNS = 10  # تعداد رد و بدل‌هایی که به خاطر می‌سپاره

# --- Restore Rubika session from env vars (split into 2 parts to avoid mobile paste truncation) ---
SESSION_FILE = "my_rubika_account.rp"
session_b64 = (os.environ.get("SESSION_B64_PART1", "") + os.environ.get("SESSION_B64_PART2", ""))
if session_b64 and not os.path.exists(SESSION_FILE):
    import base64
    with open(SESSION_FILE, "wb") as f:
        f.write(base64.b64decode(session_b64))
    print(f"Session file restored. Combined base64 length: {len(session_b64)} (should be 21848)")
    print(f"Restored session file size: {os.path.getsize(SESSION_FILE)} bytes (should be 16384)")

client = Client(name="my_rubika_account")

# --- Keep-alive web server (for Render + UptimeRobot) ---
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def get_chat_session(chat_guid):
    if chat_guid not in chat_histories:
        chat_histories[chat_guid] = model.start_chat(history=[])
    return chat_histories[chat_guid]

@client.on_message_updates()
async def reply_to_pv(update: Updates):
    chat_guid = getattr(update, "object_guid", "") or ""
    user_text = getattr(update, "text", None)
    if not user_text:
        return

    is_private = chat_guid.startswith("u0")

    if not is_private:
        if TRIGGER_WORD not in user_text:
            return  # توی گروه/کانال، فقط وقتی کلمه‌ی کلیدی صدا زده بشه جواب بده
        user_text = user_text.replace(TRIGGER_WORD, "", 1).strip()
        if not user_text:
            user_text = "سلام"  # اگه فقط خودِ کلمه‌ی کلیدی فرستاده شده بود

    print(f"New message from {chat_guid} (private={is_private}): {user_text}")
    try:
        await asyncio.sleep(random.uniform(5, 8))
        chat = get_chat_session(chat_guid)
        response = await asyncio.to_thread(chat.send_message, user_text)
        if len(chat.history) > MAX_TURNS * 2:
            chat.history = chat.history[-MAX_TURNS * 2:]
        await update.reply(response.text)
        print("Reply sent!")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    print("Bot is running...")
    client.run(phone_number=os.environ.get("RUBIKA_PHONE"))
    

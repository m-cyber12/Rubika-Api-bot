import os
import asyncio
import threading
import random
from rubpy import Client, handlers, Message
import google.generativeai as genai
from flask import Flask

genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-pro')

client = Client("my_rubika_account")

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

@client.on(handlers.MessageUpdates())
async def reply_to_pv(message: Message):
    if message.is_private and not message.author_guid == client.user.user_guid:
        user_text = message.text
        if user_text:
            print(f"New message: {user_text}")
            try:
                await asyncio.sleep(random.uniform(5, 8))
                response = model.generate_content(user_text)
                await message.reply(response.text)
                print("Reply sent!")
            except Exception as e:
                print(f"Error: {e}")

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    print("Bot is running...")
    client.run()

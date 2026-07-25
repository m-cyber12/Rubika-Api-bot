import os
import asyncio
from rubpy import Client, handlers, Message
import google.generativeai as genai
import random

genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-pro')

client = Client("my_rubika_account")

@client.on(handlers.MessageUpdates())
async def reply_to_pv(message: Message):
    if message.is_private and not message.author_guid == client.user.user_guid:
        user_text = message.text
        if user_text:
            print(f"New message: {user_text}")
            try:
                await asyncio.sleep(random.uniform(3, 8))
                response = model.generate_content(user_text)
                await message.reply(response.text)
                print("Reply sent!")
            except Exception as e:
                print(f"Error: {e}")

print("Bot is running...")
client.run()
          

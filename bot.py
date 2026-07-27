import os
import asyncio
import threading
import random
import logging
import json
import re
from datetime import datetime
from rubpy import Client
from rubpy.types import Updates
import google.generativeai as genai
from flask import Flask, request, jsonify, render_template_string

logging.basicConfig(level=logging.WARNING)
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

# ==================== تنظیمات ====================
OWNER_NAME = "آقای حسن‌پور"
OWNER_CONTROL_GROUP = os.environ.get("OWNER_CONTROL_GROUP", "")

BOT_PERSONA = f"""
تو دستیار شخصی {OWNER_NAME} هستی که روی اکانت روبیکای اون فعالیت می‌کنی.
با لحن صمیمی و دوستانه و به فارسی جواب بده.
جواب‌ها کوتاه و طبیعی باشن، مثل یه پیام معمولی تو چت.

قوانین مهم:
- اگه سوالی درباره {OWNER_NAME} پرسیده شد و جوابش رو می‌دونی، مستقیم جواب بده.
- اگه سوالی درباره {OWNER_NAME} بود و جوابش رو نمی‌دونی، فقط این رو بگو:
  "از {OWNER_NAME} می‌پرسم و بهت می‌گم ⏳"
- هرگز درباره {OWNER_NAME} حدس نزن یا اطلاعات غلط نده.
"""

TRIGGER_WORD = "دستیار"
model = genai.GenerativeModel('gemini-flash-latest', system_instruction=BOT_PERSONA)

# --- حافظه‌ها ---
chat_histories = {}
MAX_TURNS = 10
bot_sent_message_ids = set()

KB_FILE = "knowledge_base.json"
knowledge_base = {}

PENDING_FILE = "pending_replies.json"
pending_replies = {}

LOG_FILE = "chat_log.json"
chat_logs = []

# --- Load/Save ---
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return default
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_all():
    global knowledge_base, pending_replies, chat_logs
    knowledge_base = load_json(KB_FILE, {})
    pending = load_json(PENDING_FILE, {})
    pending_replies = {int(k): v for k, v in pending.items()}
    chat_logs = load_json(LOG_FILE, [])

def save_kb(): save_json(KB_FILE, knowledge_base)
def save_pending(): save_json(PENDING_FILE, {str(k): v for k, v in pending_replies.items()})
def save_logs(): save_json(LOG_FILE, chat_logs)

# --- Restore Rubika session ---
SESSION_FILE = "my_rubika_account.rp"
session_b64 = (os.environ.get("SESSION_B64_PART1", "") + os.environ.get("SESSION_B64_PART2", ""))
if session_b64 and not os.path.exists(SESSION_FILE):
    import base64
    with open(SESSION_FILE, "wb") as f:
        f.write(base64.b64decode(session_b64))

client = Client(name="my_rubika_account")

# ==================== FLASK APP ====================
app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html dir="rtl" lang="fa">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>دستیار روبیکا - کنترل پنل</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Segoe UI', Tahoma, sans-serif; background: #0f0f23; color: #e0e0e0; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { color: #00d4aa; margin-bottom: 20px; text-align: center; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        @media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }
        .card { background: #1a1a2e; border-radius: 12px; padding: 20px; border: 1px solid #2a2a4e; }
        .card h2 { color: #00d4aa; margin-bottom: 15px; font-size: 18px; }
        textarea, input { width: 100%; padding: 12px; border-radius: 8px; border: 1px solid #333; background: #0f0f23; color: #fff; margin-bottom: 10px; font-family: inherit; }
        button { background: #00d4aa; color: #000; border: none; padding: 12px 24px; border-radius: 8px; cursor: pointer; font-weight: bold; transition: 0.2s; }
        button:hover { background: #00b894; }
        .btn-danger { background: #e74c3c; color: #fff; }
        .btn-danger:hover { background: #c0392b; }
        .message { padding: 10px; border-radius: 8px; margin-bottom: 8px; }
        .msg-user { background: #16213e; border-right: 3px solid #00d4aa; }
        .msg-ai { background: #1a1a2e; border-right: 3px solid #e74c3c; }
        .pending-item { background: #16213e; padding: 12px; border-radius: 8px; margin-bottom: 10px; border-right: 3px solid #f39c12; }
        .kb-item { background: #16213e; padding: 10px; border-radius: 8px; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; }
        .kb-item div { flex: 1; }
        .small { font-size: 12px; color: #888; }
        #chat-box { height: 300px; overflow-y: auto; margin-bottom: 10px; padding: 10px; background: #0f0f23; border-radius: 8px; border: 1px solid #333; }
        .stats { display: flex; gap: 15px; margin-bottom: 20px; flex-wrap: wrap; }
        .stat-box { background: #1a1a2e; padding: 15px 25px; border-radius: 10px; text-align: center; border: 1px solid #2a2a4e; }
        .stat-box .num { font-size: 24px; font-weight: bold; color: #00d4aa; }
        .tabs { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
        .tab { padding: 10px 20px; background: #1a1a2e; border-radius: 8px; cursor: pointer; border: 1px solid #2a2a4e; }
        .tab.active { background: #00d4aa; color: #000; font-weight: bold; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 دستیار روبیکا - کنترل پنل</h1>
        <div class="stats">
            <div class="stat-box"><div class="num" id="stat-kb">0</div><div>دانش ذخیره شده</div></div>
            <div class="stat-box"><div class="num" id="stat-pending">0</div><div>در انتظار جواب</div></div>
            <div class="stat-box"><div class="num" id="stat-chats">0</div><div>چت امروز</div></div>
        </div>
        <div class="tabs">
            <div class="tab active" onclick="showTab('chat')">💬 چت با دستیار</div>
            <div class="tab" onclick="showTab('send')">📨 ارسال پیام</div>
            <div class="tab" onclick="showTab('kb')">📚 مدیریت دانش</div>
            <div class="tab" onclick="showTab('pending')">⏳ سوالات در انتظار</div>
            <div class="tab" onclick="showTab('logs')">📋 لاگ‌ها</div>
        </div>
        <div id="tab-chat" class="tab-content active">
            <div class="card">
                <h2>چت با Gemini</h2>
                <div id="chat-box"></div>
                <div style="display: flex; gap: 10px;">
                    <input type="text" id="chat-input" placeholder="پیامت رو بنویس..." style="flex:1;" onkeypress="if(event.key==='Enter') sendChat()">
                    <button onclick="sendChat()">ارسال</button>
                </div>
            </div>
        </div>
        <div id="tab-send" class="tab-content">
            <div class="card">
                <h2>📨 ارسال پیام از طریق ربات</h2>
                <input type="text" id="send-guid" placeholder="GUID چت (مثلاً u0...)">
                <textarea id="send-text" rows="3" placeholder="متن پیام..."></textarea>
                <button onclick="sendMessage()">📤 ارسال پیام</button>
                <p class="small">GUID رو از لاگ‌ها می‌تونی پیدا کنی</p>
            </div>
        </div>
        <div id="tab-kb" class="tab-content">
            <div class="card">
                <h2>📚 مدیریت Knowledge Base</h2>
                <input type="text" id="kb-question" placeholder="سوال (مثلاً: شغلت چیه؟)">
                <textarea id="kb-answer" rows="2" placeholder="جواب..."></textarea>
                <button onclick="addKB()">➕ اضافه کردن</button>
                <div id="kb-list" style="margin-top: 15px;"></div>
            </div>
        </div>
        <div id="tab-pending" class="tab-content">
            <div class="card">
                <h2>⏳ سوالات در انتظار جواب تو</h2>
                <div id="pending-list"></div>
            </div>
        </div>
        <div id="tab-logs" class="tab-content">
            <div class="card">
                <h2>📋 لاگ چت‌های اخیر</h2>
                <div id="logs-list"></div>
            </div>
        </div>
    </div>
    <script>
        function showTab(name) {
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.getElementById('tab-' + name).classList.add('active');
            event.target.classList.add('active');
            if(name === 'kb') loadKB();
            if(name === 'pending') loadPending();
            if(name === 'logs') loadLogs();
        }
        async function sendChat() {
            const input = document.getElementById('chat-input');
            const text = input.value.trim();
            if(!text) return;
            input.value = '';
            addToChat('user', text);
            const res = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: text})});
            const data = await res.json();
            addToChat('ai', data.reply);
        }
        function addToChat(role, text) {
            const box = document.getElementById('chat-box');
            const div = document.createElement('div');
            div.className = 'message msg-' + role;
            div.innerHTML = '<strong>' + (role==='user'?'👤 تو':'🤖 AI') + ':</strong> ' + escapeHtml(text);
            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
        }
        async function sendMessage() {
            const guid = document.getElementById('send-guid').value.trim();
            const text = document.getElementById('send-text').value.trim();
            if(!guid || !text) return alert('GUID و متن رو پر کن!');
            const res = await fetch('/api/send-message', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({guid: guid, text: text})});
            const data = await res.json();
            alert(data.message || data.error);
        }
        async function addKB() {
            const q = document.getElementById('kb-question').value.trim();
            const a = document.getElementById('kb-answer').value.trim();
            if(!q || !a) return alert('سوال و جواب رو پر کن!');
            await fetch('/api/kb', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({question: q, answer: a})});
            document.getElementById('kb-question').value = '';
            document.getElementById('kb-answer').value = '';
            loadKB();
            updateStats();
        }
        async function deleteKB(q) {
            if(!confirm('حذف شود؟')) return;
            await fetch('/api/kb?q=' + encodeURIComponent(q), {method: 'DELETE'});
            loadKB();
            updateStats();
        }
        async function loadKB() {
            const res = await fetch('/api/kb');
            const data = await res.json();
            const list = document.getElementById('kb-list');
            list.innerHTML = '';
            for(const [q, a] of Object.entries(data.kb)) {
                list.innerHTML += '<div class="kb-item"><div><strong>Q:</strong> ' + escapeHtml(q) + '<br><strong>A:</strong> ' + escapeHtml(a) + '</div><button class="btn-danger" onclick="deleteKB(\'' + escapeHtml(q).replace(/'/g,"\\'") + '\')">🗑</button></div>';
            }
        }
        async function loadPending() {
            const res = await fetch('/api/pending');
            const data = await res.json();
            const list = document.getElementById('pending-list');
            list.innerHTML = '';
            for(const [id, info] of Object.entries(data.pending)) {
                list.innerHTML += '<div class="pending-item"><strong>🆔 ' + id + '</strong><br>💬 ' + escapeHtml(info.user_text) + '<br><span class="small">چت: ' + info.chat_guid + '</span><br><input type="text" id="ans-' + id + '" placeholder="جوابت رو بنویس و Enter بزن" style="margin-top:8px;" onkeypress="if(event.key===\\'Enter\\') answerPending(' + id + ')"></div>';
            }
        }
        async function answerPending(id) {
            const input = document.getElementById('ans-' + id);
            const text = input.value.trim();
            if(!text) return;
            const res = await fetch('/api/answer-pending', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({pending_id: parseInt(id), answer: text})});
            const data = await res.json();
            if(data.success) { loadPending(); updateStats(); } else { alert(data.error); }
        }
        async function loadLogs() {
            const res = await fetch('/api/logs');
            const data = await res.json();
            const list = document.getElementById('logs-list');
            list.innerHTML = '';
            data.logs.slice().reverse().forEach(log => {
                list.innerHTML += '<div class="message msg-user"><span class="small">' + log.time + '</span><br><strong>' + log.from + ':</strong> ' + escapeHtml(log.text) + '<br><span class="small">چت: ' + log.guid + '</span></div>';
            });
        }
        async function updateStats() {
            const res = await fetch('/api/stats');
            const data = await res.json();
            document.getElementById('stat-kb').textContent = data.kb_count;
            document.getElementById('stat-pending').textContent = data.pending_count;
            document.getElementById('stat-chats').textContent = data.today_chats;
        }
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        updateStats();
        setInterval(updateStats, 10000);
    </script>
</body>
</html>
"""

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)

@app.route("/api/stats")
def api_stats():
    today = datetime.now().strftime("%Y-%m-%d")
    today_chats = sum(1 for log in chat_logs if log.get("date") == today)
    return jsonify({"kb_count": len(knowledge_base), "pending_count": len(pending_replies), "today_chats": today_chats})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json()
    user_msg = data.get("message", "")
    try:
        chat = model.start_chat(history=[])
        response = chat.send_message(user_msg)
        return jsonify({"reply": response.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/send-message", methods=["POST"])
def api_send_message():
    data = request.get_json()
    guid = data.get("guid", "")
    text = data.get("text", "")
    if not guid or not text:
        return jsonify({"error": "GUID and text required"}), 400
    async def do_send():
        try:
            await client.send_message(guid, text)
            return True
        except Exception as e:
            print(f"Send error: {e}")
            return False
    loop = asyncio.get_event_loop()
    if loop.is_running():
        future = asyncio.run_coroutine_threadsafe(do_send(), loop)
        success = future.result(timeout=10)
    else:
        success = loop.run_until_complete(do_send())
    if success:
        return jsonify({"message": "✅ پیام ارسال شد!"})
    else:
        return jsonify({"error": "❌ خطا در ارسال"}), 500

@app.route("/api/kb", methods=["GET", "POST", "DELETE"])
def api_kb():
    if request.method == "GET":
        return jsonify({"kb": knowledge_base})
    elif request.method == "POST":
        data = request.get_json()
        q = data.get("question", "").strip()
        a = data.get("answer", "").strip()
        if q and a:
            knowledge_base[q] = a
            save_kb()
            return jsonify({"success": True})
        return jsonify({"error": "Invalid data"}), 400
    elif request.method == "DELETE":
        q = request.args.get("q", "")
        if q in knowledge_base:
            del knowledge_base[q]
            save_kb()
            return jsonify({"success": True})
        return jsonify({"error": "Not found"}), 404

@app.route("/api/pending")
def api_pending():
    return jsonify({"pending": {str(k): v for k, v in pending_replies.items()}})

@app.route("/api/answer-pending", methods=["POST"])
def api_answer_pending():
    data = request.get_json()
    pending_id = data.get("pending_id")
    answer = data.get("answer", "").strip()
    if pending_id not in pending_replies:
        return jsonify({"error": "Not found"}), 404
    original = pending_replies.pop(pending_id)
    save_pending()
    knowledge_base[original["user_text"]] = answer
    save_kb()
    async def do_send():
        try:
            await client.send_message(original["chat_guid"], answer)
            return True
        except Exception as e:
            print(f"Error: {e}")
            return False
    loop = asyncio.get_event_loop()
    future = asyncio.run_coroutine_threadsafe(do_send(), loop)
    success = future.result(timeout=10)
    if success:
        return jsonify({"success": True, "message": "✅ جواب ارسال و ذخیره شد!"})
    else:
        pending_replies[pending_id] = original
        save_pending()
        return jsonify({"error": "Send failed"}), 500

@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": chat_logs})

# ==================== قسمت ۲: ربات روبیکا ====================
def get_chat_session(chat_guid):
    if chat_guid not in chat_histories:
        chat_histories[chat_guid] = model.start_chat(history=[])
    return chat_histories[chat_guid]

@client.on_message_updates()
async def handle_messages(update: Updates):
    chat_guid = getattr(update, "object_guid", "") or ""
    user_text = getattr(update, "text", None)
    author_guid = getattr(update, "author_guid", "") or ""
    message_id = getattr(update, "message_id", None)
    
    if not user_text:
        return

    chat_logs.append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "guid": chat_guid,
        "from": author_guid or "unknown",
        "text": user_text
    })
    if len(chat_logs) > 500:
        chat_logs.pop(0)
    save_logs()

    # ۱. ریپلای در گروه کنترل
    if chat_guid == OWNER_CONTROL_GROUP:
        reply_to_id = getattr(update, "reply_to_message_id", None) or getattr(update, "reply_message_id", None)
        if reply_to_id and reply_to_id in pending_replies:
            original = pending_replies.pop(reply_to_id)
            save_pending()
            knowledge_base[original["user_text"]] = user_text
            save_kb()
            try:
                await client.send_message(original["chat_guid"], user_text)
                await update.reply("✅ جوابت ارسال و ذخیره شد!")
            except Exception as e:
                pending_replies[reply_to_id] = original
                save_pending()
                await update.reply(f"❌ خطا: {e}")
            return
        return

    # ۲. پیام عادی
    is_private = chat_guid.startswith("u0")
    if is_private:
        if author_guid and author_guid != chat_guid:
            return
    else:
        reply_to_id = getattr(update, "reply_to_message_id", None) or getattr(update, "reply_message_id", None)
        is_reply_to_bot = reply_to_id is not None and reply_to_id in bot_sent_message_ids
        if TRIGGER_WORD not in user_text and not is_reply_to_bot:
            return
        user_text = user_text.replace(TRIGGER_WORD, "", 1).strip()
        if not user_text:
            user_text = "سلام"

    # ۳. بررسی Knowledge Base
    if user_text in knowledge_base:
        try:
            await asyncio.sleep(random.uniform(1, 3))
            sent = await update.reply(knowledge_base[user_text])
            sent_id = getattr(sent, "message_id", None)
            if sent_id:
                bot_sent_message_ids.add(sent_id)
        except Exception as e:
            print(f"KB Error: {e}")
        return

    # ۴. AI
    try:
        await asyncio.sleep(random.uniform(3, 6))
        chat = get_chat_session(chat_guid)
        
        kb_context = ""
        if knowledge_base:
            kb_context = "\nاطلاعات شناخته شده:\n"
            for q, a in list(knowledge_base.items())[-5:]:
                kb_context += f"- {q}: {a}\n"
        
        response = await chat.send_message_async(user_text + kb_context)
        ai_text = response.text

        waiting_phrases = ["می‌پرسم", "ازش می‌پرسم", "بپرسم", "نمی‌دونم", "نمی‌دانم", "نمی دونم", "اطلاع ندارم"]
        is_waiting = any(phrase in ai_text for phrase in waiting_phrases)

        if is_waiting and OWNER_CONTROL_GROUP:
            notif_text = (
                f"❓ سوال جدید درباره تو\n"
                f"🆔 چت: `{chat_guid}`\n\n"
                f"💬 سوال:\n{user_text}\n\n"
                f"🤖 پیشنهاد AI: {ai_text}\n\n"
                f"⬅️ برای جواب دادن، به این پیام ریپلای کن."
            )
            sent_notif = await client.send_message(OWNER_CONTROL_GROUP, notif_text)
            notif_id = getattr(sent_notif, "message_id", None)
            if notif_id:
                pending_replies[notif_id] = {
                    "chat_guid": chat_guid,
                    "user_text": user_text,
                    "author_guid": author_guid
                }
                save_pending()
            await update.reply(ai_text)
        else:
            sent = await update.reply(ai_text)
            sent_id = getattr(sent, "message_id", None)
            if sent_id:
                bot_sent_message_ids.add(sent_id)
                if len(bot_sent_message_ids) > 500:
                    bot_sent_message_ids.pop()

        if len(chat.history) > MAX_TURNS * 2:
            chat_histories[chat_guid] = model.start_chat(history=chat.history[-MAX_TURNS * 2:])

    except Exception as e:
        print(f"Error: {e}")

# ==================== اجرا ====================
if __name__ == "__main__":
    load_all()
    def run_web():
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port)
    threading.Thread(target=run_web, daemon=True).start()
    print("🚀 Bot + Dashboard running...")
    print(f"📊 Dashboard: http://your-render-url/")
    print(f"📬 Control group: {OWNER_CONTROL_GROUP or 'NOT SET'}")
    client.run(phone_number=os.environ.get("RUBIKA_PHONE"))
    

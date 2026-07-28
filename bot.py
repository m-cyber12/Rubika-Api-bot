import os
import asyncio
import threading
import random
import logging
import json
from datetime import datetime
from rubpy import Client
from rubpy.types import Updates
import google.generativeai as genai
from flask import Flask, request, jsonify, render_template_string

logging.basicConfig(level=logging.INFO)
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

# ==================== تنظیمات ====================
OWNER_NAME = "حسن"
OWNER_CONTROL_GROUP = os.environ.get("OWNER_CONTROL_GROUP", "").strip()

TRIGGER_WORD = "فرایدی"

BOT_PERSONA = f"""
تو دستیار شخصی {OWNER_NAME} هستی که روی اکانت روبیکای اون فعالیت می‌کنی.
با لحن صمیمی و دوستانه و به فارسی جواب بده.
جواب‌ها کوتاه و طبیعی باشن.

قوانین:
- اگه کسی اسم "{OWNER_NAME}" رو برد، منظورش صاحب اکانت ({OWNER_NAME}) هست.
- اگه سوالی درباره {OWNER_NAME} پرسیده شد و بلد بودی، مستقیم جواب بده.
- اگه نمی‌دونی، حتماً بگو: "از {OWNER_NAME} می‌پرسم و بهت می‌گم ⏳"
- هرگز حدس نزن.
"""

model = genai.GenerativeModel('gemini-flash-latest', system_instruction=BOT_PERSONA)

# --- حافظه‌ها ---
chat_histories = {}
MAX_TURNS = 10
bot_sent_message_ids = set()

KB_FILE = "knowledge_base.json"
knowledge_base = {}

PENDING_FILE = "pending_replies.json"
pending_replies = {}
BOT_SENT_FILE = "bot_sent_ids.json"



LOG_FILE = "chat_log.json"
chat_logs = []

main_loop = None

# --- Load/Save ---
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[LOAD ERROR] {path}: {e}")
            return default
    return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[SAVE ERROR] {path}: {e}")
        return False

def load_all():
    global knowledge_base, pending_replies, chat_logs, bot_sent_message_ids
    knowledge_base = load_json(KB_FILE, {})
    pending_raw = load_json(PENDING_FILE, {})
    pending_replies = {}
    for k, v in pending_raw.items():
        # کلیدها رو همیشه به‌عنوان رشته نگه می‌داریم تا گم نشوند
        pending_replies[str(k)] = v
    bot_sent_ids_raw = load_json(BOT_SENT_FILE, [])
    bot_sent_message_ids = set(str(x) for x in bot_sent_ids_raw)
    chat_logs = load_json(LOG_FILE, [])
    print(f"[STARTUP] KB={len(knowledge_base)}, Pending={len(pending_replies)}, Logs={len(chat_logs)}, SentIDs={len(bot_sent_message_ids)}")

def save_kb():
    if save_json(KB_FILE, knowledge_base):
        print(f"[SAVE] KB saved: {len(knowledge_base)} items")
    else:
        print(f"[SAVE FAIL] KB not saved!")

def save_pending():
    ok = save_json(PENDING_FILE, {str(k): v for k, v in pending_replies.items()})
    if ok:
        print(f"[SAVE] Pending saved: {len(pending_replies)} items")
    else:
        print(f"[SAVE FAIL] Pending not saved!")

def save_bot_sent():
    ok = save_json(BOT_SENT_FILE, list(bot_sent_message_ids))
    if ok:
        print(f"[SAVE] Bot sent IDs saved: {len(bot_sent_message_ids)} items")

def save_logs():
    save_json(LOG_FILE, chat_logs)

# --- Restore Rubika session ---
SESSION_FILE = "my_rubika_account.rp"

part1 = os.environ.get("SESSION_B64_PART1", "")
part2 = os.environ.get("SESSION_B64_PART2", "")
session_b64 = part1 + part2

if not session_b64:
    part1 = os.environ.get("session_b64_part1", "")
    part2 = os.environ.get("session_b64_part2", "")
    session_b64 = part1 + part2
    if session_b64:
        print("[SESSION] Found lowercase session_b64_part1/part2")

if session_b64 and not os.path.exists(SESSION_FILE):
    import base64
    try:
        with open(SESSION_FILE, "wb") as f:
            f.write(base64.b64decode(session_b64))
        print(f"[SESSION] Restored {SESSION_FILE}: {os.path.getsize(SESSION_FILE)} bytes")
    except Exception as e:
        print(f"[SESSION] Restore error: {e}")
elif os.path.exists(SESSION_FILE):
    print(f"[SESSION] File exists: {os.path.getsize(SESSION_FILE)} bytes")
else:
    print("[SESSION] No session env vars found!")

client = Client(name="my_rubika_account")

# ==================== HELPER ====================
def send_msg_sync(guid, text, reply_to=None):
    if not guid or not text:
        return False, "Empty guid or text"
    if main_loop is None:
        return False, "Bot not ready yet"
    
    async def _send():
        try:
            if reply_to:
                result = await client.send_message(guid, text, reply_to_message_id=reply_to)
            else:
                result = await client.send_message(guid, text)
            return True, result
        except Exception as e:
            print(f"[SEND FAIL] {guid}: {e}")
            try:
                result = await client.send_message(guid, text)
                return True, result
            except Exception as e2:
                return False, str(e2)
    
    try:
        future = asyncio.run_coroutine_threadsafe(_send(), main_loop)
        return future.result(timeout=15)
    except Exception as e:
        return False, str(e)

# ==================== FLASK APP ====================
app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html dir="rtl" lang="fa">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>دستیار روبیکا</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Tahoma,'Segoe UI',sans-serif;background:#0d1117;color:#c9d1d9;padding:12px}
.container{max-width:900px;margin:0 auto}
h1{color:#58a6ff;text-align:center;margin-bottom:12px;font-size:20px}
.stats{display:flex;gap:10px;margin-bottom:12px;justify-content:center;flex-wrap:wrap}
.stat{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:10px 18px;text-align:center;min-width:90px}
.stat .num{font-size:24px;font-weight:bold;color:#3fb950}
.stat .label{font-size:11px;color:#8b949e;margin-top:3px}
.tabs{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap;justify-content:center}
.tab{background:#21262d;border:1px solid #30363d;border-radius:8px;padding:9px 14px;cursor:pointer;font-size:13px;transition:.2s;color:#c9d1d9}
.tab:hover{background:#30363d}
.tab.active{background:#238636;color:#fff;border-color:#238636;font-weight:bold}
.panel{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:14px;display:none}
.panel.active{display:block}
input,textarea{width:100%;padding:10px;border-radius:6px;border:1px solid #30363d;background:#0d1117;color:#c9d1d9;margin-bottom:8px;font-family:inherit;font-size:14px}
textarea{min-height:70px;resize:vertical}
button{background:#238636;color:#fff;border:none;padding:9px 18px;border-radius:6px;cursor:pointer;font-size:14px}
button:hover{background:#2ea043}
.btn-red{background:#da3633}
.btn-red:hover{background:#f85149}
.msg-box{height:220px;overflow-y:auto;border:1px solid #30363d;border-radius:6px;padding:10px;margin-bottom:8px;background:#0d1117}
.msg{padding:7px 9px;border-radius:6px;margin-bottom:5px;font-size:13px;line-height:1.5}
.msg-u{background:#1f6feb18;border-right:3px solid #58a6ff}
.msg-a{background:#23863618;border-right:3px solid #3fb950}
.kb-item,.pending-item{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:10px;margin-bottom:8px}
.kb-item{display:flex;gap:10px;align-items:flex-start}
.kb-item div,.pending-item div{flex:1;font-size:13px}
.pending-row{display:flex;gap:8px;margin-bottom:10px;align-items:center}
.pending-row input{flex:1;margin:0}
.small{color:#8b949e;font-size:12px}
hr{border:0;border-top:1px solid #30363d;margin:10px 0}
.empty{text-align:center;color:#8b949e;padding:20px;font-size:13px}
</style>
</head>
<body>
<div class="container">
<h1>🤖 دستیار روبیکا</h1>
<div class="stats">
  <div class="stat"><div class="num" id="st-kb">0</div><div class="label">دانش</div></div>
  <div class="stat"><div class="num" id="st-pen">0</div><div class="label">در انتظار</div></div>
  <div class="stat"><div class="num" id="st-log">0</div><div class="label">لاگ امروز</div></div>
</div>
<div class="tabs">
  <button type="button" class="tab active" data-tab="chat">💬 چت با AI</button>
  <button type="button" class="tab" data-tab="send">📨 ارسال پیام</button>
  <button type="button" class="tab" data-tab="kb">📚 دانش</button>
  <button type="button" class="tab" data-tab="pending">⏳ سوالات</button>
  <button type="button" class="tab" data-tab="logs">📋 لاگ</button>
</div>

<div id="panel-chat" class="panel active">
  <div class="msg-box" id="chat-box"></div>
  <div style="display:flex;gap:8px">
    <input type="text" id="chat-in" placeholder="پیامت رو بنویس..." style="flex:1;margin:0">
    <button type="button" id="btn-chat-send">ارسال</button>
  </div>
</div>

<div id="panel-send" class="panel">
  <input type="text" id="s-guid" placeholder="GUID چت (مثلاً u0...)">
  <textarea id="s-text" placeholder="متن پیام..."></textarea>
  <button type="button" id="btn-send-msg">📤 ارسال</button>
  <p class="small">GUID رو از تب لاگ پیدا کن</p>
</div>

<div id="panel-kb" class="panel">
  <input type="text" id="k-q" placeholder="سوال (مثلاً: شغل حسن چیه؟)">
  <textarea id="k-a" placeholder="جواب..."></textarea>
  <button type="button" id="btn-add-kb">➕ ذخیره</button>
  <hr>
  <div id="kb-list"></div>
</div>

<div id="panel-pending" class="panel">
  <div id="pending-list"></div>
</div>

<div id="panel-logs" class="panel">
  <div id="logs-list" style="max-height:400px;overflow-y:auto"></div>
</div>
</div>

<script>
console.log('JS loaded - v5');

function esc(t){const d=document.createElement('div');d.textContent=t||'';return d.innerHTML;}

function switchTab(name){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  document.querySelector('[data-tab="'+name+'"]').classList.add('active');
  if(name==='kb') loadKB();
  if(name==='pending') loadPending();
  if(name==='logs') loadLogs();
}

document.querySelectorAll('.tab').forEach(function(btn){
  btn.addEventListener('click', function(){ switchTab(this.getAttribute('data-tab')); });
});

function addChat(role,text){
  const b=document.getElementById('chat-box');
  const d=document.createElement('div');
  d.className='msg msg-'+role;
  d.innerHTML=(role==='u'?'👤 <b>تو:</b> ':'🤖 <b>AI:</b> ')+esc(text);
  b.appendChild(d); b.scrollTop=b.scrollHeight;
}

async function sendChat(){
  const inp=document.getElementById('chat-in');
  const t=inp.value.trim(); if(!t) return;
  inp.value=''; addChat('u',t);
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg:t})});
    const d=await r.json();
    addChat('a',d.reply||d.error||'خطا');
  }catch(e){addChat('a','❌ خطای شبکه');}
}

document.getElementById('btn-chat-send').addEventListener('click', sendChat);
document.getElementById('chat-in').addEventListener('keydown', function(e){if(e.key==='Enter') sendChat();});

async function sendMsg(){
  const g=document.getElementById('s-guid').value.trim();
  const t=document.getElementById('s-text').value.trim();
  if(!g||!t) return alert('GUID و متن رو پر کن!');
  try{
    const r=await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({guid:g,text:t})});
    const d=await r.json();
    alert(d.ok?'✅ ارسال شد!':'❌ '+(d.error||'خطا'));
  }catch(e){alert('❌ خطای شبکه');}
}
document.getElementById('btn-send-msg').addEventListener('click', sendMsg);

async function addKB(){
  const q=document.getElementById('k-q').value.trim();
  const a=document.getElementById('k-a').value.trim();
  if(!q||!a) return alert('سوال و جواب رو پر کن!');
  try{
    await fetch('/api/kb',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q,a})});
    document.getElementById('k-q').value='';
    document.getElementById('k-a').value='';
    loadKB(); updateStats();
  }catch(e){alert('❌ خطا');}
}
async function delKB(q){
  if(!confirm('حذف شود؟')) return;
  try{
    await fetch('/api/kb?q='+encodeURIComponent(q),{method:'DELETE'});
    loadKB(); updateStats();
  }catch(e){alert('❌ خطا');}
}
async function loadKB(){
  const list=document.getElementById('kb-list');
  try{
    const r=await fetch('/api/kb'); const d=await r.json();
    const items=d.kb||{}; list.innerHTML='';
    if(Object.keys(items).length===0){list.innerHTML='<div class="empty">دانشی ذخیره نشده</div>';return;}
    for(const [q,a] of Object.entries(items)){
      const div=document.createElement('div'); div.className='kb-item';
      div.innerHTML='<div><b>س:</b> '+esc(q)+'<br><b>ج:</b> '+esc(a)+'</div>';
      const btn=document.createElement('button'); btn.className='btn-red'; btn.textContent='🗑';
      btn.addEventListener('click', function(){delKB(q);});
      div.appendChild(btn); list.appendChild(div);
    }
  }catch(e){list.innerHTML='<div class="empty">❌ خطا در بارگذاری</div>';}
}
document.getElementById('btn-add-kb').addEventListener('click', addKB);

async function ansPen(id,text){
  text=text.trim(); if(!text) return;
  try{
    const r=await fetch('/api/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:parseInt(id),text:text})});
    const d=await r.json();
    if(d.ok){loadPending();updateStats();}else alert(d.error||'خطا');
  }catch(e){alert('❌ خطا');}
}
async function loadPending(){
  const list=document.getElementById('pending-list');
  try{
    const r=await fetch('/api/pending'); const d=await r.json();
    const items=d.pending||{}; list.innerHTML='';
    const entries=Object.entries(items);
    if(entries.length===0){list.innerHTML='<div class="empty">سوالی در انتظار نیست</div>';return;}
    for(const [id,info] of entries){
      const item=document.createElement('div'); item.className='pending-item';
      item.innerHTML='<div><b>#'+id+'</b> <span class="small">'+esc(info.chat_guid)+'</span><br>'+esc(info.user_text)+'</div>';
      list.appendChild(item);
      const row=document.createElement('div'); row.className='pending-row';
      const inp=document.createElement('input'); inp.type='text'; inp.placeholder='جوابت رو بنویس...';
      inp.addEventListener('keydown', function(e){if(e.key==='Enter') ansPen(id,inp.value);});
      const btn=document.createElement('button'); btn.textContent='✅';
      btn.addEventListener('click', function(){ansPen(id,inp.value);});
      row.appendChild(inp); row.appendChild(btn); list.appendChild(row);
    }
  }catch(e){list.innerHTML='<div class="empty">❌ خطا در بارگذاری</div>';}
}

async function loadLogs(){
  const list=document.getElementById('logs-list');
  try{
    const r=await fetch('/api/logs'); const d=await r.json();
    const logs=(d.logs||[]).slice().reverse(); list.innerHTML='';
    if(logs.length===0){list.innerHTML='<div class="empty">لاگ خالیه</div>';return;}
    for(const log of logs){
      const div=document.createElement('div'); div.className='msg msg-u';
      div.innerHTML='<span class="small">'+esc(log.time)+' | '+esc(log.guid)+'</span><br><b>'+esc(log.from)+':</b> '+esc(log.text);
      list.appendChild(div);
    }
  }catch(e){list.innerHTML='<div class="empty">❌ خطا در بارگذاری</div>';}
}

async function updateStats(){
  try{
    const r=await fetch('/api/stats'); const d=await r.json();
    document.getElementById('st-kb').textContent=d.kb;
    document.getElementById('st-pen').textContent=d.pen;
    document.getElementById('st-log').textContent=d.today;
  }catch(e){console.log('stats error',e);}
}

updateStats();
setInterval(updateStats,5000);
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
    today_count = sum(1 for log in chat_logs if log.get("date") == today)
    return jsonify({"kb": len(knowledge_base), "pen": len(pending_replies), "today": today_count})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json() or {}
    msg = data.get("msg", "")
    if not msg:
        return jsonify({"error": "Empty"}), 400
    try:
        chat = model.start_chat(history=[])
        res = chat.send_message(msg)
        return jsonify({"reply": res.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json() or {}
    guid = data.get("guid", "").strip()
    text = data.get("text", "").strip()
    ok, result = send_msg_sync(guid, text)
    if ok:
        return jsonify({"ok": True})
    return jsonify({"error": result}), 500

@app.route("/api/kb", methods=["GET", "POST", "DELETE"])
def api_kb():
    if request.method == "GET":
        return jsonify({"kb": knowledge_base})
    elif request.method == "POST":
        data = request.get_json() or {}
        q = data.get("q", "").strip()
        a = data.get("a", "").strip()
        if q and a:
            knowledge_base[q] = a
            save_kb()
            return jsonify({"ok": True})
        return jsonify({"error": "Empty"}), 400
    else:
        q = request.args.get("q", "")
        if q in knowledge_base:
            del knowledge_base[q]
            save_kb()
            return jsonify({"ok": True})
        return jsonify({"error": "Not found"}), 404

@app.route("/api/pending")
def api_pending():
    return jsonify({"pending": {str(k): v for k, v in pending_replies.items()}})

@app.route("/api/answer", methods=["POST"])
def api_answer():
    data = request.get_json() or {}
    pid = str(data.get("id", "")).strip()
    if pid == "" or pid == "None":
        return jsonify({"error": "Invalid id"}), 400
    text = data.get("text", "").strip()
    if pid not in pending_replies:
        return jsonify({"error": "Not found"}), 404
    original = pending_replies.pop(pid)
    save_pending()
    knowledge_base[original["user_text"]] = text
    save_kb()
    ok, result = send_msg_sync(original["chat_guid"], text, reply_to=original.get("message_id"))
    if ok:
        return jsonify({"ok": True})
    pending_replies[pid] = original
    save_pending()
    return jsonify({"error": result}), 500

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
    global main_loop
    if main_loop is None:
        main_loop = asyncio.get_running_loop()
    
    chat_guid = getattr(update, "object_guid", "") or ""
    user_text = getattr(update, "text", None)
    author_guid = getattr(update, "author_guid", "") or ""
    raw_msg_id = getattr(update, "message_id", None)
    
    # نگه‌داری message_id به‌صورت رشته برای سازگاری
    try:
        message_id = str(raw_msg_id).strip() if raw_msg_id is not None and str(raw_msg_id).strip() != "None" else None
    except:
        message_id = None
    
    if not user_text:
        return

    # لاگ
    chat_logs.append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "guid": chat_guid,
        "from": author_guid or "unknown",
        "text": user_text[:200]
    })
    if len(chat_logs) > 1000:
        chat_logs.pop(0)
    save_logs()

    # ۱. گروه کنترل (اگه ست شده باشه)
    if OWNER_CONTROL_GROUP and chat_guid == OWNER_CONTROL_GROUP:
        reply_to = getattr(update, "reply_to_message_id", None) or getattr(update, "reply_message_id", None)
        reply_str = str(reply_to) if reply_to is not None else None
        if reply_str and reply_str in pending_replies:
            original = pending_replies.pop(reply_str)
            save_pending()
            knowledge_base[original["user_text"]] = user_text
            save_kb()
            try:
                await client.send_message(
                    original["chat_guid"], 
                    user_text, 
                    reply_to_message_id=original.get("message_id")
                )
                await update.reply("✅ جوابت ارسال و ذخیره شد!")
            except Exception as e:
                print(f"[CONTROL ERROR] {e}")
                try:
                    await client.send_message(original["chat_guid"], user_text)
                    await update.reply("✅ ارسال شد (بدون ریپلای)")
                except Exception as e2:
                    pending_replies[reply_str] = original
                    save_pending()
                    await update.reply(f"❌ خطا: {e2}")
            return
        return

    # ۲. فیلتر پیام عادی
    is_private = chat_guid.startswith("u0")
    if is_private:
        if author_guid and author_guid != chat_guid:
            return
    else:
        reply_to = getattr(update, "reply_to_message_id", None) or getattr(update, "reply_message_id", None)
        # تبدیل reply_to به رشته برای مقایسه با کلیدهای ذخیره شده
        reply_str = str(reply_to) if reply_to is not None else None
        is_reply_to_bot = reply_str is not None and reply_str in bot_sent_message_ids
        if TRIGGER_WORD not in user_text and not is_reply_to_bot:
            return
        user_text = user_text.replace(TRIGGER_WORD, "", 1).strip()
        if not user_text:
            user_text = "سلام"

    print(f"[MSG] {chat_guid} (pv={is_private}): {user_text[:80]}")

    # ۳. بررسی Knowledge Base
    if user_text in knowledge_base:
        try:
            await asyncio.sleep(random.uniform(1, 3))
            sent = await update.reply(knowledge_base[user_text])
            sid = getattr(sent, "message_id", None)
            if sid is not None:
                bot_sent_message_ids.add(str(sid))
                save_bot_sent()
            print("[KB] Replied from knowledge")
        except Exception as e:
            print(f"[KB ERROR] {e}")
        return

    # ۴. AI
    try:
        await asyncio.sleep(random.uniform(3, 6))
        chat = get_chat_session(chat_guid)
        
        kb_ctx = ""
        if knowledge_base:
            kb_ctx = "\nاطلاعات شناخته شده:\n"
            for q, a in list(knowledge_base.items())[-5:]:
                kb_ctx += f"- {q}: {a}\n"
        
        response = await chat.send_message_async(user_text + kb_ctx)
        ai_text = response.text

        waiting = any(p in ai_text for p in ["می‌پرسم", "ازش می‌پرسم", "بپرسم", "نمی‌دونم", "نمی‌دانم", "نمی دونم", "اطلاع ندارم"])
        print(f"[AI] waiting={waiting} | {ai_text[:100]}")

        if waiting:
            # ثبت توی pending
            pending_id = str(random.randint(100000, 999999))
            pending_item = {
                "chat_guid": chat_guid,
                "user_text": user_text,
                "author_guid": author_guid,
                "message_id": message_id,
                "time": datetime.now().strftime("%H:%M:%S")
            }
            pending_replies[pending_id] = pending_item
            print(f"[PENDING] Adding id={pending_id}, msg_id={message_id}")
            save_pending()
            print(f"[PENDING] Saved to file. Total pending: {len(pending_replies)}")

            # نوتیف گروه کنترل (اگه ست شده باشه)
            if OWNER_CONTROL_GROUP:
                try:
                    notif = (
                        f"❓ سوال جدید\n"
                        f"🆔 چت: `{chat_guid}`\n\n"
                        f"💬 {user_text}\n\n"
                        f"🤖 AI: {ai_text}\n\n"
                        f"⬅️ ریپلای کن تا جواب بفرستم"
                    )
                    sent_notif = await client.send_message(OWNER_CONTROL_GROUP, notif)
                    nid = getattr(sent_notif, "message_id", None)
                    nid_str = str(nid) if nid is not None else None
                    if nid_str:
                        pending_replies[nid_str] = pending_replies.pop(pending_id)
                        try:
                            pending_replies[nid_str]["message_id"] = message_id
                        except:
                            pass
                        save_pending()
                        print(f"[NOTIF] Sent to control group, nid={nid_str}")
                except Exception as e:
                    print(f"[NOTIF ERROR] {e}")

            # پیام "منتظر" به کاربر
            try:
                sent = await update.reply(ai_text)
                sid = getattr(sent, "message_id", None)
                if sid is not None:
                    bot_sent_message_ids.add(str(sid))
                    save_bot_sent()
                print("[REPLY] Wait message sent to user")
            except Exception as e:
                print(f"[REPLY ERROR] {e}")
        else:
            sent = await update.reply(ai_text)
            sid = getattr(sent, "message_id", None)
            if sid is not None:
                bot_sent_message_ids.add(str(sid))
                save_bot_sent()
                if len(bot_sent_message_ids) > 500:
                    bot_sent_message_ids.pop()
                    save_bot_sent()
            print("[AI] Direct reply sent")

        if len(chat.history) > MAX_TURNS * 2:
            chat_histories[chat_guid] = model.start_chat(history=chat.history[-MAX_TURNS * 2:])

    except Exception as e:
        print(f"[ERROR] {e}")

# ==================== اجرا ====================
if __name__ == "__main__":
    load_all()
    
    def run_web():
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port, threaded=True)
    
    threading.Thread(target=run_web, daemon=True).start()
    print("=" * 50)
    print("🚀 Bot + Dashboard running")
    print(f"📊 URL: https://your-app.onrender.com/")
    print(f"📬 Control Group: {OWNER_CONTROL_GROUP or 'OFF (فقط پنل)'}")
    print(f"🧠 KB: {len(knowledge_base)} | ⏳ Pending: {len(pending_replies)}")
    print("=" * 50)
    
    RUBIKA_PHONE = os.environ.get("RUBIKA_PHONE") or os.environ.get("rubika_phone")
    if RUBIKA_PHONE:
        print(f"📱 Using phone: {RUBIKA_PHONE[:6]}...")
        client.run(phone_number=RUBIKA_PHONE)
    else:
        print("📱 No phone number set, trying session auth...")
        client.run()
        

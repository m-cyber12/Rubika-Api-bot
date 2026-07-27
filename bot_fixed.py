import os
import asyncio
import threading
import random
import logging
import json
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from rubpy import Client
from rubpy.types import Updates
import google.generativeai as genai
from flask import Flask, request, jsonify, render_template_string

logging.basicConfig(level=logging.INFO)

# ==================== چند کلید API ====================
api_keys_str = os.environ.get("GEMINI_API_KEYS", os.environ.get("GEMINI_API_KEY", ""))
api_keys = [k.strip() for k in api_keys_str.split(",") if k.strip()]
if not api_keys:
    print("❌ ERROR: No Gemini API keys found!")
    exit(1)

def pick_key():
    return random.choice(api_keys)

genai.configure(api_key=pick_key())

# ==================== تنظیمات ====================
OWNER_NAME = "حسن"
OWNER_CONTROL_GROUP = os.environ.get("OWNER_CONTROL_GROUP", "").strip()
TRIGGER_WORD = "فرایدی"

def get_persian_year():
    now = datetime.now()
    if now.month > 3 or (now.month == 3 and now.day >= 21):
        return now.year - 621
    return now.year - 622

PERSIAN_YEAR = get_persian_year()

BOT_PERSONA = f"""
تو دستیار شخصی {OWNER_NAME} هستی که روی اکانت روبیکای اون فعالیت می‌کنی.
با لحن صمیمی و دوستانه و به فارسی جواب بده.
جواب‌ها کوتاه و طبیعی باشن.

قوانین:
- اگه کسی اسم "{OWNER_NAME}" رو برد، منظورش صاحب اکانت ({OWNER_NAME}) هست.
- اگه سوالی درباره {OWNER_NAME} پرسیده شد و بلد بودی، مستقیم جواب بده.
- اگه نمی‌دونی، حتماً بگو: "از {OWNER_NAME} می‌پرسم و بهت می‌گم ⏳"
- هرگز حدس نزن.
- اگه کسی از سن یا سال یا تولد پرسید، با توجه به سال {PERSIAN_YEAR} شمسی حساب کن.
"""

# --- حافظه‌ها ---
chat_histories = {}
MAX_TURNS = 10

# FIX #1: آیدی پیام‌های ربات همیشه به صورت «رشته» نگهداری می‌شن
# چون روبیکا message_id رو str برمی‌گردونه ولی قبلاً جاهایی int می‌شد.
bot_sent_message_ids = set()

BOT_IDS_FILE = "bot_message_ids.json"   # FIX #1: بعد از ری‌استارت هم یادش بمونه
MAX_BOT_IDS = 3000

MY_GUID = None  # گویید خود اکانت، بعد از استارت پر می‌شه


def norm_id(value):
    """
    FIX #1 (کلید اصلی حل باگ ریپلای):
    همه‌ی آیدی‌ها (message_id / reply_to_message_id) رو به str نرمال می‌کنه
    تا مقایسه‌ی int با str باعث نشه ریپلای تشخیص داده نشه.
    """
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def remember_bot_message(sent):
    """آیدی پیامی که ربات فرستاده رو ذخیره می‌کنه (نرمال‌شده)."""
    sid = None
    if sent is not None:
        sid = norm_id(getattr(sent, "message_id", None))
        if sid is None:
            try:
                sid = norm_id(sent["message_update"]["message_id"])
            except Exception:
                sid = None
    if sid:
        bot_sent_message_ids.add(sid)
        if len(bot_sent_message_ids) > MAX_BOT_IDS:
            for old in list(bot_sent_message_ids)[: len(bot_sent_message_ids) - MAX_BOT_IDS]:
                bot_sent_message_ids.discard(old)
        save_bot_ids()
    return sid


def get_reply_to_id(update):
    """
    FIX #1: گرفتن آیدی پیامی که کاربر بهش ریپلای زده.
    rubpy فقط پراپرتی reply_message_id داره؛ reply_to_message_id مستقیم روی
    Update نیست و از داخل message خونده می‌شه. اینجا همه‌ی حالت‌ها چک می‌شن.
    """
    for attr in ("reply_message_id", "reply_to_message_id"):
        try:
            val = getattr(update, attr, None)
        except Exception:
            val = None
        if val:
            return norm_id(val)
    # آخرین تلاش: مستقیم از دیکشنری خام آپدیت
    try:
        raw = update.original_update or {}
        msg = raw.get("message") or {}
        val = msg.get("reply_to_message_id") or raw.get("reply_to_message_id")
        if val:
            return norm_id(val)
    except Exception:
        pass
    return None

KB_FILE = "knowledge_base.json"
knowledge_base = {}

PENDING_FILE = "pending_replies.json"
pending_replies = {}

LOG_FILE = "chat_log.json"
chat_logs = []

main_loop = None
executor = ThreadPoolExecutor(max_workers=4)

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
    # FIX #2: کلیدها به صورت str نگه داشته می‌شن.
    # قبلاً int(k) بود و هر کلیدی که عددی نبود (یا کلید ذخیره‌شده‌ی
    # message_id گروه کنترل که str بود) با except رد می‌شد و pending خالی می‌موند.
    for k, v in pending_raw.items():
        key = norm_id(k)
        if key:
            pending_replies[key] = v
    chat_logs = load_json(LOG_FILE, [])
    bot_sent_message_ids = set(norm_id(i) for i in load_json(BOT_IDS_FILE, []) if norm_id(i))
    print(f"[STARTUP] KB={len(knowledge_base)}, Pending={len(pending_replies)}, "
          f"Logs={len(chat_logs)}, BotMsgIDs={len(bot_sent_message_ids)}")

def save_kb():
    if save_json(KB_FILE, knowledge_base):
        print(f"[SAVE] KB: {len(knowledge_base)} items")

def save_pending():
    ok = save_json(PENDING_FILE, {str(k): v for k, v in pending_replies.items()})
    if ok:
        print(f"[SAVE] Pending: {len(pending_replies)} items")

def save_bot_ids():
    save_json(BOT_IDS_FILE, list(bot_sent_message_ids))

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
        print("[SESSION] Found lowercase session vars")

if session_b64 and not os.path.exists(SESSION_FILE):
    import base64
    try:
        with open(SESSION_FILE, "wb") as f:
            f.write(base64.b64decode(session_b64))
        print(f"[SESSION] Restored: {os.path.getsize(SESSION_FILE)} bytes")
    except Exception as e:
        print(f"[SESSION] Restore error: {e}")
elif os.path.exists(SESSION_FILE):
    print(f"[SESSION] Exists: {os.path.getsize(SESSION_FILE)} bytes")
else:
    print("[SESSION] No session vars!")

client = Client(name="my_rubika_account")

# ==================== HELPER ====================
def send_msg_sync(guid, text, reply_to=None):
    if not guid or not text:
        return False, "Empty"
    if main_loop is None:
        return False, "No loop"
    
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
console.log('JS loaded - v8');

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
    /* FIX #2: آیدی رو رشته‌ای می‌فرستیم. parseInt روی آیدی‌های بلند روبیکا
       (که از حد Number رد می‌شن یا صفر ابتدایی دارن) خرابش می‌کرد و 404 می‌گرفتیم. */
    const r=await fetch('/api/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:String(id),text:text})});
    const d=await r.json();
    if(d.ok){
      loadPending(); updateStats(); 
      alert(d.message || '✅ ذخیره شد');
    }else{
      alert(d.error || 'خطا');
    }
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
      item.innerHTML='<div><b>#'+esc(id)+'</b> <span class="small">'+esc(info.chat_guid)+' • '+esc(info.time||'')+'</span><br>'+esc(info.user_text)+'</div>';
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

let lastPenCount=-1;
async function updateStats(){
  try{
    const r=await fetch('/api/stats'); const d=await r.json();
    document.getElementById('st-kb').textContent=d.kb;
    document.getElementById('st-pen').textContent=d.pen;
    document.getElementById('st-log').textContent=d.today;
    /* FIX #2: اگه تب سوالات بازه و تعدادش عوض شده، خودکار تازه بشه.
       (فقط وقتی تعداد فرق کرده، تا متنی که داری تایپ می‌کنی پاک نشه) */
    const pp=document.getElementById('panel-pending');
    if(pp && pp.classList.contains('active') && d.pen!==lastPenCount) loadPending();
    lastPenCount=d.pen;
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
        genai.configure(api_key=pick_key())
        m = genai.GenerativeModel('gemini-flash-latest', system_instruction=BOT_PERSONA)
        chat = m.start_chat(history=[])
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
    # FIX #2: کلیدها str و جدیدترین سوال بالاتر
    items = sorted(
        pending_replies.items(),
        key=lambda kv: (kv[1].get("date", ""), kv[1].get("time", "")),
        reverse=True,
    )
    return jsonify({"pending": {str(k): v for k, v in items}, "count": len(items)})

# جواب از پنل → فقط KB ذخیره، ارسال نمی‌شه
@app.route("/api/answer", methods=["POST"])
def api_answer():
    data = request.get_json() or {}
    # FIX #2: آیدی از پنل به صورت رشته/عدد میاد؛ نرمال‌سازی می‌شه
    # تا با کلید ذخیره‌شده (str) مچ بشه. قبلاً همیشه 404 می‌داد.
    pid = norm_id(data.get("id"))
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "متن جواب خالیه"}), 400
    if pid not in pending_replies:
        return jsonify({"error": "Not found"}), 404
    original = pending_replies.pop(pid)
    save_pending()
    knowledge_base[original["user_text"]] = text
    save_kb()
    return jsonify({
        "ok": True,
        "message": f"✅ ذخیره شد!\n\nسوال: {original['user_text']}\nجواب: {text}\n\nAI از دفعه بعد استفاده می‌کنه."
    })

@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": chat_logs})

# ==================== قسمت ۲: ربات روبیکا ====================
# نکته: این تابع قبلاً به متغیر `model` اشاره می‌کرد که هیچ‌جا تعریف نشده بود
# (NameError). چون هیچ‌جا صدا زده نمی‌شد باگ خودش رو نشون نمی‌داد؛ درستش شد.
def get_chat_session(chat_guid):
    if chat_guid not in chat_histories:
        genai.configure(api_key=pick_key())
        m = genai.GenerativeModel('gemini-flash-latest', system_instruction=BOT_PERSONA)
        chat_histories[chat_guid] = m.start_chat(history=[])
    return chat_histories[chat_guid]

def is_age_question(text):
    if not text:
        return False
    return any(kw in text for kw in ["چند سال", "سن", "سالش", "عمر", "قدیمی", "تولد", "متولد", "چندسال"])

def is_about_owner(text):
    return OWNER_NAME in text if text else False


# FIX #2: لیست کامل‌تر عبارت‌های «نمی‌دونم» تا سوال حتماً توی صف بیفته
WAITING_PATTERNS = [
    r"می[‌\s]?پرسم", r"بپرسم", r"سوال\s*می[‌\s]?کنم",
    r"نمی[‌\s]?دان", r"نمی[‌\s]?دون", r"نمیدون", r"نمیدان",
    r"اطلاع\w*\s*(دقیقی\s*)?ندار", r"اطلاعی\s*ندار", r"خبر\s*ندار",
    r"مطمئن\s*نیستم", r"نمی[‌\s]?تونم\s*(بگم|جواب)", r"یادم\s*نیست",
    r"در\s*جریان\s*نیستم", r"چیزی\s*ندار",
]


def looks_like_waiting(text):
    if not text:
        return False
    return any(re.search(p, text) for p in WAITING_PATTERNS)


async def is_reply_to_me(update, reply_to):
    """
    FIX #1: تشخیص اینکه کاربر به پیام «ربات» ریپلای زده یا نه.
    مرحله ۱ (سریع): آیدی توی حافظه‌ی pیام‌های ارسالی ربات هست؟
    مرحله ۲ (مطمئن): اگه نبود (ری‌استارت شده یا حافظه پاک شده)،
    خود پیام ریپلای‌شده رو از سرور می‌گیریم و می‌بینیم نویسنده‌ش خودمونیم یا نه.
    """
    if not reply_to:
        return False

    if reply_to in bot_sent_message_ids:
        return True

    try:
        result = await client.get_messages_by_id(
            getattr(update, "object_guid", None), [str(reply_to)]
        )
        messages = getattr(result, "messages", None) or []
        if messages:
            author = norm_id(getattr(messages[0], "author_object_guid", None))
            me = norm_id(MY_GUID or getattr(client, "guid", None))
            if author and me and author == me:
                bot_sent_message_ids.add(reply_to)   # کش کن برای دفعه بعد
                save_bot_ids()
                return True
    except Exception as e:
        print(f"[REPLY CHECK ERROR] {e}")

    return False

@client.on_message_updates()
async def handle_messages(update: Updates):
    global main_loop
    if main_loop is None:
        main_loop = asyncio.get_running_loop()
    
    chat_guid = getattr(update, "object_guid", "") or ""
    user_text = getattr(update, "text", None)
    author_guid = getattr(update, "author_guid", "") or ""

    # FIX #1: دیگه int() نمی‌کنیم. روبیکا آیدی رو رشته می‌ده و
    # تبدیل به int باعث می‌شد مقایسه با reply_to_message_id (رشته) همیشه False بشه.
    message_id = norm_id(getattr(update, "message_id", None))

    if not user_text:
        return

    # FIX #1: پیام‌های خود ربات رو نادیده بگیر (جلوگیری از حلقه‌ی جواب به خود)
    me = norm_id(MY_GUID or getattr(client, "guid", None))
    if me and norm_id(author_guid) == me:
        if message_id:
            bot_sent_message_ids.add(message_id)
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

    # FIX #1: آیدی پیامی که بهش ریپلای شده، یک‌بار و درست خونده می‌شه
    reply_to = get_reply_to_id(update)

    # ۱. گروه کنترل (اگه ست شده باشه)
    if OWNER_CONTROL_GROUP and chat_guid == OWNER_CONTROL_GROUP:
        if reply_to and reply_to in pending_replies:
            original = pending_replies.pop(reply_to)
            save_pending()
            knowledge_base[original["user_text"]] = user_text
            save_kb()
            await update.reply("✅ جوابت ذخیره شد توی دانش.")

            # FIX #2: جواب رو برای همون کسی که پرسیده بود هم بفرست
            target = original.get("chat_guid")
            if target:
                try:
                    sent = await client.send_message(
                        target, user_text,
                        reply_to_message_id=original.get("message_id") or None
                    )
                    remember_bot_message(sent)
                except Exception as e:
                    print(f"[FORWARD ANSWER ERROR] {e}")
            return
        return

    # ۲. فیلتر پیام عادی
    is_private = chat_guid.startswith("u0")
    if is_private:
        if author_guid and author_guid != chat_guid:
            return
    else:
        # FIX #1: مقایسه‌ی نرمال‌شده + fallback به سرور
        is_reply_to_bot = await is_reply_to_me(update, reply_to)
        has_trigger = TRIGGER_WORD in user_text
        print(f"[GROUP] reply_to={reply_to} is_reply_to_bot={is_reply_to_bot} trigger={has_trigger}")

        if not has_trigger and not is_reply_to_bot:
            return
        if has_trigger:
            user_text = user_text.replace(TRIGGER_WORD, "", 1).strip()
        if not user_text:
            user_text = "سلام"

    print(f"\n[MSG] {chat_guid} (pv={is_private}): {user_text[:80]}")

    # ۳. بررسی Knowledge Base
    kb_answer = knowledge_base.get(user_text)
    
    if kb_answer and not is_age_question(user_text) and not is_age_question(kb_answer):
        try:
            await asyncio.sleep(random.uniform(1, 3))
            sent = await update.reply(kb_answer)
            remember_bot_message(sent)   # FIX #1
            print("[KB] Direct reply")
            return
        except Exception as e:
            print(f"[KB ERROR] {e}")

    # ۴. AI
    try:
        await asyncio.sleep(random.uniform(2, 4))
        
        kb_ctx = ""
        if knowledge_base:
            kb_ctx = "\nاطلاعات شناخته شده:\n"
            for q, a in list(knowledge_base.items())[-5:]:
                kb_ctx += f"- {q}: {a}\n"
        
        date_ctx = f"\nسال شمسی الان: {PERSIAN_YEAR}\n"
        full_prompt = user_text + date_ctx + kb_ctx
        
        print(f"[AI] Sending prompt... ({len(full_prompt)} chars)")
        
        # ساخت model با کلید رندوم
        genai.configure(api_key=pick_key())
        m = genai.GenerativeModel('gemini-flash-latest', system_instruction=BOT_PERSONA)
        chat = m.start_chat(history=[])
        
        # ارسال sync توی thread جداگانه (stable‌تر)
        def _ai_send():
            return chat.send_message(full_prompt)
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(executor, _ai_send)
        ai_text = response.text
        print(f"[AI RAW] {ai_text[:120]}")

        # تشخیص waiting  (FIX #2: الگوهای کامل‌تر)
        waiting = looks_like_waiting(ai_text)
        if waiting:
            print("[AI] Detected waiting phrase")

        # fallback: سوال درباره حسن بدون جواب KB
        if not waiting and is_about_owner(user_text) and not kb_answer:
            waiting = True
            ai_text = f"از {OWNER_NAME} می‌پرسم و بهت می‌گم ⏳"
            print("[FALLBACK] Owner question, no KB -> pending")

        if waiting:
            # ثبت pending
            # FIX #2: کلید همیشه str، تا با /api/pending و /api/answer یکی باشه
            pending_id = str(random.randint(100000, 999999))
            entry = {
                "chat_guid": chat_guid,
                "user_text": user_text,
                "author_guid": author_guid,
                "message_id": message_id,
                "time": datetime.now().strftime("%H:%M:%S"),
                "date": datetime.now().strftime("%Y-%m-%d"),
            }
            pending_replies[pending_id] = entry
            save_pending()
            print(f"[PENDING] id={pending_id} total={len(pending_replies)}")

            # نوتیف گروه کنترل
            if OWNER_CONTROL_GROUP:
                try:
                    notif = f"❓ سوال جدید\n🆔 {chat_guid}\n\n💬 {user_text}\n\n🤖 {ai_text}\n\n⬅️ ریپلای کن تا ذخیره کنم"
                    sent_notif = await client.send_message(OWNER_CONTROL_GROUP, notif)
                    nid = norm_id(getattr(sent_notif, "message_id", None))
                    # FIX #2: کلید رو با آیدی پیام گروه کنترل عوض می‌کنیم
                    # ولی حواسمون هست که entry گم نشه اگه nid تکراری/خالی بود.
                    if nid and nid != pending_id:
                        pending_replies[nid] = pending_replies.pop(pending_id, entry)
                        pending_replies[nid]["message_id"] = message_id
                        save_pending()
                except Exception as e:
                    print(f"[NOTIF ERROR] {e}")

            # پیام "منتظر" به کاربر
            try:
                sent = await update.reply(ai_text)
                remember_bot_message(sent)   # FIX #1
            except Exception as e:
                print(f"[REPLY ERROR] {e}")
        else:
            sent = await update.reply(ai_text)
            remember_bot_message(sent)       # FIX #1
            print("[AI] Direct reply sent")

    except Exception as e:
        print(f"[AI ERROR] {e}")
        try:
            await update.reply("یه مشکلی پیش اومد، دوباره امتحان کن 😅")
        except:
            pass

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
    print(f"📬 Control Group: {OWNER_CONTROL_GROUP or 'OFF'}")
    print(f"🧠 KB: {len(knowledge_base)} | ⏳ Pending: {len(pending_replies)}")
    print(f"🔑 API Keys: {len(api_keys)}")
    print("=" * 50)
    
    RUBIKA_PHONE = os.environ.get("RUBIKA_PHONE") or os.environ.get("rubika_phone")

    # FIX #1: بعد از استارت، گوییدِ خود اکانت رو می‌گیریم تا بتونیم
    # تشخیص بدیم یه پیام مالِ خودمونه یا نه (برای تشخیص ریپلای به ربات).
    async def _boot():
        global MY_GUID, main_loop
        main_loop = asyncio.get_running_loop()
        try:
            me = await client.get_me()
            MY_GUID = norm_id(
                getattr(getattr(me, "user", None), "user_guid", None)
                or getattr(client, "guid", None)
            )
            print(f"[BOOT] MY_GUID = {MY_GUID}")
        except Exception as e:
            print(f"[BOOT] get_me failed: {e}")

    if RUBIKA_PHONE:
        print(f"📱 Phone: {RUBIKA_PHONE[:6]}...")
        client.run(_boot(), phone_number=RUBIKA_PHONE)
    else:
        print("📱 Session auth...")
        client.run(_boot())
        

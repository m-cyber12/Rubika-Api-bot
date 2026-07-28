import os
import asyncio
import threading
import random
import logging
import json
import re
import shutil
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
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
قوانین مهم:
- اگه سوالی پرسیده شد و جوابش توی اطلاعات داده شده بود، مستقیم جواب بده.
- اگه سوالی درباره {OWNER_NAME} پرسیده شد و بلد بودی، مستقیم جواب بده.
- اگه نمی‌دونی، حتماً بگو: "از {OWNER_NAME} می‌پرسم و بهت می‌گم ⏳"
- هرگز حدس نزن.
- جواب‌هات کوتاه و خودمونی باشن.
- اگه کسی ریپلای زد به پیام‌هات، باهاش ادامه بحث بده.
"""

model = genai.GenerativeModel('gemini-flash-latest', system_instruction=BOT_PERSONA)

# --- حافظه‌ها ---
chat_histories = {}
MAX_TURNS = 10

# FIX #1: آیدی پیام‌های ربات همیشه به صورت «رشته» نگهداری می‌شن
# چون روبیکا message_id رو str برمی‌گردونه ولی قبلاً جاهایی int می‌شد.
bot_sent_message_ids = set()
BOT_IDS_FILE = "bot_message_ids.json"
MAX_BOT_IDS = 3000

MY_GUID = None  # گویید خود اکانت، بعد از استارت پر می‌شه

KB_FILE = "knowledge_base.json"
knowledge_base = {}

PENDING_FILE = "pending_replies.json"
pending_replies = {}

LOG_FILE = "chat_log.json"
chat_logs = []

main_loop = None
executor = ThreadPoolExecutor(max_workers=4)


# ==================== FIX #1: نرمال‌سازی آیدی ====================
def norm_id(value):
    """
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
            for old in list(bot_sent_message_ids)[:len(bot_sent_message_ids) - MAX_BOT_IDS]:
                bot_sent_message_ids.discard(old)
            save_bot_ids()
    return sid


def get_reply_to_id(update):
    """
    گرفتن آیدی پیامی که کاربر بهش ریپلای زده.
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


async def is_reply_to_me(update, reply_to):
    """
    تشخیص اینکه کاربر به پیام «ربات» ریپلای زده یا نه.
    مرحله ۱ (سریع): آیدی توی حافظه‌ی پیام‌های ارسالی ربات هست؟
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
                bot_sent_message_ids.add(reply_to)  # کش کن برای دفعه بعد
                save_bot_ids()
                return True
    except Exception as e:
        print(f"[REPLY CHECK ERROR] {e}")

    return False


# لیست کامل‌تر عبارت‌های «نمی‌دونم» تا سوال حتماً توی صف بیفته
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


# ==================== توابع کمکی برای فایل ====================
def safe_load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[LOAD ERROR] {path}: {e}")
        try:
            backup = path + ".corrupt"
            shutil.copy(path, backup)
            print(f"[BACKUP] Corrupt file saved as {backup}")
        except:
            pass
        return default


def safe_save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[SAVE ERROR] {path}: {e}")
        return False


def load_all():
    global knowledge_base, pending_replies, chat_logs, bot_sent_message_ids
    knowledge_base = safe_load_json(KB_FILE, {})
    pending_raw = safe_load_json(PENDING_FILE, {})
    pending_replies = {}
    # FIX #2: کلیدها به صورت str نگه داشته می‌شن.
    # قبلاً int(k) بود و هر کلیدی که عددی نبود (یا کلید ذخیره‌شده‌ی
    # message_id گروه کنترل که str بود) با except رد می‌شد و pending خالی می‌موند.
    for k, v in pending_raw.items():
        key = norm_id(k)
        if key:
            pending_replies[key] = v
    chat_logs = safe_load_json(LOG_FILE, [])
    bot_sent_message_ids = set(norm_id(i) for i in safe_load_json(BOT_IDS_FILE, []) if norm_id(i))
    print(f"[STARTUP] KB={len(knowledge_base)}, Pending={len(pending_replies)}, "
          f"Logs={len(chat_logs)}, BotMsgIDs={len(bot_sent_message_ids)}")


def save_kb():
    if safe_save_json(KB_FILE, knowledge_base):
        print(f"[SAVE] KB: {len(knowledge_base)} items")


def save_pending():
    if safe_save_json(PENDING_FILE, pending_replies):
        print(f"[SAVE] Pending: {len(pending_replies)} items")


def save_bot_ids():
    safe_save_json(BOT_IDS_FILE, list(bot_sent_message_ids))


def save_logs():
    safe_save_json(LOG_FILE, chat_logs)


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


# ==================== توابع کمکی برای ارسال و شناسایی ریپلای ====================
async def send_and_track(guid, text, reply_to=None):
    """ارسال پیام و ثبت message_id در set برای تشخیص ریپلای"""
    try:
        if reply_to:
            sent = await client.send_message(guid, text, reply_to_message_id=reply_to)
        else:
            sent = await client.send_message(guid, text)

        remember_bot_message(sent)
        return sent
    except Exception as e:
        print(f"[SEND ERROR] {e}")
        raise


def send_msg_sync(guid, text, reply_to=None):
    """نسخه همگام برای استفاده از پنل فلاسک"""
    if not guid or not text:
        return False, "Empty"
    if main_loop is None:
        return False, "No loop"

    async def _send():
        try:
            return await send_and_track(guid, text, reply_to)
        except:
            return None

    try:
        future = asyncio.run_coroutine_threadsafe(_send(), main_loop)
        result = future.result(timeout=15)
        if result:
            return True, "OK"
        return False, "Send failed"
    except Exception as e:
        return False, str(e)


# ==================== تابع AI با retry ====================
async def ask_gemini(prompt):
    """درخواست به Gemini با تلاش مجدد"""
    last_error = None
    for attempt in range(3):
        try:
            temp_model = genai.GenerativeModel('gemini-flash-latest', system_instruction=BOT_PERSONA)
            chat = temp_model.start_chat(history=[])
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(executor, chat.send_message, prompt)
            return response.text
        except Exception as e:
            last_error = e
            print(f"[GEMINI] Attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(1)
    raise last_error or Exception("All Gemini attempts failed")


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
.stat-card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 20px;text-align:center;min-width:100px}
.stat-card .num{font-size:24px;font-weight:bold;color:#58a6ff}
.stat-card .label{font-size:12px;color:#8b949e}
.tabs{display:flex;gap:4px;margin-bottom:12px;border-bottom:1px solid #30363d}
.tab{padding:8px 16px;cursor:pointer;border-bottom:2px solid transparent;color:#8b949e;font-size:13px}
.tab:hover{color:#c9d1d9}
.tab.active{color:#58a6ff;border-bottom-color:#58a6ff}
.panel{display:none}
.panel.active{display:block}
.btn{padding:6px 14px;border:none;border-radius:6px;cursor:pointer;font-family:inherit;font-size:12px}
.btn-primary{background:#238636;color:#fff}
.btn-primary:hover{background:#2ea043}
.btn-danger{background:#da3633;color:#fff}
.btn-danger:hover{background:#f85149}
.btn-send{background:#1f6feb;color:#fff}
.btn-send:hover{background:#388bfd}
input,textarea{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;padding:8px;border-radius:6px;font-family:inherit;font-size:13px;width:100%}
input:focus,textarea:focus{outline:none;border-color:#58a6ff}
.kb-item{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px;margin-bottom:8px}
.kb-q{color:#58a6ff;font-weight:bold;font-size:13px}
.kb-a{color:#c9d1d9;font-size:12px;margin-top:4px}
.kb-actions{margin-top:6px;display:flex;gap:6px}
.pending-item{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px;margin-bottom:8px}
.pending-info{font-size:12px;color:#8b949e;margin-bottom:6px}
.pending-text{font-size:13px;color:#c9d1d9;margin-bottom:8px}
.pending-row{display:flex;gap:6px}
.pending-row input{flex:1}
.log-item{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px;margin-bottom:4px;font-size:12px}
.log-time{color:#8b949e}
.log-guid{color:#7ee787;font-size:11px}
.log-text{color:#c9d1d9;margin-top:2px}
.empty{text-align:center;color:#8b949e;padding:20px;font-size:14px}
.gid-box{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px;margin-bottom:12px;text-align:center}
.gid-box code{color:#7ee787;font-size:13px;user-select:all}
.row{display:flex;gap:6px;margin-bottom:8px}
.row input{flex:1}
</style>
</head>
<body>
<div class="container">
<h1>🤖 دستیار روبیکا</h1>

<div class="stats">
  <div class="stat-card"><div class="num" id="st-kb">0</div><div class="label">دانش</div></div>
  <div class="stat-card"><div class="num" id="st-pen">0</div><div class="label">در انتظار</div></div>
  <div class="stat-card"><div class="num" id="st-log">0</div><div class="label">لاگ امروز</div></div>
</div>

<div class="gid-box">شناسه گروه کنترل: <code id="gid-display">در حال دریافت...</code><br><small style="color:#8b949e">GUID رو از تب لاگ پیدا کن</small></div>

<div class="tabs">
  <div class="tab active" onclick="showPanel('kb')">📚 دانش</div>
  <div class="tab" onclick="showPanel('pending')">❓ سوالات</div>
  <div class="tab" onclick="showPanel('logs')">📋 لاگ</div>
</div>

<div id="panel-kb" class="panel active">
  <div class="row">
    <input id="kb-q" placeholder="سوال...">
    <input id="kb-a" placeholder="جواب...">
    <button class="btn btn-primary" onclick="addKB()">➕ افزودن</button>
  </div>
  <div id="kb-list"></div>
</div>

<div id="panel-pending" class="panel">
  <div id="pending-list"></div>
</div>

<div id="panel-logs" class="panel">
  <div id="log-list"></div>
</div>
</div>

<script>
console.log('JS loaded - v9');

function esc(t){const d=document.createElement('div');d.textContent=t||'';return d.innerHTML;}

function showPanel(name){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  document.querySelectorAll('.tab')[['kb','pending','logs'].indexOf(name)].classList.add('active');
  if(name==='kb') loadKB();
  if(name==='pending') loadPending();
  if(name==='logs') loadLogs();
}

async function loadKB(){
  try{
    const r=await fetch('/api/kb');
    const d=await r.json();
    const list=document.getElementById('kb-list');
    const entries=Object.entries(d.kb||{});
    if(entries.length===0){list.innerHTML='<div class="empty">هیچ دانشی ثبت نشده</div>';return;}
    list.innerHTML='';
    for(const [q,a] of entries){
      const item=document.createElement('div');
      item.className='kb-item';
      item.innerHTML='<div class="kb-q">❓ '+esc(q)+'</div><div class="kb-a">✅ '+esc(a)+'</div>'+
        '<div class="kb-actions"><button class="btn btn-danger" onclick="delKB('+JSON.stringify(q)+')">🗑 حذف</button></div>';
      list.appendChild(item);
    }
  }catch(e){document.getElementById('kb-list').innerHTML='<div class="empty">❌ خطا</div>';}
}

async function addKB(){
  const q=document.getElementById('kb-q').value.trim();
  const a=document.getElementById('kb-a').value.trim();
  if(!q||!a){alert('سوال و جواب رو بنویس');return;}
  try{
    const r=await fetch('/api/kb',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q,a})});
    const d=await r.json();
    if(d.ok){document.getElementById('kb-q').value='';document.getElementById('kb-a').value='';loadKB();updateStats();}
    else alert(d.error||'خطا');
  }catch(e){alert('❌ خطا');}
}

async function delKB(key){
  if(!confirm('حذف شه؟')) return;
  try{
    const r=await fetch('/api/kb',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})});
    const d=await r.json();
    if(d.ok){loadKB();updateStats();}
    else alert(d.error||'خطا');
  }catch(e){alert('❌ خطا');}
}

async function ansPen(id,text){
  text=text.trim();
  if(!text) return;
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
  try{
    const r=await fetch('/api/pending');
    const d=await r.json();
    const list=document.getElementById('pending-list');
    const entries=Object.entries(d.pending||{});
    if(entries.length===0){list.innerHTML='<div class="empty">❓ سوالی در انتظار نیست</div>';return;}
    list.innerHTML='';
    for(const [id,info] of entries){
      const item=document.createElement('div');
      item.className='pending-item';
      item.innerHTML='<div class="pending-info">#'+esc(id)+' '+esc(info.chat_guid)+' &bull; '+(info.time||'')+' '+(info.date||'')+'</div>'+
        '<div class="pending-text">💬 '+esc(info.user_text)+'</div>';
      list.appendChild(item);
      const row=document.createElement('div');
      row.className='pending-row';
      const inp=document.createElement('input');
      inp.type='text';
      inp.placeholder='جوابت رو بنویس...';
      const btn=document.createElement('button');
      btn.className='btn btn-send';
      btn.textContent='📤 ارسال';
      btn.onclick=((idd,inpRef)=>()=>ansPen(idd,inpRef.value))(id,inp);
      row.appendChild(inp);
      row.appendChild(btn);
      list.appendChild(row);
    }
  }catch(e){document.getElementById('pending-list').innerHTML='<div class="empty">❌ خطا در بارگذاری</div>';}
}

async function loadLogs(){
  try{
    const r=await fetch('/api/logs');
    const d=await r.json();
    const list=document.getElementById('log-list');
    const logs=(d.logs||[]).reverse().slice(0,100);
    if(logs.length===0){list.innerHTML='<div class="empty">📋 لاگی نیست</div>';return;}
    list.innerHTML='';
    for(const l of logs){
      const item=document.createElement('div');
      item.className='log-item';
      item.innerHTML='<span class="log-time">'+esc(l.time)+'</span> <span class="log-guid">'+esc(l.guid)+'</span>'+
        '<div class="log-text">'+esc(l.text)+'</div>';
      list.appendChild(item);
    }
  }catch(e){document.getElementById('log-list').innerHTML='<div class="empty">❌ خطا</div>';}
}

let lastPenCount=-1;
async function updateStats(){
  try{
    const r=await fetch('/api/stats');
    const d=await r.json();
    document.getElementById('st-kb').textContent=d.kb;
    document.getElementById('st-pen').textContent=d.pen;
    document.getElementById('st-log').textContent=d.today;
    /* FIX #2: اگه تب سوالات بازه و تعدادش عوض شده، خودکار تازه بشه. */
    const pp=document.getElementById('panel-pending');
    if(pp && pp.classList.contains('active') && d.pen!==lastPenCount) loadPending();
    lastPenCount=d.pen;
  }catch(e){console.log('stats error',e);}
}

async function loadGUID(){
  try{
    const r=await fetch('/api/guid');
    const d=await r.json();
    document.getElementById('gid-display').textContent=d.guid||'نامشخص';
  }catch(e){}
}

loadGUID();
loadKB();
updateStats();
setInterval(updateStats,10000);
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
    today_logs = [l for l in chat_logs if l.get("date") == today]
    return jsonify({
        "kb": len(knowledge_base),
        "pen": len(pending_replies),
        "today": len(today_logs),
    })


@app.route("/api/guid")
def api_guid():
    return jsonify({"guid": getattr(client, "guid", None)})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json() or {}
    msg = data.get("message", "").strip()
    if not msg:
        return jsonify({"error": "Empty"}), 400
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        reply = loop.run_until_complete(ask_gemini(msg))
        loop.close()
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    elif request.method == "DELETE":
        data = request.get_json() or {}
        key = data.get("key", "").strip()
        if key in knowledge_base:
            del knowledge_base[key]
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
    # ارسال جواب به کاربر اصلی
    target = original.get("chat_guid")
    if target:
        try:
            ok, msg = send_msg_sync(target, text, reply_to=original.get("message_id"))
            print(f"[FORWARD] Answer sent to {target}: ok={ok}")
        except Exception as e:
            print(f"[FORWARD ERROR] {e}")
    return jsonify({
        "ok": True,
        "message": f"✅ ذخیره شد!\n\nسوال: {original['user_text']}\nجواب: {text}\n\nAI از دفعه بعد استفاده می‌کنه."
    })


@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": chat_logs})


# ==================== قسمت ۲: ربات روبیکا ====================
def get_chat_session(chat_guid):
    if chat_guid not in chat_histories:
        chat_histories[chat_guid] = model.start_chat(history=[])
    return chat_histories[chat_guid]


def is_age_question(text):
    if not text:
        return False
    return any(kw in text for kw in ["چند سال", "سن", "سالش", "عمر", "قدیمی", "تولد", "متولد", "چندسال"])


def is_about_owner(text):
    return OWNER_NAME in text if text else False


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
        "text": user_text[:200],
        "author": author_guid,
    })
    if len(chat_logs) > 500:
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
            # ارسال تایید با تابع track
            await send_and_track(chat_guid, "✅ جوابت ذخیره شد توی دانش.")

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

    # ۲. گروه vs PV
    is_group = chat_guid.startswith("g")

    if is_group:
        # در گروه: فقط پیام‌هایی که تریگر ورد دارن یا ریپلای به ربات هستن
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
    else:
        # در PV: فقط پیام‌های دیگران (نه خودمون)
        if author_guid and author_guid != chat_guid:
            return

    print(f"\n[MSG] {chat_guid} (group={is_group}): {user_text[:80]}")

    # ۳. بررسی Knowledge Base
    kb_answer = knowledge_base.get(user_text)

    if kb_answer and not is_age_question(user_text) and not is_age_question(kb_answer):
        try:
            await asyncio.sleep(random.uniform(1, 3))
            sent = await send_and_track(chat_guid, kb_answer, reply_to=message_id)
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

        full_prompt = user_text + kb_ctx

        print(f"[AI] Sending prompt... ({len(full_prompt)} chars)")

        ai_text = await ask_gemini(full_prompt)
        print(f"[AI RAW] {ai_text[:120]}")

        # تشخیص waiting (FIX #2: الگوهای کامل‌تر)
        waiting = looks_like_waiting(ai_text)
        if waiting:
            print("[AI] Detected waiting phrase")

        # fallback: سوال درباره مالک بدون جواب KB
        if not waiting and is_about_owner(user_text) and not kb_answer:
            waiting = True
            ai_text = f"از {OWNER_NAME} می‌پرسم و بهت می‌گم ⏳"
            print("[FALLBACK] Owner question, no KB -> pending")

        if waiting:
            # ثبت pending
            # FIX #2: کلید همیشه str، تا با /api/pending و /api/answer یکی باشه
            pending_id = norm_id(str(random.randint(100000, 999999)))
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
                    sent_notif = await send_and_track(OWNER_CONTROL_GROUP, notif)
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
                remember_bot_message(sent)  # FIX #1
            except Exception as e:
                print(f"[REPLY ERROR] {e}")
        else:
            sent = await update.reply(ai_text)
            remember_bot_message(sent)  # FIX #1
            print("[AI] Direct reply sent")

    except Exception as e:
        print(f"[AI ERROR] {e}")
        try:
            await send_and_track(chat_guid, "نتونستم جواب بدم، دوباره تلاش کن 🙏", reply_to=message_id)
        except:
            pass


# ==================== MAIN ====================
def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    load_all()

    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    print("🌐 Web panel starting...")
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

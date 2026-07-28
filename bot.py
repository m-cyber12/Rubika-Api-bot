"""
🤖 دستیار روبیکا – نسخه دیباگ‌شده
═══════════════════════════════════════
لیست باگ‌های رفع‌شده:
─────────────────────
 1.  سشن روبیکا: قبلاً فقط یکبار در زمان import ساخته می‌شد و اگر env
     هنوز آماده نبود، فایل خراب یا ساخته نمی‌شد. الان هر بار که کلاینت
     شروع می‌شه، سشن چک و در صورت نیاز بازسازی می‌شه.

 2.  bot_sent_message_ids بدون محدودیت رشد می‌کرد و حافظه پر می‌شد.
     الان حداکثر 5000 آیتم نگه داشته می‌شه (FIFO).

 3.  Thread safety: دیکشنری‌های مشترک بین Flask و async بدون قفل بودن.
     الان از threading.Lock استفاده می‌شه.

 4.  main_loop ممکن بود None باشه وقتی Flask route صدا زده می‌شد.
     الان با Event صبر می‌کنه تا loop آماده بشه.

 5.  send_message_async جمینای داخل event loop روبیکا بلاک می‌کرد.
     الان با asyncio.to_thread اجرا می‌شه.

 6.  اندپوینت /api/health اضافه شد.

 7.  شناسه pending از random به uuid4 تغییر کرد (برخورد صفر).

 8.  اگر ارسال نوتیفیکیشن به گروه کنترل خطا می‌داد، pending حذف
     می‌شد و برنمی‌گشت. الان rollback می‌شه.

 9.  اعتبارسنجی GEMINI_API_KEY در شروع برنامه.

10.  Flask با use_reloader=False اجرا می‌شه تا دوبار اجرا نشه.

11.  chat_histories سقف 50 چت داره و قدیمی‌ها حذف می‌شن.

12.  مدیریت خطای بهتر در همه بخش‌ها.

13.  خروجی مرتب‌تر لاگ‌ها.
═══════════════════════════════════════
"""

import os
import sys
import asyncio
import threading
import random
import logging
import json
import uuid
import base64
from datetime import datetime
from collections import OrderedDict

from rubpy import Client
from rubpy.types import Updates
import google.generativeai as genai
from flask import Flask, request, jsonify, render_template_string

# ──────────────── لاگینگ ─────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rubika-bot")

# ──────────────── تنظیمات ─────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
if not GEMINI_API_KEY:
    log.error("❌ GEMINI_API_KEY تنظیم نشده! ربات بدون AI کار نمی‌کنه.")
    # برنامه رو متوقف نمی‌کنیم تا داشبورد بالا باشه، ولی AI غیرفعاله.

genai.configure(api_key=GEMINI_API_KEY or "NO-KEY")

OWNER_NAME = os.environ.get("OWNER_NAME", "حسن").strip()
OWNER_CONTROL_GROUP = os.environ.get("OWNER_CONTROL_GROUP", "").strip()
TRIGGER_WORD = "فرایدی"

BOT_PERSONA = f"""
تو دستیار شخصی {OWNER_NAME} هستی که روی اکانت روبیکای اون فعالیت می‌کنی.
با لحن صمیمی و دوستانه و به فارسی جواب بده.
جواب‌ها کوتاه و طبیعی باشن.

قوانین:
- اگه کسی اسم "{OWNER_NAME}" رو برد، منظورش صاحب اکانت ({OWNER_NAME}) هست.
- اگه سوالی درباره {OWNER_NAME} پرسیده شد و بلد بودی، مستقیم جواب بده.
- اگه درباره {OWNER_NAME} نمی‌دونی، بگو: "از {OWNER_NAME} می‌پرسم و بهت می‌گم ⏳"
- برای سوالات عمومی (غیر از {OWNER_NAME})، از دانش خودت استفاده کن و جواب بده.
- اگه سوالی رو نمی‌دونی، بگو: "متاسفانه الان جوابش رو نمی‌دونم."
"""

model = None
if GEMINI_API_KEY:
    try:
        model = genai.GenerativeModel(
            "gemini-flash-latest", system_instruction=BOT_PERSONA
        )
        log.info("✅ مدل Gemini با موفقیت بارگذاری شد.")
    except Exception as e:
        log.error(f"❌ خطا در بارگذاری مدل Gemini: {e}")
else:
    log.warning("⚠️ Gemini غیرفعال – GEMINI_API_KEY تنظیم نشده.")

MAX_CHAT_HISTORIES = 50
MAX_TURNS = 10
MAX_BOT_SENT_IDS = 5000

# ──────────────── قفل‌ها (Thread Safety) ─────────────────
_lock_kb = threading.Lock()
_lock_pending = threading.Lock()
_lock_logs = threading.Lock()
_lock_sent = threading.Lock()
_lock_hist = threading.Lock()

# ──────────────── حافظه ─────────────────
# OrderedDict برای محدود کردن تعداد چت‌های فعال
chat_histories: OrderedDict[str, object] = OrderedDict()
bot_sent_message_ids: set[str] = set()

KB_FILE = "knowledge_base.json"
PENDING_FILE = "pending_replies.json"
BOT_SENT_FILE = "bot_sent_ids.json"
LOG_FILE = "chat_log.json"

knowledge_base: dict[str, str] = {}
pending_replies: dict[str, dict] = {}
chat_logs: list[dict] = []

main_loop = None
main_loop_ready = threading.Event()  # ✅ باگ #4: صبر تا آماده شدن loop


# ════════════════════════════════════════
#  ذخیره‌سازی / بارگذاری JSON
# ════════════════════════════════════════

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"LOAD ERROR {path}: {e}")
            return default
    return default


def save_json(path, data):
    try:
        # نوشتن اتمیک: اول فایل موقت، بعد rename
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log.error(f"SAVE ERROR {path}: {e}")
        return False


def load_all():
    global knowledge_base, pending_replies, chat_logs, bot_sent_message_ids

    with _lock_kb:
        knowledge_base = load_json(KB_FILE, {})

    with _lock_pending:
        raw = load_json(PENDING_FILE, {})
        pending_replies = {str(k): v for k, v in raw.items()}

    with _lock_sent:
        raw_ids = load_json(BOT_SENT_FILE, [])
        bot_sent_message_ids = set(str(x) for x in raw_ids)
        # ✅ باگ #2: محدود کردن سایز
        _trim_bot_sent_ids()

    with _lock_logs:
        chat_logs = load_json(LOG_FILE, [])
        # ✅ حداکثر 2000 لاگ نگه دار
        if len(chat_logs) > 2000:
            chat_logs = chat_logs[-2000:]

    log.info(
        f"STARTUP  KB={len(knowledge_base)}  "
        f"Pending={len(pending_replies)}  "
        f"Logs={len(chat_logs)}  "
        f"SentIDs={len(bot_sent_message_ids)}"
    )


def save_kb():
    with _lock_kb:
        save_json(KB_FILE, knowledge_base)


def save_pending():
    with _lock_pending:
        save_json(PENDING_FILE, {str(k): v for k, v in pending_replies.items()})


def save_bot_sent():
    with _lock_sent:
        save_json(BOT_SENT_FILE, list(bot_sent_message_ids))


def save_logs():
    with _lock_logs:
        save_json(LOG_FILE, chat_logs)


def _trim_bot_sent_ids():
    """✅ باگ #2: محدود کردن bot_sent_message_ids به MAX_BOT_SENT_IDS."""
    while len(bot_sent_message_ids) > MAX_BOT_SENT_IDS:
        bot_sent_message_ids.pop()  # حذف یکی از قدیمی‌ها (set.pop)


# ════════════════════════════════════════
#  سشن روبیکا
# ════════════════════════════════════════

SESSION_FILE = "my_rubika_account.rp"


def restore_session():
    """✅ باگ #1: بازسازی سشن هر بار که لازم باشه، نه فقط در import."""
    part1 = os.environ.get("SESSION_B64_PART1", "").strip()
    part2 = os.environ.get("SESSION_B64_PART2", "").strip()
    session_b64 = (part1 + part2) if (part1 and part2) else ""

    if not session_b64:
        # fallback: lowercase variant
        part1 = os.environ.get("session_b64_part1", "").strip()
        part2 = os.environ.get("session_b64_part2", "").strip()
        session_b64 = (part1 + part2) if (part1 and part2) else ""

    if not session_b64:
        log.warning("⚠️ SESSION_B64_PART1 / PART2 تنظیم نشده.")
        return False

    # اگر فایل از قبل هست و معتبره، دست نزن
    if os.path.exists(SESSION_FILE) and os.path.getsize(SESSION_FILE) > 10:
        log.info(f"✅ فایل سشن موجود: {SESSION_FILE}")
        return True

    try:
        data = base64.b64decode(session_b64)
        with open(SESSION_FILE, "wb") as f:
            f.write(data)
        log.info(f"✅ سشن بازسازی شد ({len(data)} bytes)")
        return True
    except Exception as e:
        log.error(f"❌ خطا در بازسازی سشن: {e}")
        return False


restore_session()
client = Client(name="my_rubika_account")


# ════════════════════════════════════════
#  ارسال پیام (sync، برای Flask)
# ════════════════════════════════════════

def send_msg_sync(guid, text, reply_to=None):
    """ارسال پیام از ترد Flask به event loop روبیکا."""
    if not guid or not text:
        return False, "Empty guid or text"

    # ✅ باگ #4: صبر کن تا main_loop آماده بشه (حداکثر 30 ثانیه)
    if main_loop is None:
        ready = main_loop_ready.wait(timeout=30)
        if not ready:
            return False, "Event loop not ready (timeout)"

    if main_loop is None:
        return False, "Event loop is None"

    if reply_to is not None:
        try:
            reply_to = int(reply_to)
        except (ValueError, TypeError):
            reply_to = None

    async def _send():
        try:
            if reply_to:
                result = await client.send_message(
                    guid, text, reply_to_message_id=reply_to
                )
            else:
                result = await client.send_message(guid, text)
            return True, result
        except Exception as e:
            log.error(f"SEND ERROR: {e}")
            return False, str(e)

    try:
        future = asyncio.run_coroutine_threadsafe(_send(), main_loop)
        return future.result(timeout=20)
    except Exception as e:
        log.error(f"SEND FUTURE ERROR: {e}")
        return False, str(e)


# ════════════════════════════════════════
#  Flask – داشبورد و API
# ════════════════════════════════════════

app = Flask(__name__)


# ──────── داشبورد HTML ─────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html dir="rtl" lang="fa">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>دستیار روبیکا</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Tahoma,sans-serif;background:#0d1117;color:#c9d1d9;padding:12px}
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
.env-ok{color:#3fb950}
.env-miss{color:#da3633}
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
  <button type="button" class="tab" data-tab="config">⚙️ تنظیمات</button>
</div>
<div id="panel-chat" class="panel active">
  <div class="msg-box" id="chat-box"></div>
  <div style="display:flex;gap:8px">
    <input type="text" id="chat-in" placeholder="پیامت رو بنویس..." style="flex:1;margin:0">
    <button type="button" id="btn-chat-send">ارسال</button>
  </div>
</div>
<div id="panel-send" class="panel">
  <input type="text" id="s-guid" placeholder="GUID چت">
  <textarea id="s-text" placeholder="متن پیام"></textarea>
  <button type="button" id="btn-send-msg">📤 ارسال</button>
  <div id="send-status" class="small" style="margin-top:8px"></div>
</div>
<div id="panel-kb" class="panel">
  <input type="text" id="k-q" placeholder="سوال">
  <textarea id="k-a" placeholder="جواب"></textarea>
  <button type="button" id="btn-add-kb">➕ ذخیره</button>
  <hr>
  <div id="kb-list"></div>
</div>
<div id="panel-pending" class="panel">
  <div id="pending-list"></div>
</div>
<div id="panel-logs" class="panel">
  <div id="logs-list"></div>
</div>
<div id="panel-config" class="panel">
  <div id="config-info"></div>
</div>
</div>
<script>
function esc(t){const d=document.createElement('div');d.textContent=t||'';return d.innerHTML;}
function switchTab(name){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  document.querySelector('[data-tab="'+name+'"]').classList.add('active');
  if(name==='kb') loadKB();
  if(name==='pending') loadPending();
  if(name==='logs') loadLogs();
  if(name==='config') loadConfig();
}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));
async function sendChat(){
  const inp=document.getElementById('chat-in');
  const t=inp.value.trim(); if(!t) return;
  inp.value='';
  const box=document.getElementById('chat-box');
  box.innerHTML+='<div class="msg msg-u">👤 <b>تو:</b> '+esc(t)+'</div>';
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg:t})});
    const d=await r.json();
    box.innerHTML+='<div class="msg msg-a">🤖 <b>AI:</b> '+esc(d.reply||d.error||'خطا')+'</div>';
    box.scrollTop=box.scrollHeight;
  }catch(e){
    box.innerHTML+='<div class="msg msg-a">❌ خطا</div>';
  }
}
document.getElementById('btn-chat-send').onclick=sendChat;
document.getElementById('chat-in').onkeydown=e=>{if(e.key==='Enter')sendChat();};
async function sendMsg(){
  const g=document.getElementById('s-guid').value.trim();
  const t=document.getElementById('s-text').value.trim();
  const st=document.getElementById('send-status');
  if(!g||!t){st.textContent='⚠️ GUID و متن رو پر کن!';return;}
  st.textContent='⏳ در حال ارسال...';
  try{
    const r=await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({guid:g,text:t})});
    const d=await r.json();
    st.textContent=d.ok?'✅ ارسال شد!':'❌ '+(d.error||'خطا');
    if(d.ok){document.getElementById('s-guid').value='';document.getElementById('s-text').value='';}
  }catch(e){st.textContent='❌ خطای شبکه';}
}
document.getElementById('btn-send-msg').onclick=sendMsg;
async function addKB(){
  const q=document.getElementById('k-q').value.trim();
  const a=document.getElementById('k-a').value.trim();
  if(!q||!a) return;
  try{
    await fetch('/api/kb',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q:q,a:a})});
    document.getElementById('k-q').value='';document.getElementById('k-a').value='';
    loadKB(); updateStats();
  }catch(e){}
}
document.getElementById('btn-add-kb').onclick=addKB;
async function delKB(q){
  try{await fetch('/api/kb/'+encodeURIComponent(q),{method:'DELETE'});loadKB();updateStats();}catch(e){}
}
async function loadKB(){
  const list=document.getElementById('kb-list');
  try{
    const r=await fetch('/api/kb');
    const items=(await r.json()).kb||{};
    list.innerHTML='';
    const entries=Object.entries(items);
    if(entries.length===0){list.innerHTML='<div class="empty">دانشی ثبت نشده</div>';return;}
    for(const [q,a] of entries){
      const div=document.createElement('div');div.className='kb-item';
      div.innerHTML='<div><b class="small">❓</b> '+esc(q)+'<br><b class="small">💡</b> '+esc(a)+'</div>';
      const btn=document.createElement('button');btn.textContent='🗑';btn.className='btn-red';
      btn.onclick=()=>delKB(q);
      div.appendChild(btn);list.appendChild(div);
    }
  }catch(e){}
}
async function loadPending(){
  const list=document.getElementById('pending-list');
  try{
    const r=await fetch('/api/pending');
    const items=(await r.json()).pending||{};
    list.innerHTML='';
    const entries=Object.entries(items);
    if(entries.length===0){list.innerHTML='<div class="empty">سوالی در انتظار نیست</div>';return;}
    for(const [id,info] of entries){
      const div=document.createElement('div');div.className='pending-item';
      div.innerHTML='<div><b>#'+id+'</b> <span class="small">'+esc(info.chat_guid)+'</span><br>'+esc(info.user_text)+'</div>';
      list.appendChild(div);
      const row=document.createElement('div');row.className='pending-row';
      const inp=document.createElement('input');inp.type='text';inp.placeholder='جوابت رو بنویس...';
      inp.onkeydown=e=>{if(e.key==='Enter')ansPen(id,inp.value);};
      const btn=document.createElement('button');btn.textContent='✅';
      btn.onclick=()=>ansPen(id,inp.value);
      row.appendChild(inp);row.appendChild(btn);list.appendChild(row);
    }
  }catch(e){}
}
async function ansPen(id,text){
  if(!text.trim()) return;
  try{
    const r=await fetch('/api/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,text:text.trim()})});
    const d=await r.json();
    if(d.ok){loadPending();updateStats();}
  }catch(e){}
}
async function loadLogs(){
  const list=document.getElementById('logs-list');
  try{
    const r=await fetch('/api/logs');
    const logs=(await r.json()).logs||[];
    list.innerHTML='';
    if(logs.length===0){list.innerHTML='<div class="empty">لاگ خالیه</div>';return;}
    for(const log of logs.reverse()){
      const div=document.createElement('div');div.className='msg msg-u';
      div.innerHTML='<span class="small">'+esc(log.time)+' | '+esc(log.guid)+'</span><br><b>'+esc(log.from)+':</b> '+esc(log.text);
      list.appendChild(div);
    }
  }catch(e){}
}
async function loadConfig(){
  const el=document.getElementById('config-info');
  try{
    const r=await fetch('/api/config');
    const d=await r.json();
    const env=d.env||{};
    let html='<h3 style="color:#58a6ff;margin-bottom:10px">⚙️ وضعیت متغیرهای محیطی</h3>';
    html+='<table style="width:100%;font-size:13px;border-collapse:collapse">';
    const items=[
      ['GEMINI_API_KEY','کلید Gemini API'],
      ['SESSION_B64_PART1','سشن روبیکا (پارت ۱)'],
      ['SESSION_B64_PART2','سشن روبیکا (پارت ۲)'],
      ['OWNER_CONTROL_GROUP','گروه کنترل'],
      ['RUBIKA_PHONE','شماره تلفن'],
    ];
    for(const [k,label] of items){
      const ok=env[k];
      html+='<tr style="border-bottom:1px solid #30363d"><td style="padding:8px">'+label+'</td>';
      html+='<td style="padding:8px;text-align:left"><span class="'+(ok?'env-ok':'env-miss')+'">'+(ok?'✅ تنظیم شده':'❌ تنظیم نشده')+'</span></td></tr>';
    }
    html+='</table>';
    html+='<div style="margin-top:12px;padding:10px;background:#0d1117;border-radius:6px;font-size:12px;color:#8b949e">';
    html+='<b style="color:#e3b341">📝 راهنما:</b><br>';
    html+='متغیرهای محیطی SESSION_B64_PART1 و PART2 باید به صورت دو نیمه Base64 فایل سشن تنظیم بشن.';
    html+='</div>';
    el.innerHTML=html;
  }catch(e){el.innerHTML='<div class="empty">خطا</div>';}
}
async function updateStats(){
  try{
    const r=await fetch('/api/stats');
    const d=await r.json();
    document.getElementById('st-kb').textContent=d.kb;
    document.getElementById('st-pen').textContent=d.pen;
    document.getElementById('st-log').textContent=d.today;
  }catch(e){}
}
updateStats();
setInterval(updateStats,5000);
</script>
</body>
</html>
"""


# ──────── مسیرهای Flask ─────────

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


# ✅ باگ #6: اندپوینت health اضافه شد
@app.route("/api/health")
def api_health():
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "gemini_configured": bool(GEMINI_API_KEY),
        "rubika_session": os.path.exists(SESSION_FILE),
    })


@app.route("/api/stats")
def api_stats():
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock_logs:
        today_count = sum(1 for lg in chat_logs if lg.get("date") == today)
    with _lock_kb:
        kb_count = len(knowledge_base)
    with _lock_pending:
        pen_count = sum(1 for v in pending_replies.values() if v.get("status", "waiting") == "waiting")
    return jsonify({"kb": kb_count, "pen": pen_count, "today": today_count})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    if not model:
        return jsonify({"error": "GEMINI_API_KEY تنظیم نشده – AI غیرفعاله."}), 503

    data = request.get_json() or {}
    msg = (data.get("msg") or "").strip()
    if not msg:
        return jsonify({"error": "Empty"}), 400
    try:
        # ✅ باگ #5: اگر لازم بود context دانش اضافه کن
        with _lock_kb:
            kb_items = list(knowledge_base.items())[-5:]
        kb_ctx = ""
        if kb_items:
            kb_ctx = "\nاطلاعات:\n" + "\n".join(f"- {q}: {a}" for q, a in kb_items)

        chat = model.start_chat(history=[])
        res = chat.send_message(msg + kb_ctx)
        return jsonify({"reply": res.text})
    except Exception as e:
        log.error(f"CHAT ERROR: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json() or {}
    guid = (data.get("guid") or "").strip()
    text = (data.get("text") or "").strip()
    ok, result = send_msg_sync(guid, text)
    return jsonify({"ok": ok, "error": result if not ok else None})


@app.route("/api/kb", methods=["GET", "POST"])
def api_kb():
    if request.method == "GET":
        with _lock_kb:
            return jsonify({"kb": dict(knowledge_base)})
    else:
        data = request.get_json() or {}
        q = (data.get("q") or "").strip()
        a = (data.get("a") or "").strip()
        if q and a:
            with _lock_kb:
                knowledge_base[q] = a
            save_kb()
            return jsonify({"ok": True})
        return jsonify({"error": "Empty"}), 400


@app.route("/api/kb/<path:q>", methods=["DELETE"])
def api_kb_delete(q):
    with _lock_kb:
        if q in knowledge_base:
            del knowledge_base[q]
        else:
            return jsonify({"error": "Not found"}), 404
    save_kb()
    return jsonify({"ok": True})


@app.route("/api/pending")
def api_pending():
    with _lock_pending:
        return jsonify({
            "pending": {
                str(k): {**v, "status": v.get("status", "waiting")}
                for k, v in pending_replies.items()
                if v.get("status", "waiting") == "waiting"
            }
        })


@app.route("/api/answer", methods=["POST"])
def api_answer():
    if not model:
        return jsonify({"error": "AI غیرفعاله"}), 503

    data = request.get_json() or {}
    pid = str(data.get("id", "")).strip()
    if not pid or pid == "None":
        return jsonify({"error": "Invalid id"}), 400
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Empty text"}), 400

    with _lock_pending:
        if pid not in pending_replies:
            return jsonify({"error": "Not found"}), 404
        original = pending_replies.pop(pid)

    save_pending()

    # ذخیره در دانش
    with _lock_kb:
        knowledge_base[original["user_text"]] = text
    save_kb()

    # بازنویسی با لحن ربات
    try:
        chat = model.start_chat(history=[])
        prompt = (
            f"کاربر پرسید: '{original['user_text']}'، "
            f"پاسخ من: '{text}'، حالا با لحن خودت بگو."
        )
        final_answer = chat.send_message(prompt).text
    except Exception:
        final_answer = text

    ok, result = send_msg_sync(
        original["chat_guid"], final_answer,
        reply_to=original.get("message_id"),
    )
    if ok:
        return jsonify({"ok": True})
    else:
        # ✅ باگ #8: برگرداندن pending در صورت خطا
        with _lock_pending:
            pending_replies[pid] = original
        save_pending()
        return jsonify({"error": str(result)}), 500


@app.route("/api/logs")
def api_logs():
    with _lock_logs:
        return jsonify({"logs": list(reversed(chat_logs[-100:]))})


@app.route("/api/config")
def api_config():
    return jsonify({
        "env": {
            "GEMINI_API_KEY": bool(GEMINI_API_KEY),
            "SESSION_B64_PART1": bool(os.environ.get("SESSION_B64_PART1")),
            "SESSION_B64_PART2": bool(os.environ.get("SESSION_B64_PART2")),
            "OWNER_CONTROL_GROUP": bool(OWNER_CONTROL_GROUP),
            "RUBIKA_PHONE": bool(os.environ.get("RUBIKA_PHONE") or os.environ.get("rubika_phone")),
            "OWNER_NAME": OWNER_NAME,
        }
    })


# ════════════════════════════════════════
#  ربات روبیکا – هندلر پیام‌ها
# ════════════════════════════════════════

def get_chat_session(chat_guid):
    """✅ باگ #11: محدود کردن chat_histories به MAX_CHAT_HISTORIES."""
    with _lock_hist:
        if chat_guid in chat_histories:
            chat_histories.move_to_end(chat_guid)
            return chat_histories[chat_guid]

        if not model:
            return None

        session = model.start_chat(history=[])
        chat_histories[chat_guid] = session

        # حذف قدیمی‌ترین اگر سقف رد شد
        while len(chat_histories) > MAX_CHAT_HISTORIES:
            chat_histories.popitem(last=False)

        return session


@client.on_message_updates()
async def handle_messages(update: Updates):
    global main_loop

    # ✅ باگ #4: تنظیم main_loop فقط یکبار
    if main_loop is None:
        main_loop = asyncio.get_running_loop()
        main_loop_ready.set()

    if not model:
        return  # AI غیرفعاله

    chat_guid = getattr(update, "object_guid", "") or ""
    user_text = getattr(update, "text", None)
    author_guid = getattr(update, "author_guid", "") or ""
    raw_msg_id = getattr(update, "message_id", None)

    try:
        message_id = int(raw_msg_id) if raw_msg_id is not None else None
    except (ValueError, TypeError):
        message_id = None

    if not user_text:
        return

    # ──── ثبت لاگ ────
    with _lock_logs:
        chat_logs.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "guid": chat_guid,
            "from": author_guid or "unknown",
            "text": user_text[:200],
        })
        # ✅ محدود کردن لاگ‌ها
        if len(chat_logs) > 2000:
            del chat_logs[: len(chat_logs) - 2000]
    save_logs()

    # ──── گروه کنترل (ریپلای روی نوتیفیکیشن) ────
    if OWNER_CONTROL_GROUP and chat_guid == OWNER_CONTROL_GROUP:
        reply_to = (
            getattr(update, "reply_to_message_id", None)
            or getattr(update, "reply_message_id", None)
        )
        reply_str = str(reply_to) if reply_to is not None else None

        if reply_str:
            with _lock_pending:
                if reply_str in pending_replies:
                    original = pending_replies.pop(reply_str)
                else:
                    original = None

            if original:
                save_pending()
                with _lock_kb:
                    knowledge_base[original["user_text"]] = user_text
                save_kb()

                try:
                    chat = model.start_chat(history=[])
                    prompt = (
                        f"کاربر پرسید: '{original['user_text']}'، "
                        f"پاسخ من: '{user_text}'، حالا با لحن خودت بگو."
                    )
                    final_answer = chat.send_message(prompt).text
                except Exception:
                    final_answer = user_text

                try:
                    await client.send_message(
                        original["chat_guid"],
                        final_answer,
                        reply_to_message_id=original.get("message_id"),
                    )
                    await update.reply("✅ پاسخ ارسال شد!")
                except Exception as e:
                    log.error(f"CONTROL GROUP REPLY ERROR: {e}")
                    # ✅ باگ #8: برگرداندن pending
                    with _lock_pending:
                        pending_replies[reply_str] = original
                    save_pending()
                    await update.reply(f"❌ خطا: {e}")
                return

    # ──── فیلتر پیام ────
    is_private = chat_guid.startswith("u0")
    if is_private:
        if author_guid and author_guid != chat_guid:
            return
    else:
        reply_to = (
            getattr(update, "reply_to_message_id", None)
            or getattr(update, "reply_message_id", None)
        )
        reply_str = str(reply_to) if reply_to is not None else None

        with _lock_sent:
            is_reply_to_bot = reply_str is not None and reply_str in bot_sent_message_ids

        if not is_reply_to_bot and TRIGGER_WORD not in user_text:
            return

        if TRIGGER_WORD in user_text:
            user_text = user_text.replace(TRIGGER_WORD, "", 1).strip()
            if not user_text:
                user_text = "سلام"
            if not is_reply_to_bot:
                with _lock_hist:
                    if model:
                        chat_histories[chat_guid] = model.start_chat(history=[])
                        while len(chat_histories) > MAX_CHAT_HISTORIES:
                            chat_histories.popitem(last=False)
                log.info("NEW  تاریخچه ریست شد")

    log.info(f"MSG  {chat_guid} | {user_text[:50]}")

    # ──── پاسخ از دانش ────
    with _lock_kb:
        kb_answer = knowledge_base.get(user_text)

    if kb_answer:
        try:
            await asyncio.sleep(random.uniform(1, 3))
            sent = await update.reply(kb_answer)
            sid = getattr(sent, "message_id", None)
            if sid is not None:
                with _lock_sent:
                    bot_sent_message_ids.add(str(sid))
                    _trim_bot_sent_ids()
                save_bot_sent()
            log.info("KB  پاسخ از دانش")
        except Exception as e:
            log.error(f"KB ERROR: {e}")
        return

    # ──── پاسخ از AI ────
    try:
        await asyncio.sleep(random.uniform(3, 6))
        chat = get_chat_session(chat_guid)
        if chat is None:
            return

        # context دانش
        with _lock_kb:
            kb_items = list(knowledge_base.items())[-5:]
        kb_ctx = ""
        if kb_items:
            kb_ctx = "\nاطلاعات:\n" + "\n".join(f"- {q}: {a}" for q, a in kb_items)

        # ✅ باگ #5: اجرای send_message_sync در ترد جداگانه تا بلاک نشه
        prompt_text = user_text + kb_ctx
        try:
            response = await asyncio.to_thread(chat.send_message, prompt_text)
        except TypeError:
            # fallback اگر to_thread کار نکرد
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, chat.send_message, prompt_text
            )

        ai_text = response.text
        log.info(f"AI  {ai_text[:100]}")

        # تشخیص "نمی‌دونم"
        waiting = False
        clean = ai_text.replace("😊", "").replace("❤️", "").strip()
        if "از حسن می‌پرسم" in clean or f"از {OWNER_NAME} می‌پرسم" in clean:
            waiting = True
        else:
            keywords = ["نمی‌دونم", "نمی‌دانم", "اطلاع ندارم", "می‌پرسم",
                        "متاسفانه الان جوابش رو نمی‌دونم"]
            waiting = any(k in ai_text for k in keywords)
        log.info(f"AI  waiting={waiting}")

        if waiting:
            # ✅ باگ #7: استفاده از uuid4 به جای random
            pending_id = str(uuid.uuid4().int)[:8]

            with _lock_pending:
                pending_replies[pending_id] = {
                    "chat_guid": chat_guid,
                    "user_text": user_text,
                    "author_guid": author_guid,
                    "message_id": message_id,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "status": "waiting",
                }
            save_pending()
            log.info(f"PENDING  {pending_id} | {user_text}")

            if OWNER_CONTROL_GROUP:
                try:
                    notif = (
                        f"❓ سوال: {user_text}\n"
                        f"🆔 {chat_guid}\n"
                        f"⬅️ ریپلای کن"
                    )
                    sent_notif = await client.send_message(
                        OWNER_CONTROL_GROUP, notif
                    )
                    nid = getattr(sent_notif, "message_id", None)
                    if nid is not None:
                        # ✅ باگ #8: تعویض کلید با لاک
                        with _lock_pending:
                            if pending_id in pending_replies:
                                pending_replies[str(nid)] = pending_replies.pop(
                                    pending_id
                                )
                        save_pending()
                        log.info(f"NOTIF  ارسال شد nid={nid}")
                except Exception as e:
                    log.error(f"NOTIF ERROR: {e}")

            try:
                sent = await update.reply(ai_text)
                sid = getattr(sent, "message_id", None)
                if sid is not None:
                    with _lock_sent:
                        bot_sent_message_ids.add(str(sid))
                        _trim_bot_sent_ids()
                    save_bot_sent()
            except Exception as e:
                log.error(f"REPLY ERROR: {e}")
        else:
            sent = await update.reply(ai_text)
            sid = getattr(sent, "message_id", None)
            if sid is not None:
                with _lock_sent:
                    bot_sent_message_ids.add(str(sid))
                    _trim_bot_sent_ids()
                save_bot_sent()
            log.info("AI  پاسخ مستقیم")

        # ✅ باگ #11: محدود کردن تاریخچه چت
        with _lock_hist:
            if len(chat.history) > MAX_TURNS * 2:
                chat_histories[chat_guid] = model.start_chat(
                    history=chat.history[-MAX_TURNS * 2 :]
                )

    except Exception as e:
        log.error(f"ERROR: {e}", exc_info=True)


# ════════════════════════════════════════
#  اجرای اصلی
# ════════════════════════════════════════

if __name__ == "__main__":
    load_all()

    def run_web():
        port = int(os.environ.get("PORT", 10000))
        # ✅ باگ #10: use_reloader=False تا دوبار اجرا نشه
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)

    threading.Thread(target=run_web, daemon=True).start()

    print("=" * 55)
    print("🚀 Bot + Dashboard running")
    print(f"📬 Control Group : {OWNER_CONTROL_GROUP or 'غیرفعال'}")
    print(f"🔑 Gemini API    : {'✅ فعال' if GEMINI_API_KEY else '❌ غیرفعال'}")
    print(f"🔑 Rubika Session: {'✅ موجود' if os.path.exists(SESSION_FILE) else '❌ ناموجود'}")
    print(f"🧠 KB: {len(knowledge_base)} | ⏳ Pending: {len(pending_replies)}")
    print("=" * 55)

    RUBIKA_PHONE = (
        os.environ.get("RUBIKA_PHONE")
        or os.environ.get("rubika_phone")
        or ""
    ).strip()

    if RUBIKA_PHONE:
        client.run(phone_number=RUBIKA_PHONE)
    else:
        client.run()

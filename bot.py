"""
🤖 دستیار روبیکا – نسخه دیباگ‌شده + سیستم هوشمند چرخش کلیدهای API
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
_raw_keys = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
CURRENT_KEY_INDEX = 0
_lock_api_key = threading.Lock()

if not GEMINI_API_KEYS:
    log.error("❌ GEMINI_API_KEY تنظیم نشده! ربات بدون AI کار نمی‌کنه.")
    # برنامه رو متوقف نمی‌کنیم تا داشبورد بالا باشه، ولی AI غیرفعاله.

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

def configure_gemini():
    global model
    if not GEMINI_API_KEYS:
        return None
    current_key = GEMINI_API_KEYS[CURRENT_KEY_INDEX]
    genai.configure(api_key=current_key)
    try:
        model = genai.GenerativeModel(
            "gemini-flash-latest", system_instruction=BOT_PERSONA
        )
        log.info(f"✅ مدل Gemini با کلید [{current_key[:6]}...] بارگذاری شد.")
        return model
    except Exception as e:
        log.error(f"❌ خطا در بارگذاری مدل Gemini: {e}")
        return None

model = configure_gemini()

def switch_api_key():
    global CURRENT_KEY_INDEX
    with _lock_api_key:
        if len(GEMINI_API_KEYS) <= 1:
            return False
        CURRENT_KEY_INDEX = (CURRENT_KEY_INDEX + 1) % len(GEMINI_API_KEYS)
        log.warning(f"🔄 تغییر کلید API به دلیل لیمیت شدن. کلید جدید ایندکس: {CURRENT_KEY_INDEX}")
        configure_gemini()
        
        # آپدیت کردن سشن‌های چت باز با مدل جدید
        with _lock_hist:
            for guid, chat_obj in list(chat_histories.items()):
                if model:
                    try:
                        chat_histories[guid] = model.start_chat(history=chat_obj.history)
                    except Exception as e:
                        log.error(f"خطا در بروزرسانی سشن چت {guid}: {e}")
        return True

def execute_with_rotation(func, *args, **kwargs):
    max_tries = max(1, len(GEMINI_API_KEYS))
    for attempt in range(max_tries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            err_str = str(e).lower()
            if "429" in err_str or "quota" in err_str or "exhausted" in err_str or "rate" in err_str:
                log.warning(f"⚠️ ارور لیمیت جمینای. تلاش {attempt+1}/{max_tries}")
                if not switch_api_key():
                    raise e
            else:
                raise e
    raise Exception("تمامی کلیدهای API مسدود یا لیمیت شده‌اند.")


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


def _extract_msg_id(obj) -> str | None:
    """
    استخراج message_id از آبجکت rubpy (Update یا dict).
    rubpy گاهی message_id رو به شکل‌های مختلف برمی‌گردونه.
    """
    if obj is None:
        return None
    # اگر آبجکت rubpy Update هست
    mid = getattr(obj, "message_id", None)
    if mid is not None:
        return str(mid)
    # اگر dict هست
    if isinstance(obj, dict):
        mid = obj.get("message_id") or obj.get("data", {}).get("message_id")
        if mid is not None:
            return str(mid)
    return None


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

    # rubpy expects reply_to_message_id as str, NOT int
    if reply_to is not None:
        reply_to = str(reply_to)

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
<title>دستیار روبیکا | پنل مدرن</title>
<link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;500;700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg-primary:#0a0a0f;
  --bg-secondary:#12121a;
  --bg-card:rgba(18,18,26,0.8);
  --bg-glass:rgba(255,255,255,0.03);
  --accent-1:#00d4ff;
  --accent-2:#7c3aed;
  --accent-3:#f472b6;
  --accent-4:#34d399;
  --text-primary:#f0f0f5;
  --text-secondary:#8888a0;
  --border:rgba(255,255,255,0.06);
  --glow-1:0 0 30px rgba(0,212,255,0.3);
  --glow-2:0 0 30px rgba(124,58,237,0.3);
  --glow-3:0 0 30px rgba(244,114,182,0.3);
  --radius:16px;
  --radius-sm:10px;
  --transition:all 0.3s cubic-bezier(0.4,0,0.2,1);
}
body{
  font-family:'Vazirmatn',sans-serif;
  background:var(--bg-primary);
  color:var(--text-primary);
  min-height:100vh;
  overflow-x:hidden;
}
body::before{
  content:'';position:fixed;top:0;left:0;right:0;bottom:0;
  background:
    radial-gradient(ellipse 800px 600px at 20% 20%, rgba(0,212,255,0.08) 0%, transparent 60%),
    radial-gradient(ellipse 600px 800px at 80% 80%, rgba(124,58,237,0.08) 0%, transparent 60%),
    radial-gradient(ellipse 500px 500px at 50% 50%, rgba(244,114,182,0.05) 0%, transparent 60%);
  pointer-events:none;z-index:0;
}
.app{display:flex;min-height:100vh;position:relative;z-index:1}

/* ───── Sidebar ───── */
.sidebar{
  width:260px;min-height:100vh;background:var(--bg-secondary);
  border-left:1px solid var(--border);padding:24px 16px;
  display:flex;flex-direction:column;position:fixed;right:0;top:0;bottom:0;
  backdrop-filter:blur(20px);z-index:100;
}
.sidebar-logo{
  text-align:center;margin-bottom:32px;padding:20px;
  background:linear-gradient(135deg,rgba(0,212,255,0.1),rgba(124,58,237,0.1));
  border-radius:var(--radius);border:1px solid var(--border);
}
.sidebar-logo h1{
  font-size:20px;font-weight:900;
  background:linear-gradient(135deg,var(--accent-1),var(--accent-2));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text;
}
.sidebar-logo p{font-size:11px;color:var(--text-secondary);margin-top:4px}
.nav{flex:1;display:flex;flex-direction:column;gap:4px}
.nav-item{
  display:flex;align-items:center;gap:12px;padding:12px 16px;
  border-radius:var(--radius-sm);cursor:pointer;transition:var(--transition);
  color:var(--text-secondary);font-size:14px;font-weight:500;
  border:1px solid transparent;position:relative;overflow:hidden;
}
.nav-item:hover{background:var(--bg-glass);color:var(--text-primary);border-color:var(--border)}
.nav-item.active{
  background:linear-gradient(135deg,rgba(0,212,255,0.15),rgba(124,58,237,0.15));
  color:var(--accent-1);border-color:rgba(0,212,255,0.2);
  box-shadow:var(--glow-1);
}
.nav-item.active::before{
  content:'';position:absolute;right:0;top:50%;transform:translateY(-50%);
  width:3px;height:60%;background:linear-gradient(180deg,var(--accent-1),var(--accent-2));
  border-radius:0 4px 4px 0;
}
.nav-item i{font-size:18px;width:24px;text-align:center}
.sidebar-footer{
  padding:16px;background:var(--bg-glass);border-radius:var(--radius-sm);
  border:1px solid var(--border);margin-top:auto;
}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--accent-4);display:inline-block;margin-left:8px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.5}}

/* ───── Main Content ───── */
.main{flex:1;padding:24px;margin-right:260px}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}
.header h2{font-size:24px;font-weight:700}
.header-actions{display:flex;gap:12px}
.header-btn{
  padding:10px 20px;border-radius:var(--radius-sm);border:1px solid var(--border);
  background:var(--bg-glass);color:var(--text-primary);cursor:pointer;
  font-family:inherit;font-size:13px;transition:var(--transition);
  backdrop-filter:blur(10px);
}
.header-btn:hover{border-color:var(--accent-1);box-shadow:var(--glow-1)}

/* ───── Stats Cards ───── */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:24px}
.stat-card{
  background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
  padding:20px;position:relative;overflow:hidden;transition:var(--transition);
  backdrop-filter:blur(10px);
}
.stat-card:hover{transform:translateY(-4px);border-color:var(--accent-1);box-shadow:var(--glow-1)}
.stat-card::before{
  content:'';position:absolute;top:0;right:0;width:100%;height:3px;
  background:linear-gradient(90deg,var(--accent-1),var(--accent-2));
}
.stat-card:nth-child(2)::before{background:linear-gradient(90deg,var(--accent-2),var(--accent-3))}
.stat-card:nth-child(3)::before{background:linear-gradient(90deg,var(--accent-3),var(--accent-4))}
.stat-card:nth-child(4)::before{background:linear-gradient(90deg,var(--accent-4),var(--accent-1))}
.stat-icon{
  width:48px;height:48px;border-radius:12px;display:flex;align-items:center;justify-content:center;
  font-size:20px;margin-bottom:12px;
}
.stat-card:nth-child(1) .stat-icon{background:rgba(0,212,255,0.1);color:var(--accent-1)}
.stat-card:nth-child(2) .stat-icon{background:rgba(124,58,237,0.1);color:var(--accent-2)}
.stat-card:nth-child(3) .stat-icon{background:rgba(244,114,182,0.1);color:var(--accent-3)}
.stat-card:nth-child(4) .stat-icon{background:rgba(52,211,153,0.1);color:var(--accent-4)}
.stat-value{font-size:32px;font-weight:900;margin-bottom:4px}
.stat-label{font-size:13px;color:var(--text-secondary)}

/* ───── Panels ───── */
.panel{display:none;animation:fadeIn 0.4s ease}
.panel.active{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.card{
  background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
  padding:20px;margin-bottom:16px;backdrop-filter:blur(10px);transition:var(--transition);
}
.card:hover{border-color:rgba(255,255,255,0.1)}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.card-title{font-size:16px;font-weight:700;display:flex;align-items:center;gap:8px}
.card-title i{color:var(--accent-1)}

/* ───── Chat Box ───── */
.chat-container{display:flex;flex-direction:column;height:500px}
.chat-messages{
  flex:1;overflow-y:auto;padding:16px;border-radius:var(--radius-sm);
  background:var(--bg-primary);border:1px solid var(--border);margin-bottom:12px;
}
.chat-messages::-webkit-scrollbar{width:6px}
.chat-messages::-webkit-scrollbar-track{background:transparent}
.chat-messages::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
.msg{
  padding:12px 16px;border-radius:12px;margin-bottom:8px;font-size:14px;
  line-height:1.7;max-width:85%;animation:msgIn 0.3s ease;
}
@keyframes msgIn{from{opacity:0;transform:scale(0.95)}to{opacity:1;transform:scale(1)}}
.msg-user{
  background:linear-gradient(135deg,rgba(0,212,255,0.15),rgba(0,212,255,0.05));
  border:1px solid rgba(0,212,255,0.2);margin-left:auto;border-bottom-right-radius:4px;
}
.msg-ai{
  background:linear-gradient(135deg,rgba(124,58,237,0.15),rgba(124,58,237,0.05));
  border:1px solid rgba(124,58,237,0.2);margin-right:auto;border-bottom-left-radius:4px;
}
.msg-meta{font-size:11px;color:var(--text-secondary);margin-top:4px;display:flex;align-items:center;gap:6px}
.msg-meta i{font-size:10px}
.chat-input-wrap{display:flex;gap:10px}
.chat-input{
  flex:1;padding:14px 18px;border-radius:var(--radius-sm);border:1px solid var(--border);
  background:var(--bg-primary);color:var(--text-primary);font-family:inherit;font-size:14px;
  transition:var(--transition);outline:none;
}
.chat-input:focus{border-color:var(--accent-1);box-shadow:var(--glow-1)}
.chat-input::placeholder{color:var(--text-secondary)}
.send-btn{
  padding:14px 24px;border-radius:var(--radius-sm);border:none;
  background:linear-gradient(135deg,var(--accent-1),var(--accent-2));
  color:#fff;font-family:inherit;font-size:14px;font-weight:600;
  cursor:pointer;transition:var(--transition);display:flex;align-items:center;gap:8px;
}
.send-btn:hover{transform:scale(1.02);box-shadow:var(--glow-1)}

/* ───── Forms ───── */
.form-group{margin-bottom:16px}
.form-label{display:block;font-size:13px;color:var(--text-secondary);margin-bottom:8px;font-weight:500}
.form-input,.form-textarea{
  width:100%;padding:12px 16px;border-radius:var(--radius-sm);border:1px solid var(--border);
  background:var(--bg-primary);color:var(--text-primary);font-family:inherit;font-size:14px;
  transition:var(--transition);outline:none;
}
.form-input:focus,.form-textarea:focus{border-color:var(--accent-1);box-shadow:var(--glow-1)}
.form-textarea{min-height:100px;resize:vertical}
.form-input::placeholder,.form-textarea::placeholder{color:var(--text-secondary)}
.btn{
  padding:12px 24px;border-radius:var(--radius-sm);border:none;
  font-family:inherit;font-size:14px;font-weight:600;cursor:pointer;
  transition:var(--transition);display:inline-flex;align-items:center;gap:8px;
}
.btn-primary{background:linear-gradient(135deg,var(--accent-1),var(--accent-2));color:#fff}
.btn-primary:hover{transform:translateY(-2px);box-shadow:var(--glow-1)}
.btn-danger{background:linear-gradient(135deg,#ef4444,#dc2626);color:#fff}
.btn-danger:hover{transform:translateY(-2px);box-shadow:0 0 20px rgba(239,68,68,0.3)}
.btn-success{background:linear-gradient(135deg,var(--accent-4),#059669);color:#fff}
.btn-success:hover{transform:translateY(-2px);box-shadow:0 0 20px rgba(52,211,153,0.3)}
.btn-sm{padding:8px 16px;font-size:12px}

/* ───── Items List ───── */
.item-list{display:flex;flex-direction:column;gap:10px}
.item-card{
  background:var(--bg-primary);border:1px solid var(--border);border-radius:var(--radius-sm);
  padding:16px;display:flex;gap:12px;align-items:flex-start;transition:var(--transition);
}
.item-card:hover{border-color:rgba(255,255,255,0.1);transform:translateX(-4px)}
.item-content{flex:1}
.item-content b{font-size:13px;color:var(--accent-1)}
.item-content p{font-size:13px;color:var(--text-secondary);margin-top:4px;line-height:1.6}
.item-actions{display:flex;gap:6px}

/* ───── Pending Items ───── */
.pending-card{
  background:var(--bg-primary);border:1px solid rgba(244,114,182,0.2);
  border-radius:var(--radius-sm);padding:16px;margin-bottom:12px;
  transition:var(--transition);
}
.pending-card:hover{border-color:var(--accent-3);box-shadow:var(--glow-3)}
.pending-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.pending-id{
  font-size:12px;font-weight:700;padding:4px 10px;border-radius:20px;
  background:rgba(244,114,182,0.15);color:var(--accent-3);
}
.pending-time{font-size:11px;color:var(--text-secondary)}
.pending-text{font-size:14px;line-height:1.7;margin-bottom:12px}
.pending-input-wrap{display:flex;gap:8px}
.pending-input{
  flex:1;padding:10px 14px;border-radius:var(--radius-sm);border:1px solid var(--border);
  background:var(--bg-primary);color:var(--text-primary);font-family:inherit;font-size:13px;
  outline:none;transition:var(--transition);
}
.pending-input:focus{border-color:var(--accent-3);box-shadow:var(--glow-3)}

/* ───── Logs ───── */
.log-item{
  padding:12px 16px;border-radius:var(--radius-sm);background:var(--bg-primary);
  border:1px solid var(--border);margin-bottom:8px;font-size:13px;
  transition:var(--transition);
}
.log-item:hover{border-color:rgba(255,255,255,0.1)}
.log-time{color:var(--accent-1);font-weight:600;font-size:12px}
.log-guid{color:var(--text-secondary);font-size:11px;margin:0 8px}
.log-from{color:var(--accent-2);font-weight:600}
.log-text{color:var(--text-primary);margin-top:4px;line-height:1.6}

/* ───── Config Table ───── */
.config-table{width:100%;border-collapse:separate;border-spacing:0}
.config-table th{
  padding:12px 16px;text-align:right;font-size:12px;color:var(--text-secondary);
  border-bottom:1px solid var(--border);font-weight:600;text-transform:uppercase;
  letter-spacing:0.5px;
}
.config-table td{
  padding:12px 16px;border-bottom:1px solid var(--border);font-size:14px;
}
.config-table tr:hover td{background:var(--bg-glass)}
.config-badge{
  padding:6px 12px;border-radius:20px;font-size:12px;font-weight:600;
  display:inline-flex;align-items:center;gap:6px;
}
.config-badge.ok{background:rgba(52,211,153,0.15);color:var(--accent-4)}
.config-badge.error{background:rgba(239,68,68,0.15);color:#ef4444}
.config-badge i{font-size:10px}

/* ───── Empty State ───── */
.empty-state{
  text-align:center;padding:40px 20px;color:var(--text-secondary);
}
.empty-state i{font-size:48px;margin-bottom:16px;opacity:0.3}
.empty-state p{font-size:14px}

/* ───── Guide Box ───── */
.guide-box{
  background:linear-gradient(135deg,rgba(0,212,255,0.05),rgba(124,58,237,0.05));
  border:1px solid rgba(0,212,255,0.2);border-radius:var(--radius-sm);
  padding:16px;margin-top:16px;
}
.guide-box h4{color:var(--accent-1);font-size:14px;margin-bottom:8px;display:flex;align-items:center;gap:8px}
.guide-box p{font-size:13px;color:var(--text-secondary);line-height:1.8}

/* ───── Responsive ───── */
@media(max-width:768px){
  .sidebar{display:none}
  .main{margin-right:0}
  .stats-grid{grid-template-columns:1fr 1fr}
  .header{flex-direction:column;gap:12px;align-items:flex-start}
}

/* ───── Scrollbar ───── */
::-webkit-scrollbar{width:8px}
::-webkit-scrollbar-track{background:var(--bg-primary)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,0.1)}

/* ───── Loading Spinner ───── */
.spinner{
  width:20px;height:20px;border:2px solid var(--border);border-top-color:var(--accent-1);
  border-radius:50%;animation:spin 0.8s linear infinite;display:inline-block;
}
@keyframes spin{to{transform:rotate(360deg)}}

/* ───── Toast Notification ───── */
.toast{
  position:fixed;bottom:24px;left:24px;padding:14px 20px;border-radius:var(--radius-sm);
  background:var(--bg-secondary);border:1px solid var(--accent-4);color:var(--accent-4);
  font-size:13px;font-weight:500;z-index:1000;animation:toastIn 0.3s ease;
  box-shadow:0 10px 40px rgba(0,0,0,0.3);
}
.toast.error{border-color:#ef4444;color:#ef4444}
@keyframes toastIn{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
</style>
</head>
<body>
<div class="app">
  <!-- ───── Sidebar ───── -->
  <aside class="sidebar">
    <div class="sidebar-logo">
      <h1><i class="fas fa-robot"></i> روبیکا</h1>
      <p>پنل مدیریت هوشمند</p>
    </div>
    <nav class="nav">
      <div class="nav-item active" data-tab="dashboard"><i class="fas fa-th-large"></i>داشبورد</div>
      <div class="nav-item" data-tab="chat"><i class="fas fa-comments"></i>چت با AI</div>
      <div class="nav-item" data-tab="send"><i class="fas fa-paper-plane"></i>ارسال پیام</div>
      <div class="nav-item" data-tab="kb"><i class="fas fa-brain"></i>دانش</div>
      <div class="nav-item" data-tab="pending"><i class="fas fa-clock"></i>سوالات</div>
      <div class="nav-item" data-tab="logs"><i class="fas fa-list-alt"></i>لاگ</div>
      <div class="nav-item" data-tab="config"><i class="fas fa-cog"></i>تنظیمات</div>
    </nav>
    <div class="sidebar-footer">
      <span class="status-dot"></span>
      <span style="font-size:12px;color:var(--text-secondary)">سیستم فعال</span>
    </div>
  </aside>

  <!-- ───── Main Content ───── -->
  <main class="main">
    <div class="header">
      <h2 id="page-title">داشبورد</h2>
      <div class="header-actions">
        <button class="header-btn" onclick="updateStats()"><i class="fas fa-sync-alt"></i> بروزرسانی</button>
        <button class="header-btn" onclick="window.open('/api/health','_blank')"><i class="fas fa-heartbeat"></i> سلامت</button>
      </div>
    </div>

    <!-- ───── Dashboard Panel ───── -->
    <div id="panel-dashboard" class="panel active">
      <div class="stats-grid">
        <div class="stat-card">
          <div class="stat-icon"><i class="fas fa-brain"></i></div>
          <div class="stat-value" id="st-kb">0</div>
          <div class="stat-label">دانش ثبت شده</div>
        </div>
        <div class="stat-card">
          <div class="stat-icon"><i class="fas fa-hourglass-half"></i></div>
          <div class="stat-value" id="st-pen">0</div>
          <div class="stat-label">در انتظار پاسخ</div>
        </div>
        <div class="stat-card">
          <div class="stat-icon"><i class="fas fa-calendar-day"></i></div>
          <div class="stat-value" id="st-log">0</div>
          <div class="stat-label">لاگ امروز</div>
        </div>
        <div class="stat-card">
          <div class="stat-icon"><i class="fas fa-key"></i></div>
          <div class="stat-value" id="st-keys">-</div>
          <div class="stat-label">کلیدهای API</div>
        </div>
      </div>
      <div class="card">
        <div class="card-header">
          <div class="card-title"><i class="fas fa-info-circle"></i> راهنمای سریع</div>
        </div>
        <div class="guide-box">
          <h4><i class="fas fa-lightbulb"></i> نکات مهم</h4>
          <p>
            • برای فعال‌سازی چرخش کلیدها، کلیدها رو با کاما در GEMINI_API_KEY جدا کنید<br>
            • از تب دانش برای مدیریت سوالات و جواب‌های متداول استفاده کنید<br>
            • سوالات pending در گروه کنترل نمایش داده میشن<br>
            • لاگ‌ها هر ۵ ثانیه بروزرسانی میشن
          </p>
        </div>
      </div>
    </div>

    <!-- ───── Chat Panel ───── -->
    <div id="panel-chat" class="panel">
      <div class="card">
        <div class="chat-container">
          <div class="chat-messages" id="chat-box"></div>
          <div class="chat-input-wrap">
            <input type="text" class="chat-input" id="chat-in" placeholder="پیامت رو بنویس...">
            <button class="send-btn" id="btn-chat-send"><i class="fas fa-paper-plane"></i> ارسال</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ───── Send Panel ───── -->
    <div id="panel-send" class="panel">
      <div class="card">
        <div class="card-header">
          <div class="card-title"><i class="fas fa-paper-plane"></i> ارسال پیام</div>
        </div>
        <div class="form-group">
          <label class="form-label">GUID چت</label>
          <input type="text" class="form-input" id="s-guid" placeholder="GUID چت مورد نظر">
        </div>
        <div class="form-group">
          <label class="form-label">متن پیام</label>
          <textarea class="form-textarea" id="s-text" placeholder="متن پیام رو بنویس..."></textarea>
        </div>
        <button class="btn btn-primary" id="btn-send-msg"><i class="fas fa-paper-plane"></i> ارسال</button>
        <div id="send-status" style="margin-top:12px;font-size:13px"></div>
      </div>
    </div>

    <!-- ───── KB Panel ───── -->
    <div id="panel-kb" class="panel">
      <div class="card">
        <div class="card-header">
          <div class="card-title"><i class="fas fa-plus-circle"></i> افزودن دانش جدید</div>
        </div>
        <div class="form-group">
          <label class="form-label">سوال</label>
          <input type="text" class="form-input" id="k-q" placeholder="سوال رو بنویس...">
        </div>
        <div class="form-group">
          <label class="form-label">جواب</label>
          <textarea class="form-textarea" id="k-a" placeholder="جواب رو بنویس..."></textarea>
        </div>
        <button class="btn btn-success" id="btn-add-kb"><i class="fas fa-save"></i> ذخیره</button>
      </div>
      <div class="card">
        <div class="card-header">
          <div class="card-title"><i class="fas fa-database"></i> دانش ثبت شده</div>
        </div>
        <div class="item-list" id="kb-list"></div>
      </div>
    </div>

    <!-- ───── Pending Panel ───── -->
    <div id="panel-pending" class="panel">
      <div class="card">
        <div class="card-header">
          <div class="card-title"><i class="fas fa-hourglass-half"></i> سوالات در انتظار</div>
        </div>
        <div id="pending-list"></div>
      </div>
    </div>

    <!-- ───── Logs Panel ───── -->
    <div id="panel-logs" class="panel">
      <div class="card">
        <div class="card-header">
          <div class="card-title"><i class="fas fa-list-alt"></i> آخرین لاگ‌ها</div>
        </div>
        <div id="logs-list"></div>
      </div>
    </div>

    <!-- ───── Config Panel ───── -->
    <div id="panel-config" class="panel">
      <div class="card">
        <div class="card-header">
          <div class="card-title"><i class="fas fa-cog"></i> وضعیت سیستم</div>
        </div>
        <div id="config-info"></div>
      </div>
    </div>
  </main>
</div>

<script>
// ───── Utilities ─────
function esc(t){const d=document.createElement('div');d.textContent=t||'';return d.innerHTML;}
function showToast(msg,isError){
  const t=document.createElement('div');t.className='toast'+(isError?' error':'');t.textContent=msg;
  document.body.appendChild(t);setTimeout(()=>t.remove(),3000);
}

// ───── Tab Switching ─────
const titles={dashboard:'داشبورد',chat:'چت با AI',send:'ارسال پیام',kb:'مدیریت دانش',pending:'سوالات',logs:'لاگ',config:'تنظیمات'};
function switchTab(name){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  document.querySelector('[data-tab="'+name+'"]').classList.add('active');
  document.getElementById('page-title').textContent=titles[name]||name;
  if(name==='kb') loadKB();
  if(name==='pending') loadPending();
  if(name==='logs') loadLogs();
  if(name==='config') loadConfig();
}
document.querySelectorAll('.nav-item').forEach(n=>n.onclick=()=>switchTab(n.dataset.tab));

// ───── Chat ─────
async function sendChat(){
  const inp=document.getElementById('chat-in');
  const t=inp.value.trim();if(!t)return;
  inp.value='';
  const box=document.getElementById('chat-box');
  box.innerHTML+='<div class="msg msg-user"><div>'+esc(t)+'</div><div class="msg-meta"><i class="fas fa-user"></i> تو</div></div>';
  box.scrollTop=box.scrollHeight;
  const loading=document.createElement('div');loading.className='msg msg-ai';loading.id='loading-msg';
  loading.innerHTML='<div class="spinner"></div><div class="msg-meta"><i class="fas fa-robot"></i> در حال پردازش...</div>';
  box.appendChild(loading);box.scrollTop=box.scrollHeight;
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg:t})});
    const d=await r.json();
    document.getElementById('loading-msg')?.remove();
    box.innerHTML+='<div class="msg msg-ai"><div>'+esc(d.reply||d.error||'خطا')+'</div><div class="msg-meta"><i class="fas fa-robot"></i> AI</div></div>';
    box.scrollTop=box.scrollHeight;
  }catch(e){
    document.getElementById('loading-msg')?.remove();
    box.innerHTML+='<div class="msg msg-ai"><div>خطای شبکه</div><div class="msg-meta"><i class="fas fa-robot"></i> AI</div></div>';
  }
}
document.getElementById('btn-chat-send').onclick=sendChat;
document.getElementById('chat-in').onkeydown=e=>{if(e.key==='Enter')sendChat();};

// ───── Send Message ─────
async function sendMsg(){
  const g=document.getElementById('s-guid').value.trim();
  const t=document.getElementById('s-text').value.trim();
  const st=document.getElementById('send-status');
  if(!g||!t){st.innerHTML='<span style="color:#ef4444">GUID و متن رو پر کن!</span>';return;}
  st.innerHTML='<span class="spinner"></span> در حال ارسال...';
  try{
    const r=await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({guid:g,text:t})});
    const d=await r.json();
    if(d.ok){
      st.innerHTML='<span style="color:var(--accent-4)">ارسال شد!</span>';
      document.getElementById('s-guid').value='';document.getElementById('s-text').value='';
      showToast('پیام با موفقیت ارسال شد');
    }else{
      st.innerHTML='<span style="color:#ef4444">'+esc(d.error||'خطا')+'</span>';
    }
  }catch(e){st.innerHTML='<span style="color:#ef4444">خطای شبکه</span>';}
}
document.getElementById('btn-send-msg').onclick=sendMsg;

// ───── Knowledge Base ─────
async function addKB(){
  const q=document.getElementById('k-q').value.trim();
  const a=document.getElementById('k-a').value.trim();
  if(!q||!a)return;
  try{
    await fetch('/api/kb',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q:q,a:a})});
    document.getElementById('k-q').value='';document.getElementById('k-a').value='';
    loadKB();updateStats();showToast('دانش ذخیره شد');
  }catch(e){}
}
document.getElementById('btn-add-kb').onclick=addKB;
async function delKB(q){
  try{await fetch('/api/kb/'+encodeURIComponent(q),{method:'DELETE'});loadKB();updateStats();showToast('حذف شد');}catch(e){}
}
async function loadKB(){
  const list=document.getElementById('kb-list');
  try{
    const r=await fetch('/api/kb');const items=(await r.json()).kb||{};list.innerHTML='';
    const entries=Object.entries(items);
    if(entries.length===0){list.innerHTML='<div class="empty-state"><i class="fas fa-database"></i><p>دانشی ثبت نشده</p></div>';return;}
    for(const [q,a] of entries){
      const div=document.createElement('div');div.className='item-card';
      div.innerHTML='<div class="item-content"><b>'+esc(q)+'</b><p>'+esc(a)+'</p></div>';
      const btn=document.createElement('button');btn.className='btn btn-danger btn-sm';
      btn.innerHTML='<i class="fas fa-trash"></i>';btn.onclick=()=>delKB(q);
      div.appendChild(btn);list.appendChild(div);
    }
  }catch(e){}
}

// ───── Pending ─────
async function loadPending(){
  const list=document.getElementById('pending-list');
  try{
    const r=await fetch('/api/pending');const items=(await r.json()).pending||{};list.innerHTML='';
    const entries=Object.entries(items);
    if(entries.length===0){list.innerHTML='<div class="empty-state"><i class="fas fa-check-circle"></i><p>سوالی در انتظار نیست</p></div>';return;}
    for(const [id,info] of entries){
      const div=document.createElement('div');div.className='pending-card';
      div.innerHTML='<div class="pending-header"><span class="pending-id">#'+id+'</span><span class="pending-time">'+esc(info.time||'')+'</span></div><div class="pending-text">'+esc(info.user_text)+'</div>';
      const row=document.createElement('div');row.className='pending-input-wrap';
      const inp=document.createElement('input');inp.type='text';inp.className='pending-input';inp.placeholder='جوابت رو بنویس...';
      inp.onkeydown=e=>{if(e.key==='Enter')ansPen(id,inp.value);};
      const btn=document.createElement('button');btn.className='btn btn-success btn-sm';
      btn.innerHTML='<i class="fas fa-check"></i>';btn.onclick=()=>ansPen(id,inp.value);
      row.appendChild(inp);row.appendChild(btn);div.appendChild(row);list.appendChild(div);
    }
  }catch(e){}
}
async function ansPen(id,text){
  if(!text.trim())return;
  try{
    const r=await fetch('/api/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id,text:text.trim()})});
    const d=await r.json();
    if(d.ok){loadPending();updateStats();showToast('پاسخ ارسال شد');}
  }catch(e){}
}

// ───── Logs ─────
async function loadLogs(){
  const list=document.getElementById('logs-list');
  try{
    const r=await fetch('/api/logs');const logs=(await r.json()).logs||[];list.innerHTML='';
    if(logs.length===0){list.innerHTML='<div class="empty-state"><i class="fas fa-inbox"></i><p>لاگ خالیه</p></div>';return;}
    for(const log of logs.reverse()){
      const div=document.createElement('div');div.className='log-item';
      div.innerHTML='<div><span class="log-time">'+esc(log.time)+'</span><span class="log-guid">'+esc(log.guid)+'</span><span class="log-from">'+esc(log.from)+'</span></div><div class="log-text">'+esc(log.text)+'</div>';
      list.appendChild(div);
    }
  }catch(e){}
}

// ───── Config ─────
async function loadConfig(){
  const el=document.getElementById('config-info');
  try{
    const r=await fetch('/api/config');const d=await r.json();const env=d.env||{};
    const items=[
      ['GEMINI_API_KEY','کلیدهای Gemini API'],
      ['SESSION_B64_PART1','سشن روبیکا (پارت ۱)'],
      ['SESSION_B64_PART2','سشن روبیکا (پارت ۲)'],
      ['OWNER_CONTROL_GROUP','گروه کنترل'],
      ['RUBIKA_PHONE','شماره تلفن'],
    ];
    let html='<table class="config-table"><thead><tr><th>متغیر</th><th>وضعیت</th></tr></thead><tbody>';
    for(const [k,label] of items){
      const ok=env[k];
      html+='<tr><td>'+label+'</td><td><span class="config-badge '+(ok?'ok':'error')+'"><i class="fas '+(ok?'fa-check-circle':'fa-times-circle')+'"></i>'+(ok?'تنظیم شده':'تنظیم نشده')+'</span></td></tr>';
    }
    html+='</tbody></table>';
    html+='<div class="guide-box"><h4><i class="fas fa-lightbulb"></i> راهنما</h4><p>کلیدهای API خود را در GEMINI_API_KEY با کاما جدا کنید تا چرخش خودکار فعال شود.</p></div>';
    el.innerHTML=html;
  }catch(e){el.innerHTML='<div class="empty-state"><i class="fas fa-exclamation-triangle"></i><p>خطا در بارگذاری</p></div>';}
}

// ───── Stats ─────
async function updateStats(){
  try{
    const r=await fetch('/api/stats');const d=await r.json();
    document.getElementById('st-kb').textContent=d.kb;
    document.getElementById('st-pen').textContent=d.pen;
    document.getElementById('st-log').textContent=d.today;
  }catch(e){}
  try{
    const r=await fetch('/api/config');const d=await r.json();
    document.getElementById('st-keys').textContent=d.env?.GEMINI_API_KEY?'فعال':'غیرفعال';
  }catch(e){}
}
updateStats();setInterval(updateStats,5000);
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
        "gemini_configured": bool(GEMINI_API_KEYS),
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

        def _send_chat():
            chat = model.start_chat(history=[])
            return chat.send_message(msg + kb_ctx)

        res = execute_with_rotation(_send_chat)
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

    # ذخیره در دانش (متن خام کاربر → پاسخ خام ادمین)
    with _lock_kb:
        knowledge_base[original["user_text"]] = text
    save_kb()

    # ──── بازنویسی با لحن ربات (AI) ────
    final_answer = text  # fallback اولیه
    try:
        def _rewrite():
            chat = model.start_chat(history=[])
            prompt = (
                f"کاربر پرسید: '{original['user_text']}'\n"
                f"پاسخی که باید بدی (مفهوم اصلی): '{text}'\n"
                f"حالا این پاسخ رو دقیقاً با لحن صمیمی و دوستانه خودت بنویس. "
                f"فقط پاسخ نهایی رو بنویس، بدون مقدمه."
            )
            return chat.send_message(prompt)

        response = execute_with_rotation(_rewrite)
        rewritten = (response.text or "").strip()
        if rewritten and len(rewritten) > 2:
            final_answer = rewritten
            log.info(f"ANSWER  AI rewrite OK: {final_answer[:80]}")
        else:
            log.warning("ANSWER  AI rewrite returned empty, using raw text")
    except Exception as e:
        log.error(f"ANSWER  AI rewrite failed: {e}, using raw text")

    # ──── ارسال پاسخ به کاربر از طریق روبیکا ────
    ok, send_result = send_msg_sync(
        original["chat_guid"],
        final_answer,
        reply_to=original.get("message_id"),
    )

    # ثبت message_id پیام ارسال‌شده تا بعداً ریپلای‌ها شناسایی بشن
    if ok and send_result is not None:
        try:
            sent_mid = getattr(send_result, "message_id", None)
            if sent_mid is not None:
                with _lock_sent:
                    bot_sent_message_ids.add(str(sent_mid))
                    _trim_bot_sent_ids()
                save_bot_sent()
                log.info(f"ANSWER  sent_msg_id={sent_mid}")
        except Exception as e:
            log.error(f"ANSWER  tracking sent msg failed: {e}")

    if ok:
        return jsonify({"ok": True, "reply": final_answer})
    else:
        # برگرداندن pending در صورت خطا
        with _lock_pending:
            pending_replies[pid] = original
        save_pending()
        return jsonify({"error": str(send_result)}), 500


@app.route("/api/logs")
def api_logs():
    with _lock_logs:
        return jsonify({"logs": list(reversed(chat_logs[-100:]))})


@app.route("/api/config")
def api_config():
    return jsonify({
        "env": {
            "GEMINI_API_KEY": bool(GEMINI_API_KEYS),
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

async def async_execute_with_rotation(chat_guid, prompt_text):
    max_tries = max(1, len(GEMINI_API_KEYS))
    for attempt in range(max_tries):
        chat = get_chat_session(chat_guid)
        if not chat:
            raise Exception("AI is disabled or chat session not found")
        try:
            try:
                response = await asyncio.to_thread(chat.send_message, prompt_text)
            except TypeError:
                loop = asyncio.get_running_loop()
                response = await loop.run_in_executor(None, chat.send_message, prompt_text)
            return response
        except Exception as e:
            err_str = str(e).lower()
            if "429" in err_str or "quota" in err_str or "exhausted" in err_str or "rate" in err_str:
                log.warning(f"⚠️ ارور لیمیت جمینای در محیط async. تلاش {attempt+1}/{max_tries}")
                if not switch_api_key():
                    raise e
            else:
                raise e
    raise Exception("تمامی کلیدهای API مسدود یا لیمیت شده‌اند.")


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
                    def _control_reply():
                        chat = model.start_chat(history=[])
                        prompt = (
                            f"کاربر پرسید: '{original['user_text']}'، "
                            f"پاسخ من: '{user_text}'، حالا با لحن خودت بگو."
                        )
                        return chat.send_message(prompt)
                    final_answer = execute_with_rotation(_control_reply).text
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
        # تشخیص ریپلای – چند روش مختلف برای خواندن reply_to_message_id
        reply_to = (
            getattr(update, "reply_to_message_id", None)
            or getattr(update, "reply_message_id", None)
        )
        # fallback: خواندن مستقیم از دیکشنری خام آپدیت
        if reply_to is None:
            try:
                raw = getattr(update, "original_update", {}) or {}
                msg = raw.get("message", {}) or {}
                reply_to = msg.get("reply_to_message_id")
            except Exception:
                pass

        reply_str = str(reply_to).strip() if reply_to is not None else None
        if reply_str in ("", "None", "0"):
            reply_str = None

        with _lock_sent:
            sent_ids_count = len(bot_sent_message_ids)
            is_reply_to_bot = reply_str is not None and reply_str in bot_sent_message_ids

        # لاگ تشخیصی برای دیباگ ریپلای
        if reply_str is not None:
            log.info(
                f"REPLY_CHECK  reply_to={reply_str}  "
                f"is_bot={is_reply_to_bot}  "
                f"sent_ids_count={sent_ids_count}"
            )

        # ✅ اگر ریپلای به پیام ربات نیست و کلمه ماشه هم نیست، نادیده بگیر
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
            sid = _extract_msg_id(sent)
            if sid is not None:
                with _lock_sent:
                    bot_sent_message_ids.add(sid)
                    _trim_bot_sent_ids()
                save_bot_sent()
                log.info(f"KB  tracked sent_msg_id={sid}")
            log.info("KB  پاسخ از دانش")
        except Exception as e:
            log.error(f"KB ERROR: {e}")
        return

    # ──── پاسخ از AI ────
    try:
        await asyncio.sleep(random.uniform(3, 6))
        
        # context دانش
        with _lock_kb:
            kb_items = list(knowledge_base.items())[-5:]
        kb_ctx = ""
        if kb_items:
            kb_ctx = "\nاطلاعات:\n" + "\n".join(f"- {q}: {a}" for q, a in kb_items)

        # ✅ سیستم چرخش خودکار کلیدهای API برای رفع ارور لیمیت
        prompt_text = user_text + kb_ctx
        try:
            response = await async_execute_with_rotation(chat_guid, prompt_text)
        except Exception as e:
            log.error(f"ERROR in AI response generation: {e}")
            return

        ai_text = response.text
        log.info(f"AI  {ai_text[:100]}")
        
        # Now fetch chat again just to keep its length in check
        chat = get_chat_session(chat_guid)

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
                sid = _extract_msg_id(sent)
                if sid is not None:
                    with _lock_sent:
                        bot_sent_message_ids.add(sid)
                        _trim_bot_sent_ids()
                    save_bot_sent()
                    log.info(f"REPLY  tracked sent_msg_id={sid}")
            except Exception as e:
                log.error(f"REPLY ERROR: {e}")
        else:
            sent = await update.reply(ai_text)
            sid = _extract_msg_id(sent)
            if sid is not None:
                with _lock_sent:
                    bot_sent_message_ids.add(sid)
                    _trim_bot_sent_ids()
                save_bot_sent()
                log.info(f"AI  tracked sent_msg_id={sid}")
            log.info("AI  پاسخ مستقیم")

        # ✅ باگ #11: محدود کردن تاریخچه چت
        if chat:
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
    print(f"🔑 Gemini API    : {'✅ فعال (' + str(len(GEMINI_API_KEYS)) + ' کلید)' if GEMINI_API_KEYS else '❌ غیرفعال'}")
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

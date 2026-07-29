# مرحلهٔ دوم — ابزارهای امن سروری روی Render

این نسخه تمام امکانات مرحلهٔ اول را حفظ می‌کند و بدون Termux یا کامپیوتر، ابزارهای امن را روی همان Render اجرا می‌کند.

## ابزارها

- وضعیت uptime، CPU load، RAM و Disk سرور
- بررسی سلامت URL عمومی با جلوگیری از SSRF و metadata داخلی
- یادآوری تکی، ساعتی، روزانه و هفتگی
- ارسال یادآوری به همان چت و `OWNER_CONTROL_GROUP`
- مانیتور سلامت URL و هشدار قطع/وصل
- مانیتور RSS/Atom و هشدار مطلب جدید
- ساخت فایل محدود TXT/JSON/CSV
- لینک دانلود امضاشده و یک‌ساعته
- Outbox اتمیک با retry برای ارسال‌های ناموفق

وجود ندارد: Shell آزاد، نصب پکیج، اجرای کد تولیدشده توسط AI، شبکه خصوصی، metadata سرور، یا دسترسی به فایل‌های خارج از `server_files`.

---

## ۱. فایل سرور

فایل `bot_phase2_server.py` را به `bot.py` تغییر نام دهید و جایگزین نسخهٔ فعلی کنید. `requirements.txt` نیازی به تغییر ندارد.

بعد از Deploy:

```text
https://YOUR-SERVICE.onrender.com/api/health
```

باید نمایش دهد:

```json
{"version":"phase2-server-tools-v1.0"}
```

---

## ۲. متغیرهای پیشنهادی Render

```env
SERVER_TIMEZONE=Asia/Tehran
AUTOMATION_DELIVERY_MODE=both
PUBLIC_BASE_URL=https://YOUR-SERVICE.onrender.com
AUTOMATION_FILE=server_automation.json
SERVER_FILES_DIR=server_files
```

برای لینک دانلود فایل، یک secret جدا بسازید:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

و در Render قرار دهید:

```env
FILE_SIGNING_SECRET=خروجی_تصادفی
```

اگر این متغیر را نگذارید، کد از `DASHBOARD_PASSWORD` برای امضای لینک استفاده می‌کند؛ secret جدا توصیه می‌شود.

### مقصد پیام‌ها

مقدار فعلی:

```env
AUTOMATION_DELIVERY_MODE=both
```

یعنی نتیجه به همان چت سازنده و گروه `OWNER_CONTROL_GROUP` ارسال می‌شود.

مقادیر دیگر:

```text
same_chat
control_group
both
```

---

## ۳. فرمان‌های نمونه

### وضعیت سرور

```text
وضعیت سرور رو بگو
```

### Health-check فوری

```text
این آدرس رو بررسی کن https://example.com
```

### یادآوری مستقیم بدون مصرف Gemini

```text
۱۵ دقیقه دیگه یادم بنداز که آب بخورم
```

### یادآوری با Agent

```text
فردا ساعت ۹ صبح یادم بنداز جلسه دارم
```

```text
هر روز ساعت ۸ صبح یادم بنداز گزارش‌ها را بررسی کنم
```

### مدیریت یادآوری

```text
لیست یادآوری‌ها رو بده
```

```text
یادآوری 0123456789 رو لغو کن
```

### مانیتور URL

حداقل فاصلهٔ بررسی ۵ دقیقه است:

```text
این سایت رو هر ۵ دقیقه مانیتور کن https://example.com
```

### مانیتور RSS

```text
این فید RSS رو هر ۱۰ دقیقه مانیتور کن https://example.com/feed.xml
```

### مدیریت مانیتورها

```text
لیست مانیتورها رو بده
```

```text
مانیتور 0123456789 رو حذف کن
```

### ساخت فایل

```text
یک فایل report.json بساز و این داده‌ها را داخلش قرار بده: {"status":"ok"}
```

پسوندهای مجاز:

```text
txt
json
csv
```

حداکثر حجم هر فایل ۱۰۰ کیلوبایت و حداکثر تعداد فایل‌ها ۵۰ عدد است.

---

## ۴. وضعیت Automation

پس از ورود با رمز Dashboard:

```text
https://YOUR-SERVICE.onrender.com/api/automation/status
```

خروجی شامل تعداد reminder، monitor و outbox در انتظار است.

---

## ۵. محدودیت ذخیره‌سازی JSON

طبق انتخاب فعلی، اطلاعات در JSON ذخیره می‌شوند. فایل‌های Render بدون Persistent Disk ممکن است هنگام Restart یا Deploy حذف شوند:

- `server_automation.json`
- پوشهٔ `server_files`

کد نوشتن اتمیک، Outbox و retry دارد، اما در برابر پاک‌شدن کل دیسک موقت نمی‌تواند اطلاعات را حفظ کند. برای پایداری واقعی باید بعداً PostgreSQL/Redis یا Persistent Disk اضافه شود.

---

## ۶. نکات امنیتی

- URLهای localhost، شبکه خصوصی، link-local و metadata مسدودند.
- redirect به شبکه خصوصی نیز مسدود است.
- فقط پورت استاندارد ۸۰ و ۴۴۳ مجاز است.
- فایل فقط داخل پوشهٔ محدود ساخته می‌شود.
- لینک دانلود HMAC و زمان انقضا دارد.
- ذخیره رمز، API key یا token در یادآوری رد می‌شود.
- حذف فایل نیازمند درخواست صریح «حذف/پاک» است.

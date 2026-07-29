# راه‌اندازی مرحلهٔ دوم — Android Worker با Termux

این نسخه گوشی اندرویدی را از طریق پیام‌های **مالک** در روبیکا کنترل می‌کند. گوشی فقط اتصال خروجی HTTPS به Render می‌سازد؛ هیچ پورتی روی گوشی باز نمی‌شود.

## قابلیت‌های نسخهٔ اول

- وضعیت کلی دستگاه و فضای ذخیره‌سازی
- وضعیت و سلامت باتری
- نمایش اعلان
- بازکردن لینک امن `http/https`
- تنظیم صدای موسیقی، زنگ، هشدار و اعلان بر حسب درصد
- خواندن متن با TTS گوشی
- ویبره
- روشن/خاموش‌کردن چراغ‌قوه
- بازکردن تنظیمات Android

**عمداً وجود ندارد:** اجرای Shell دلخواه، حذف فایل، خواندن مخاطبان/پیامک، لمس خودکار صفحه، نصب برنامه و خاموش‌کردن دستگاه.

---

## ۱. نصب برنامه‌های لازم

Termux و Termux:API را از **F-Droid** نصب کنید. نسخه‌های Play Store قدیمی و ناسازگارند. هر دو برنامه باید از یک منبع نصب شوند.

داخل Termux اجرا کنید:

```bash
pkg update && pkg upgrade
pkg install python termux-api unzip nano tmux
termux-setup-storage
```

درخواست‌های دسترسی Android را تأیید کنید و یک بار برنامهٔ **Termux:API** را باز کنید.

---

## ۲. ساخت توکن امن

داخل Termux:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

خروجی را کپی کنید. این مقدار محرمانه است و نباید در چت، اسکرین‌شات یا GitHub قرار بگیرد.

در Render، بخش Environment، این متغیرها را اضافه کنید:

```env
WORKER_TOKEN=توکن_تولیدشده
ANDROID_WORKER_ID=android-phone
```

`WORKER_TOKEN` باید حداقل ۳۲ کاراکتر باشد و با `DASHBOARD_PASSWORD` فرق داشته باشد.

---

## ۳. جایگزینی فایل سرور

از ZIP مرحلهٔ دوم، فایل `bot_phase2.py` را بردارید، نامش را به `bot.py` تغییر دهید و در پروژهٔ Render جایگزین کنید.

بعد از Deploy این آدرس باید نسخهٔ زیر را نشان دهد:

```text
https://YOUR-SERVICE.onrender.com/api/health
phase2-android-worker-v1.0
```

در لاگ Render باید ببینید:

```text
Android Worker: ✅ فعال (android-phone)
```

---

## ۴. تنظیم Worker روی گوشی

فایل‌های زیر را از ZIP به یک پوشه در Termux منتقل کنید:

- `android_worker.py`
- `worker_config.example.json`

مثال، اگر ZIP در Download است:

```bash
mkdir -p ~/rubika-worker
cd ~/rubika-worker
cp /sdcard/Download/phase2_android_worker.zip .
unzip phase2_android_worker.zip android_worker.py worker_config.example.json
cp worker_config.example.json worker_config.json
nano worker_config.json
```

مقادیر را تنظیم کنید:

```json
{
  "server_url": "https://YOUR-SERVICE.onrender.com",
  "worker_token": "همان WORKER_TOKEN موجود در Render",
  "worker_id": "android-phone",
  "poll_interval": 3
}
```

سپس:

```bash
chmod 600 worker_config.json
termux-wake-lock
python android_worker.py
```

اگر اتصال درست باشد، باید ببینید:

```text
Connected to server
```

برای اجرای پایدارتر با tmux:

```bash
tmux new -s rubika-worker
python android_worker.py
```

برای جداشدن بدون توقف Worker کلیدهای `Ctrl+B` و سپس `D` را بزنید. بازگشت:

```bash
tmux attach -t rubika-worker
```

Battery Optimization را برای Termux و Termux:API غیرفعال کنید؛ در غیر این صورت Android ممکن است Worker را متوقف کند.

---

## ۵. تست از Saved Messages مالک

```text
وضعیت باتری گوشی رو بگو
```

```text
وضعیت گوشی رو بگو
```

```text
یک اعلان بفرست: تست Worker روبیکا
```

```text
صدای موسیقی گوشی رو روی ۳۰ درصد بذار
```

```text
با صدای گوشی بگو سلام، Worker فعال شد
```

```text
گوشی رو ۷۰۰ میلی‌ثانیه بلرزان
```

```text
چراغ‌قوه گوشی رو روشن کن
```

```text
این لینک رو روی گوشی باز کن https://example.com
```

ربات ابتدا شناسهٔ Job را اعلام می‌کند. پس از اجرای گوشی، نتیجه به همان چت روبیکا ارسال می‌شود.

---

## ۶. بررسی وضعیت

درحالی‌که با رمز Dashboard وارد شده‌اید:

```text
https://YOUR-SERVICE.onrender.com/api/android/status
```

فهرست ۳۰ Job اخیر:

```text
https://YOUR-SERVICE.onrender.com/api/android/jobs
```

---

## رفع خطا

### `Worker authentication required`

`WORKER_TOKEN` در Render و `worker_config.json` دقیقاً یکسان نیست یا کوتاه‌تر از ۳۲ کاراکتر است.

### `Unknown worker_id`

مقدار `ANDROID_WORKER_ID` در Render و `worker_id` گوشی باید دقیقاً یکسان باشد.

### `termux-... در دسترس نیست`

```bash
pkg install termux-api
```

برنامهٔ Termux:API باید نصب باشد و مجوز Android مربوط به آن قابلیت را داشته باشد.

### Worker بعد از خاموش‌شدن صفحه قطع می‌شود

- Battery Optimization را غیرفعال کنید.
- `termux-wake-lock` اجرا کنید.
- از `tmux` استفاده کنید.

### نکتهٔ Render

فایل صف Job روی دیسک موقت Render ذخیره می‌شود. با Restart/Deploy ممکن است Jobهای در انتظار حذف شوند؛ برای نسخهٔ بعدی می‌توان PostgreSQL یا Redis اضافه کرد.

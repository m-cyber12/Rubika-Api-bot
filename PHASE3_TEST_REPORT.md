# گزارش تست مرحلهٔ سوم

تاریخ اجرای تست: 2026-07-30

## Static

- AST parse فایل کامل: Pass
- Ruff undefined/unused-local checks: Pass
- JavaScript استخراج‌شده از Dashboard با `node --check`: Pass
- چهار assertion اتصال UI میکروفن به endpoint و پخش audio: Pass

## Groq STT

- OGG موفق: Pass
- WebM موفق: Pass
- نرمال‌سازی whitespace متن: Pass
- 429 و موفقیت در تلاش دوم: Pass
- 401 بدون retry اضافی: Pass
- فایل بزرگ‌تر از سقف: Pass
- MIME کوتاه `ogg` و `mp3`: Pass
- MIME نامعتبر و CRLF/header injection: Pass

تست‌ها با HTTP شبیه‌سازی‌شده انجام شدند؛ کلید واقعی Render وارد محیط توسعه نشد.

## Edge TTS

- متن مستقل اول: Pass
- متن مستقل دوم: Pass
- cache hit و عدم فراخوانی دوباره provider: Pass
- cache محدود به ۱۰ آیتم: Pass
- provider با خروجی خالی: Pass
- خروجی بزرگ‌تر از سقف: Pass
- fallback متنی هنگام خطای TTS: Pass

تلاش واقعی اتصال به Edge برای هر دو صدای Farid و Dilara انجام شد، اما sandbox توسعه دسترسی خروجی مستقیم به `api.msedgeservices.com` نداشت. این محدودیت محیط تست بود؛ پس از Deploy باید روی Render یک تست زنده انجام شود.

## Dashboard Voice

- WebM → STT → Server Tool → متن+صوت: Pass
- OGG → STT → Reminder → متن+صوت: Pass
- بدون Basic Auth: Pass (401)
- فایل وجود ندارد: Pass (400)
- نوع غیرصوتی: Pass (415)
- فایل بزرگ: Pass (422)
- دو پردازش هم‌زمان و درخواست سوم: Pass (429)
- TTS ناموفق و حفظ متن: Pass
- MediaRecorder/getUserMedia wiring: Pass
- توقف خودکار ۶۰ ثانیه: Pass

## Rubika Voice

- Voice مالک در Saved Messages → Server Tool → متن+ویس: Pass
- Voice مالک → Direct Web Search → متن+ویس: Pass
- Voice مالک در گروه همراه «فرایدی»: Pass
- Voice غیرمالک قبل از download رد شد: Pass
- اندازه بزرگ قبل از STT رد شد: Pass
- مدت طولانی رد شد: Pass
- شکست STT بدون اجرای Agent: Pass
- شکست TTS با حفظ پاسخ متنی: Pass
- پیام تایپی بدون تولید ناخواسته ویس: Pass
- دو پیام متنی با درخواست صریح ویس: Pass
- دو عبارت منفی «فقط متن/ویس نفرست»: Pass
- اولویت عبارت منفی بر عبارت مثبت: Pass

## rubpy واقعی

- `Update.reply_voice` با bytes، type=Voice و filename: Pass
- caption و reply target در `Update.reply_voice`: Pass

این تست forwarding داخلی rubpy است؛ پخش codec MP3 باید یک بار پس از Deploy داخل اپ روبیکا تأیید شود.

## Regression قابلیت‌های قبلی

- BBC RSS رسمی: Pass
- Iran International News Sitemap: Pass
- Reminder: Pass
- Monitor: Pass
- JSON file: Pass
- CSV file: Pass
- SSRF با metadata IP: Pass
- SSRF با DNS خصوصی: Pass
- Dashboard auth رد/قبول: Pass

## نتیجه

تمام تست‌های قابل‌اجرا در sandbox پاس شدند. دو تست زندهٔ وابسته به شبکه/سرویس واقعی پس از Deploy باقی می‌مانند:

1. Groq با کلید واقعی Render
2. Edge TTS و پخش MP3 به‌عنوان Voice داخل اپ روبیکا

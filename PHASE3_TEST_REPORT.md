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
- دو پیام متنی Dashboard با درخواست صوتی و پخش‌کننده: Pass
- دو پیام متنی Dashboard بدون درخواست صوتی: Pass
- شکست TTS پیام متنی و حفظ پاسخ: Pass

## Rubika Read-only

- جست‌وجوی نام از get_chats/get_contacts/search_global_objects: Pass
- دو query مستقل با نتیجه: Pass
- no-result و partial API failure: Pass
- حذف متن last_message و شماره تلفن از خروجی: Pass
- ماسک GUID و حذف GUID کامل: Pass
- دو عبارت طبیعی فارسی و یک عبارت نامرتبط: Pass

## Rubika Safe Control

- ساخت chat_ref و message_ref بدون افشای GUID کامل: Pass
- خواندن اخیر و جست‌وجوی پیام: Pass
- آماده‌سازی ارسال بدون اجرای زودهنگام: Pass
- تأیید مالک درست و رد مالک دیگر: Pass
- جلوگیری از اجرای دوباره یک کد: Pass
- ویرایش پیام خودی و رد پیام دیگران: Pass
- حذف پیام خودی و رد پیام دیگران: Pass
- پین و آن‌پین با دو تأیید مستقل: Pass
- لغو و انقضای کد: Pass
- رد تأیید در گروه و رد تأیید صوتی: Pass
- انقضای chat_ref و message_ref: Pass
- جلوگیری از ارسال token/API key: Pass
- دو parser مستقیم برای خواندن/ارسال با ref: Pass
- چهار parser مستقیم ویرایش/حذف/پین/آن‌پین: Pass
- prepare مستقیم بدون اجرای زودهنگام: Pass
- ارسال مستقیم در حالت destructive_only: Pass
- چهار عملیات حساس همچنان Pending: Pass
- پنل Dashboard: لیست، تأیید، لغو، تاریخچه و Auth: Pass
- جلوگیری از تأیید تکراری Dashboard: Pass
- TTL پانزده دقیقه، chat_ref هفت روز و message_ref بیست‌وچهار ساعت: Pass
- دو حالت جایگزین delete_only و none: Pass

## Live JARVIS

- VAD loop و رسم waveform: Pass (syntax/marker)
- سکوت ۱٫۲ ثانیه و سقف نوبت ۳۰ ثانیه: Pass
- دو turn مستقل WebM/OGG از endpoint: Pass
- حالت‌های LISTENING/THINKING/SPEAKING: Pass (syntax/marker)
- Mute، Interrupt و Stop cleanup: Pass (syntax/marker)
- guard پردازش هم‌زمان: Pass

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

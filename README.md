# Multiple independent domain mappings

The Windows installer supports multiple X-UI origins in one Render service. `ORIGIN_ROUTES_JSON` is an authoritative JSON array of objects with exactly `domain`, `origin_ip`, `origin_sni`, `origin_port`. Set it as a Render environment variable, never commit live configuration. Example schema (replace the documentation address before use):

```json
[{"domain":"de.example.com","origin_ip":"203.0.113.10","origin_sni":"origin.example.com","origin_port":2083}]
```

Each HTTP Host routes to its own TLS-verified origin. Host matching is case-insensitive and ignores the optional port. Backend Host is rewritten to origin SNI plus its nonstandard port. Unknown hosts return 421; the gateway health path remains public. HTTP streams and WebSockets retain the selected backend. Each origin has an independent port; `PORT` is one internal HTTP listener behind Render TLS termination and does not expose another public port.

At most 100 routes / 64 KiB. Invalid or duplicate domains fail startup/config validation. When this key is absent, legacy `ORIGIN_*` behavior remains supported as described below. With the key present, stale legacy origin values are ignored. The route table is read at startup; environment changes require deployment. The installer registers all domains on the selected service and deploys after the env update. Route removal keeps Render/DNS registrations but stops forwarding to that origin. Existing sessions follow the platform deployment lifecycle; no cross-origin fallback is performed.

The following documentation describes the legacy control and deployment architecture; the multi-origin mode supersedes single-backend configuration examples when ORIGIN_ROUTES_JSON is set.

# REN Hybrid — Python مدیریت، HAProxy انتقال داده

این بسته جایگزین آزمایشی gateway تک-origin است. HAProxy مستقیماً روی PORT گوش می‌دهد؛
Python هیچ درخواست یا پیام کاربر را دریافت یا رله نمی‌کند. Python فقط تنظیمات، فرآیندها
و آمار را مدیریت می‌کند. HAProxy متن HTTP را به origin مشخص‌شده با TLS معتبر می‌فرستد.
پس از Upgrade، WebSocket به شکل جریان بایت عبور می‌کند.

## راه‌اندازی روی Render

1. محتویات همین پوشه را در یک مخزن آزمایشی قرار دهید؛ Dockerfile باید در ریشه باشد.
2. یک **Web Service جدید** با Language برابر **Docker** در همان region سرویس فعلی بسازید.
3. Dockerfile Path را `./Dockerfile` بگذارید. Docker Command را خالی بگذارید؛ ENTRYPOINT
   داخل image برابر `python3 /app/main.py` است. دستور `pip install` لازم نیست.
4. همان متغیرهای `ORIGIN_IP`، `ORIGIN_SNI` و `ORIGIN_PORT` سرویس فعلی را وارد کنید.
5. Health Check Path برابر `/_ren/health` باشد. این endpoint زنده‌بودن HAProxy را می‌سنجد؛
   سالم‌بودن origin را تضمین نمی‌کند و حتی با origin غیرفعال باید 200 بدهد.
6. `MAX_CONCURRENT=0` بگذارید یا متغیر را حذف کنید: محدودیت عددی برنامه غیرفعال است.
   متغیر قدیمی با مقدار 2000 یا 8000 را کپی نکنید مگر اینکه واقعاً سقف بخواهید.
7. پس از Deploy، پیام `REN Hybrid online build=ren-hybrid-1` و خطوط `REN_METRICS` را ببینید.
8. ابتدا با URL آزمایشی اتصال واقعی کلاینت را بررسی کنید. انتقال دامنه/ترافیک اصلی را بعد
   از تست سرعت، آپلود، دانلود و پایداری انجام دهید. نسخه فعلی را برای بازگشت نگه دارید.

این بسته روی Render منتشر نشده و تنظیمات سرویس فعلی را تغییر نداده است.

## متغیرها

| متغیر | پیش‌فرض | معنی |
|---|---|---|
| ORIGIN_IP | الزامی | IPv4 عمومی و ثابت origin؛ برای اتصال DNS استفاده نمی‌شود |
| ORIGIN_SNI | الزامی | نام گواهی origin؛ SNI، verifyhost و Host از این نام ساخته می‌شوند |
| ORIGIN_PORT | 443 | پورت TLS origin |
| ORIGIN_ENABLED | true | با false اتصال جدید به origin رد می‌شود؛ تونل‌های قبلی ادامه دارند |
| PORT | 10000 | پورت HTTP داخلی رندر |
| MAX_CONCURRENT | 0 | صفر یعنی بدون سقف پذیرش در برنامه؛ مقدار مثبت سقف اختیاری backend است |
| CONNECT_TIMEOUT | 12 | ثانیه؛ اتصال به origin |
| WS_HANDSHAKE_TIMEOUT | 20 | ثانیه؛ انتظار پاسخ Upgrade |
| HTTP_READ_TIMEOUT | 120 | ثانیه؛ سکوت خواندن/نوشتن HTTP |
| TUNNEL_IDLE_TIMEOUT | 3600 | ثانیه؛ سکوت کامل تونل پس از Upgrade؛ نه حداکثر عمر اتصال |
| STATS_INTERVAL | 10 | فاصله لاگ آمار، ثانیه |
| HAPROXY_THREADS | تشخیص CPU، حداکثر 8 | تعداد threadهای پردازش HAProxy؛ قابل تنظیم متناسب با پلن |
| ORIGIN_CA_FILE | فایل همراه بسته | CA مورد اعتماد؛ مثلاً /etc/secrets/custom-ca.pem |
| ORIGIN_CONFIG_FILE | خالی | مسیر فایل JSON اختیاری برای تغییر زنده origin |
| CONFIG_POLL_INTERVAL | 5 | فاصله بررسی فایل JSON، ثانیه |
| ALLOW_PRIVATE_ORIGIN | false | فقط برای تست محلی یا شبکه داخلی؛ در اینترنت لازم نیست |

«بدون سقف برنامه» به معنی نامحدودبودن ظرفیت سیستم نیست. HAProxy سقف socket خود را از
بودجه واقعی فایل‌های باز فرآیند، با مقداری رزرو، محاسبه می‌کند. سقف RAM، CPU، شبکه و origin
همچنان برقرارند. مقدار `resource_capacity` در لاگ سقف مشتق‌شده از FD است و ظرفیت امن
حافظه یا تضمین تعداد اتصال نیست. نرم‌افزار محدودیت FD نامحدود را کاهش نمی‌دهد.

## تفاوت‌های عمدی با نسخه aiohttp

- هیچ وابستگی runtime به aiohttp و هیچ monkey patch روی TLS در Python وجود ندارد.
- SSL_READ_BUFFER و WS_MAX_MSG قبلی استفاده نمی‌شوند؛ HAProxy پیام WebSocket را کامل
  بازسازی نمی‌کند و محدودیت 1MiB پیام در gateway وجود ندارد. محدودیت دو endpoint باقی است.
- HAProxy heartbeat وب‌سوکت تولید نمی‌کند. کلاینت/origin باید ping/pong مناسب داشته باشند.
  تونل بدون داده به مدت TUNNEL_IDLE_TIMEOUT بسته می‌شود؛ این با heartbeat=60 قبلی یکسان نیست.
- HTTP بدون Upgrade بعد از HTTP_READ_TIMEOUT سکوت قطع می‌شود؛ برای SSE مقدار مناسب انتخاب کنید.
- خطای اتصال TLS معمولاً 503 و timeout پاسخ origin معمولاً 504 است؛ معادل دقیق 502 قبلی نیست.
- Host به ORIGIN_SNI و پورت origin بازنویسی می‌شود. مسیر و query بدون بازنویسی برنامه‌ای عبور می‌کنند.
- استانداردهای HTTP و framing را HAProxy مدیریت می‌کند؛ گواهی و نام سرور همیشه بررسی می‌شوند.
- درخواست نامعتبر HTTP ممکن است طبق قوانین HAProxy رد شود؛ سازگاری کلاینت واقعی باید تست شود.
- مذاکره فشرده‌سازی WebSocket بین دو endpoint عبور می‌کند؛ HAProxy خودش پیام را فشرده یا باز نمی‌کند.

## تغییر زنده تنظیمات

فایل `origin.example.json` شکل ورودی را نشان می‌دهد. فقط چهار کلید ORIGIN_IP/PORT/SNI/ENABLED
در JSON پذیرفته می‌شوند. فایل JSON بر متغیرهای همان origin اولویت دارد. فایل جدید را
در همان filesystem با نوشتن فایل موقت و rename اتمی جایگزین کنید.

تغییر ORIGIN_ENABLED با سوکت Runtime API داخلی اعمال و در کانفیگ ماندگار می‌شود.
تغییر IP، پورت یا SNI ابتدا با `haproxy -c` بررسی شده، سپس graceful reload می‌شود.
تونل‌های قبلی روی worker قدیمی می‌مانند؛ اتصال‌های جدید به origin جدید می‌روند.
تا تخلیه worker قبلی، reload ساختاری بعدی عقب می‌افتد تا نسل‌های قدیمی جمع نشوند.
آخرین فایل معتبر پس از تغییر نامعتبر حفظ می‌شود. شکست تأییدشده reload باعث بازگردانی فایل
قبلی می‌شود و تونل سالم قطع نمی‌شود. اگر نتیجه reload نامشخص بماند، pending_reload در لاگ
true می‌شود و کنترلر تا روشن‌شدن نتیجه تغییر ساختاری دیگری اعمال نمی‌کند. CA، پورت frontend و timeoutها در این
نسخه با Deploy تغییر می‌کنند. تغییر env در Render خودبه‌خود فایل زنده را عوض نمی‌کند.

مدیریت فقط با Unix socket با دسترسی مالک فرآیند است؛ هیچ پورت یا API مدیریتی عمومی نیست.
روی Render، فایل‌های محلی پس از جایگزینی instance پایدار نیستند. برای تنظیمات ثابت از env
استفاده کنید؛ قابلیت فایل زنده فقط با منبعی که فایل را واقعاً به‌روزرسانی کند مفید است.
برای نسخه اول GitHub polling یا پنل مدیریت اضافه نشده است.

## لاگ و تفسیر

هر instance مقدار instance و pid جدا دارد. لاگ شامل بدنه، رمز یا آدرس کاربران نیست.

- `active`: اتصال backend فعال در worker فعلی به‌علاوه worker در حال تخلیه؛ نه تعداد افراد.
- `active_current` و `draining_connections`: تفکیک اتصال‌ها قبل و بعد از reload.
- `peak`: پیک worker فعلی از زمان شروع همان نسل، نه پیک هر 10 ثانیه.
- `rss_sum_mb`: جمع RSS کنترلر، master و workerها؛ صفحات مشترک ممکن است چندبار شمرده شوند.
- `cgroup_mb/cgroup_limit_mb`: مصرف/سقف کانتینر در صورت دسترسی؛ null یعنی در دسترس نیست.
- `fds_sum`: مجموع FD فرآیندها؛ `processes/current_workers/draining_workers` برای تشخیص تجمع.
- `bytes_in_total/bytes_out_total`: آمار HAProxy شامل سربار HTTP؛ صورتحساب ترافیک کاربر نیست.
- `connect_errors_total/response_errors_total/queued`: خطا و صف backend.
- `frontend_5xx_total/origin_5xx_total`: همه 5xxها؛ دقیقاً شمارنده رد ظرفیت نیستند.
- `generation`: با reload بالا می‌رود؛ شمارنده‌های total worker جدید از نو شروع می‌شوند.

در shutdown، کنترلر تا 20 ثانیه به جریان‌های موجود فرصت می‌دهد و سپس فرآیندها را می‌بندد.
Deploy یا حذف instance رندر می‌تواند اتصال‌های موجود را قطع کند؛ reload داخلی HAProxy
جایگزین قابلیت اتصال مجدد در کلاینت نیست.

## تست محلی

Runtime فقط Python استاندارد و HAProxy 3.2.26 می‌خواهد. برای اجرای تست‌ها:

```sh
python3 -m pip install -r requirements-dev.txt
HAPROXY_BIN=/path/to/haproxy python3 -m unittest discover -s tests -v
```

تست‌ها یک origin TLS موقت محلی می‌سازند. به openssl CLI نیاز دارند. اگر sandbox ساخت AF_UNIX
را ممنوع کند، فقط در تست transport مدیریت با TCP روی loopback جایگزین می‌شود؛ کد production
همچنان Unix socket استفاده می‌کند. image و استقرار نهایی باید روی محیط Docker/Render تست شوند.

ریشه‌های عمومی Origin CA از مستندات رسمی Cloudflare دریافت شده‌اند:
https://developers.cloudflare.com/ssl/static/origin_ca_rsa_root.pem
https://developers.cloudflare.com/ssl/static/origin_ca_ecc_root.pem

# گزارش نهایی مأموریت جامع مهندسی X Agent (ادامه ممیزی ۲۰۲۶-۱۱-۰۵)

> جزئیات هر نقص (بازتولید، علت، اصلاح، آزمون، محدودیت) در `defect-ledger.md` همین پوشه است. این سند جمع‌بندی و ماتریس بستن است.
> آمادگی تولید ادعا نمی‌شود. «آزموده» یعنی با PostgreSQL/Redis/Chromium/فایل‌سیستم واقعی محلی و سرویس‌های بیرونی شبیه‌ساز.

## الف) مخزن

| مورد | مقدار |
|---|---|
| مخزن | `omerae7/Xagent` |
| شاخه (تنها شاخه کار) | `ccr-345833d2-sca9ob` |
| نقطه شروع | `c201ed15835c830b64f833f2ed89a2200ae5b34a` (Critical Fix 1 حفظ و بازآزمایی شد) |
| Commitهای این مأموریت | `1fe4adf` مجوز · `ace479b` A2–A4 · `a0d1894` مرورگر · `c391b86` تحویل · `6501688` SSE/Worker/Lease · `1647bb7` A6/A7/Python/مسیریاب · `4ab9467` A8 · `748eba8` رابط/زیرساخت · (این گزارش) |
| `main` | تغییر نکرد؛ هیچ Merge انجام نشد. PR #2 پیش‌نویس (Draft) است |
| استقرار | هیچ استقراری روی VPS، ری‌استارت سرویس تولید، کلید واقعی، پیام تلگرام واقعی یا اقدام هزینه‌زا انجام نشد |
| شاخه/worktree اضافه | هیچ شاخه یا مخزن تازه‌ای ساخته نشد. worktreeهای **موقت** فقط برای اجرای Benchmark روی Commit مبنا ساخته و بلافاصله حذف شدند |

## ب) آزمون‌ها و اعتبارسنجی

- **پایه روی `c201ed1`:** ۲۵۶ موفق.
- **نهایی:** ۳۱۱ موفق، ۰ ناموفق، ۰ رد (`pytest -q -rs`). آزمون‌های Chromium اکنون در CI هم اجرا می‌شوند.
- **سایر بررسی‌ها:**
  - `ruff check`، `compileall`، `bash -n` همه اسکریپت‌ها و مجوز اجرای اسکریپت‌های به‌روزرسان: موفق.
  - `npm run typecheck` و `npm run build` (Node 22): موفق.
  - `docker compose config` روی نسخه موقت با `.env.example`: معتبر.
- **روش:** برای هر مرحله، تست بازتولید ابتدا روی کد پیش از اصلاح (Stash موقت فایل‌های منبع) اجرا شد.

| مرحله | فایل آزمون | پیش از اصلاح | پس از اصلاح |
|---|---|---|---|
| M1 مجوز | `test_master_authz.py` | ۵ شکست / ۲ موفق | ۷ موفق |
| M2 رویداد/تسویه/صندوق | `test_master_durability.py` | ۱۱ / ۱ | ۱۲ |
| M3 مرورگر (Chromium واقعی) | `test_master_browser.py` | ۹ / ۱ | ۱۰ |
| M4 تحویل | `test_master_delivery.py` | ۳ / ۰ | ۳ |
| M5 SSE/Worker/Lease (Uvicorn واقعی) | `test_master_worker.py` | ۸ / ۰ (SSE: پایین را ببینید) | ۸ |
| M6 تلاش مجدد/قابلیت/Python/مسیریاب | `test_master_providers.py`، `test_master_capabilities.py` | ۶ / ۲ | ۸ |
| M7 کانتکست | `test_master_context.py` | بازتولید دقیق A8 | ۵ |
| M8 داشبورد | `test_master_dashboard.py` | — | ۲ |

تست‌های قدیمی فقط برای سازگاری با رابط داخلی تازه به‌روز شدند؛ هیچ تستی حذف یا تضعیف نشد:
- امضای `_page` جعلی به `(*_, **__)` تغییر کرد.
- فیلد رمز در صفحه جعلی تشخیص داده می‌شود.
- Fixture آزادسازی تب‌های رهاشده تست‌ها اضافه شد.

## پ) ماتریس بستن یافته‌های ممیزی A1–A8

| ID | وضعیت | فایل‌های اصلی | آزمون رگرسیون | محدودیت باقی‌مانده |
|---|---|---|---|---|
| A1 شواهد تکمیل | VERIFIED FIXED (پیشین، Codex) — در هر مرحله بازآزمایی شد. آزمون B2 یک مسیر جاافتاده را بست: `browser_read` روی `about:blank` شاهد موفق حساب می‌شد | `validation.py`، `pipeline.py`، `computer.py` | `test_execution_validation.py`، `test_B2_pipeline_blank_claim_does_not_succeed` | تأیید معنایی هر Handler با خودش است |
| A2 چرخه حیات رویداد | VERIFIED FIXED | `lifecycle.py`، `worker/inbox.py`، `scheduler.py` | `test_A2_*` (۶) | پذیرش رویداد با یک Run فعال هم‌زمان محدود نشده است (فقط سقف دفعات/بودجه رزروشده) |
| A3 تسویه پایانی | VERIFIED FIXED | `scheduler.settle_terminal_runs`، `main.py` | `test_A3_*` | فقط ۱۴ روز اخیر بازبینی می‌شود |
| A4 دوام صندوق ورودی | VERIFIED FIXED | `main._handle_inbox`، `inbox.py`، `telegram.py` | `test_A4_*` (۴) | اعلان تلگرام کارفرما در پردازش دوباره ممکن است تکرار شود |
| A5 SSE و Pool | VERIFIED (معیار پذیرش) · NOT REPRODUCED (نشتی اصلی در این محیط تکرار نشد) | `routers/live.py`، `main.py` | `test_A5_*` | محیط ممیزی (`lite`، فرایند جدا) دوباره ساخته نشد |
| A6 تلاش مجدد پنهان | VERIFIED FIXED (۳→۱ و ۹→۳ درخواست) | `anthropic_provider.py` | `test_A6_*` (۳) | Wire test فقط Anthropic؛ سایر آداپتورها تلاش مجدد SDK ندارند |
| A7 آمادگی قابلیت | VERIFIED FIXED | `manifest.py`، `pipeline._self_knowledge` | `test_A7_*` (۲) | کاوش‌ها در آزمون شبیه‌سازی شدند؛ CDP واقعی در M3 |
| A8 بودجه کانتکست | VERIFIED FIXED | `working.py`، `pipeline.fit_request/bound_tool_output` | `test_A8_*` (۵) | تخمین توکن ابتکاری است؛ Replay استدلال فقط تخمین زده می‌شود |

## ت) ماتریس ریسک‌های اضافی

| ریسک | وضعیت | فایل‌ها | آزمون | محدودیت |
|---|---|---|---|---|
| دروازه مأموریت «ask» (P0) | VERIFIED FIXED | `pipeline._mission_gate/_process_calls` | `test_C1*` | — |
| اتصال/انقضای تأیید | VERIFIED FIXED | `pipeline._binding/_harness_decide`، `routers/chat.py` | `test_C2*`، `test_C3`، `test_C4` | اثرانگشت در JSON آرگومان (بدون Migration) |
| انسداد زمان‌بند با Dream | VERIFIED FIXED | `main.maybe_start_dream` | `test_slow_dream_*` | — |
| Lease fencing | VERIFIED FIXED | `pipeline.execute/_assert_owner`، `bus.lease_owned` | `test_lease_*`، `test_failed_lease_*` | اثر در حال انجام ممکن است نیمه‌کاره بماند؛ Exactly-once ادعا نمی‌شود |
| محدودیت پذیرش Worker | VERIFIED FIXED | `main.consume_runs/_dispatch`، `scheduler.reconcile_runs` | `test_worker_intake_is_bounded` | — |
| دوام فرمان کنترلی | VERIFIED FIXED (منطق) | `main._control_job/_control_abandoned` | `test_control_*` | Crash واقعی فرایند آزموده نشد |
| نظارت حلقه‌های Worker | VERIFIED FIXED (منطق) | `main.supervise/readiness`، `/api/health`، به‌روزرسان | `test_essential_loop_*`، `test_health_*` | در Docker آزموده نشد |
| همگام‌سازی دوباره رابط | PARTIALLY FIXED | `live.py` سرور، `live.ts`، `main.tsx`، `Shell.tsx` | `test_A5_revoked_*` (سمت سرور) | رفتار مرورگر واقعی کاربر آزموده نشد (فقط Build/Typecheck) |
| مرز ابزار کارمند | VERIFIED FIXED | `pipeline._employee_groups/_tools_for` | `test_employee_tool_groups_*` | `tool_groups` خالی = بدون محدودیت گروه (رفتار قبلی) |
| دسکتاپ مشترک | PARTIALLY FIXED | `computer.acquire_desktop/release_session` | `test_desktop_control_lease` | Lease یک‌اجرایی و حریم تب اعمال شد؛ Context مرورگر (Cookie) هنوز مشترک است — جداسازی کامل حساب کارمندان **نیست** |
| مسیریابی بر اساس کیفیت | VERIFIED FIXED | `router._task_stats` | `test_router_prefers_verified_*` | فقط با ≥۵ Run پایانی |
| تکرارپذیری استقرار | PARTIALLY FIXED | `compose.yaml`، به‌روزرسان، `verified-python-dependencies.txt` | `docker compose config` | Lockfile پایتون و برچسب ثابت ایمیج اعمال نشد؛ e2e به‌روزرسان اجرا نشد |

## ث) سناریوهای پذیرش (بخش ۲۱)

| ID | انتظار | نتیجه | نوع آزمون | وضعیت | شواهد |
|---|---|---|---|---|---|
| A1 ادعای بدون ابزار | رد | رد | شبیه‌ساز مدل | PASS | `test_execution_validation.py` |
| A2 ابزار ناموفق، ادعای موفق | رد | رد | شبیه‌ساز | PASS | همان |
| A3 ارزیاب نامعتبر | Fail-closed | Fail-closed | شبیه‌ساز | PASS | همان |
| A4 بدون تأیید پس از بازیابی | ناموفق | ناموفق | شبیه‌ساز | PASS | همان |
| A5 پرسش عادی | بدون ابزار | بدون ابزار | شبیه‌ساز | PASS | `test_bench_1_simple`، همان |
| B1 تب مجاز موجود | کشف و استفاده | کشف و استفاده، بدون بستن | Chromium واقعی | PASS | `test_master_browser.py` |
| B2 about:blank | ناموفق | ناموفق (ابزار و Pipeline) | Chromium + Pipeline | PASS | همان |
| B3 ناوبری | مقصد تأیید شود | تأیید شد؛ ۴۰۴ ناموفق | Chromium | PASS | همان |
| B4 صفحه ورود | «ورود لازم» | `auth_required` | Chromium + صفحه محلی | PASS | همان |
| B5 Timeout | خطای دقیق | `TimeoutError` | Chromium | PASS | همان |
| B6 بینایی ساختگی | رد | `vision_analyzed=false` → ناموفق | Chromium | PASS | همان |
| B7 تب کارمند دیگر | ممنوع | خصوصی، برداشته نشد | Chromium | PASS | همان |
| B8 دو اجرای هم‌زمان | جداسازی | جدا؛ بستن یکی دیگری را نبست | Chromium | PASS | همان |
| C1 ask مأموریت | بدون اجرا | منتظر اجازه | شبیه‌ساز تلگرام | PASS | `test_master_authz.py` |
| C2 تأیید با پارامتر دیگر | رد | `mismatch` | شبیه‌ساز | PASS | همان |
| C3 تأیید کهنه | رد | `expired` | شبیه‌ساز | PASS | همان |
| C4 فراتر از محدوده | رد و Audit | رد (مأموریت لغوشده در لحظه اجرا) | شبیه‌ساز | PASS | همان |
| D1 روتین مرورگری فارسی | روتین معتبر | — | شبیه‌ساز (آزمون‌های پیشین) | PASS | `test_autonomy_acceptance.py` |
| D2 اجرای سررسید | اجرا | اجرا | DB/Redis واقعی | PASS | `test_slow_dream_*`، پیشین |
| D3 فاصله بین نوبت‌ها | صفر LLM | صفر | شبیه‌ساز | PASS | `test_bench_4_no_event_routine` |
| D4 انقضا | بدون نوبت | بسته شد | DB واقعی | PASS | `test_A2_expired_*` |
| D5 بودجه/دفعات تمام | بدون Run | بدون Run | DB واقعی | PASS | `test_A2_exhausted_*` |
| D6 Run پایان یافت | پایانی، روتین مستقل | تسویه درست | DB واقعی | PASS | `test_A3_*` |
| D7 رقابت دو Worker | بدون تکرار | یک Run | DB واقعی | PASS | `test_A2_concurrent_*`، پیشین |
| E1 شکست صندوق | قابل بازیابی | Pending ماند | Redis واقعی | PASS | `test_A4_failed_*` |
| E2 مرگ پس از اقدام بیرونی | بدون تکرار کور | Outbox نامعلوم تکرار نشد | شبیه‌ساز | PASS | `test_master_delivery.py`، پیشین J |
| E3 شکست تسویه | تطبیق | تسویه یک‌باره | DB واقعی | PASS | `test_A3_*` |
| E4 ری‌استارت با صف | ادامه ایمن | پذیرش محدود + نشانه | Redis واقعی | PASS (منطق) | `test_worker_intake_*` |
| F1 Python مجاز | اجرا در Sandbox | `6.0` | LocalSandbox واقعی | PASS | `test_master_capabilities.py` |
| F2 Python ممنوع | بدون اجرا | اجرا نشد | LocalSandbox | PASS | همان |
| F3 مرورگر قطع | آماده نیست | `needs_setup`/`unverified` | کاوش شبیه‌ساز | PASS | همان |
| F4 ۴۲۹/Timeout | حسابداری درست | ۳ درخواست = ۳ رکورد | Transport جعلی SDK | PASS | `test_master_providers.py`، پیشین |
| F5 گفتگوی طولانی | محدود | در بودجه | DB + Pipeline | PASS | `test_master_context.py` |
| G1 هشت SSE | بدون اشغال Pool | ≤۱ اتصال | Uvicorn واقعی | PASS | `test_master_worker.py` |
| G2 قطعی Backend | پیام روشن | نوار قطعی + سلامت محدود | Build/Typecheck + API | PARTIAL (رفتار مرورگر کاربر NOT RUN) | `Shell.tsx`، `test_health_*` |
| G3 شکست سلامت به‌روزرسان | امن/Rollback | دروازه آمادگی Worker اضافه شد | `bash -n` | NOT RUN (e2e Docker به‌روزرسان اجرا نشد) | `xagent-update.sh` |
| G4 بازگشایی Run | وضعیت/شواهد پایدار | پایدار | DB واقعی | PASS | `test_master_dashboard.py` |

## ج) آمادگی عملیاتی

| دسته | قابلیت‌ها |
|---|---|
| آزموده با وابستگی‌های واقعی محلی | دروازه مجوز و تأیید، چرخه حیات روتین/رویداد، تسویه، صندوق رویداد، Lease fencing، پذیرش محدود، SSE و Pool، مرورگر Chromium (CDP)، Python در LocalSandbox، بودجه کانتکست |
| آزموده با سرویس بیرونی شبیه‌ساز | مدل‌ها (پاسخ اسکریپتی)، تلگرام (FakeTelegram)، SDK Anthropic (Transport جعلی)، Telegram Web (صفحه محلی)، دسکتاپ (Sandbox جعلی) |
| آزمون زنده Staging | **هیچ‌کدام** |
| تأییدنشده | تلگرام واقعی/MTProto، Telegram Web واقعی، ارائه‌دهندگان پولی، دسکتاپ XFCE واقعی، Docker Runner، e2e به‌روزرسان، دسترسی عمومی HTTPS/DNS |
| شکست شناخته‌شده | هیچ تست ناموفقی. محدودیت‌های طراحی در بخش «ت» آمده است |

## چ) پایداری و کارایی (فقط اندازه‌گیری‌های واقعاً انجام‌شده)

- **تلاش مجدد Anthropic:** یک فراخوانی ۳ → ۱ درخواست فیزیکی. سیاست سه‌تلاشی ۹ → ۳ درخواست، با ۳ رکورد دفتر مصرف.
- **کانتکست (۴ فایل بزرگ، پنجره ۲۴K):**
  - درخواست‌ها پیش `1183→16254→31325→46396` و پس `1183→3791→6398→9006→9240` (نویسه÷۴).
  - مجموع ۹۵٬۱۵۸ → ۲۹٬۶۱۸.
- **پنجره کاری:** پیام ۴۰٬۰۰۰ نویسه با بودجه ۱۰۰ از ۱۶٬۹۲۴ توکن تخمینی به ≤۱۰۰ رسید.
- **شش مأموریت معیار** (`c201ed1` → HEAD، مدل شبیه‌ساز):
  - فراخوانی مدل و ابزار بدون تغییر.
  - توکن ورودی ‎+۰٫۳ تا ‎+۰٫۵٪.
  - ادعای کاهش هزینه برای این مسیرها نمی‌شود.
- **SSE:** ۸ جریان هم‌زمان با ≤۱ اتصال DB و پاسخ سلامت/داشبورد زیر ۳ ثانیه. کد قدیمی هم در این محیط اتصال نگه نداشت.
- **زمان‌بند:** با Dream کندِ ۳۰ ثانیه‌ای، روتین سررسیده در کمتر از ۴ ثانیه (تیک ۰٫۲ ثانیه) اجرا شد.
- **ایمنی منفی:** در همه آزمون‌های منفی تعریف‌شده، صفر اجرای غیرمجاز و صفر موفقیت کاذب مشاهده شد. این نتیجه فقط درباره همین آزمون‌هاست، نه تضمین عمومی.

## ح) آماده‌سازی استقرار (اجرا نشد — فقط با تأیید کارفرما)

1. **پیش‌نیاز:** پشتیبان کامل خودکار به‌روزرسان (`xagent backup`) و بررسی فضای دیسک.
2. **Migration:** این مأموریت **Migration تازه ندارد** (زنجیره همچنان `0001→0007`). همه تغییرات State در JSON موجود یا ستون‌های موجود است.
3. **پیکربندی:**
   - تنظیمات تازه‌ای لازم نیست.
   - `compose.yaml` فقط وابستگی web را از `service_healthy` به `service_started` تغییر داد.
   - ثابت‌ها با پیش‌فرض امن: TTL تأیید ۲۴ ساعت، کارت در انتظار ۷۲ ساعت، سقف خروجی ابزار ۱۲٬۰۰۰ نویسه.
4. **فرمان:**
   ```
   sudo xagent update --ref <SHA نهایی> --dry-run
   sudo xagent update --ref <SHA نهایی>
   ```
5. **سلامت:**
   - `curl -s https://<دامنه>/api/health`: باید `ok:true` و `worker.ready:true` برگرداند.
   - دروازه به‌روزرسان اکنون آمادگی حلقه‌های Worker را هم می‌سنجد.
6. **آزمون دود پس از استقرار:**
   - ورود.
   - گفتگوی ساده.
   - یک روتین ساده و مشاهده تسویه.
   - `browser_tabs` و باز کردن یک صفحه عمومی.
   - بررسی نوار قطعی با توقف موقت API در Staging.
7. **بازگشت:** `xagent rollback` (کد). چون Migration تازه‌ای نیست، بازگشت داده لازم نیست. Agentهای Cursor فعال را پیش از بازگشت لغو کنید.
8. **صلاحیت زنده باقی‌مانده (با حساب‌های غیرتولیدی):**
   - Telegram Web واقعی (QR/ورود، خواندن گفتگو).
   - MTProto.
   - مدل‌های پولی با سقف هزینه.
   - دسکتاپ XFCE.
   - e2e به‌روزرسان با داده نمونه.
   - HTTPS عمومی.

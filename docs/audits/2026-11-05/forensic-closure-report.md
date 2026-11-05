# گزارش نهایی بستن یافته‌های ممیزی جرم‌شناختی X Agent (F01–F22)

> جزئیات بازتولید، ریشه، اصلاح و ریسک هر یافته در `forensic-remediation.md` همین پوشه است.
> «VERIFIED FIXED» یعنی بازتولید روی کد پیش از اصلاح شکست خورد و پس از اصلاح موفق شد. آزمون‌ها محلی بودند: PostgreSQL/Redis/Chromium/Uvicorn واقعی و یکپارچه‌سازی‌های بیرونی شبیه‌ساز.
> **آمادگی تولید ادعا نمی‌شود:** هیچ گردش کار زنده‌ای روی VPS، حساب تلگرام واقعی یا ارائه‌دهنده واقعی آزموده نشد.

## الف) مخزن و Commitها

| مورد | مقدار |
|---|---|
| مخزن / شاخه | `omerae7/Xagent` / `ccr-345833d2-sca9ob`. شاخه، worktree یا مخزن تازه ساخته نشد |
| HEAD آغاز (خط مبنای ممیزی) | `bc79cc75eea9ef72b43c71df8da6d04b79397a16`. ۳۳۳ موفق، محلی |
| مرحله ۱ (F01، F02، F21) | `131fe8f` |
| مرحله ۲ (F03، F04، F05، F06) | `f8f2fed` |
| مرحله ۳ (F07، F08، F10، F18، F19، F22) | `675429e` |
| مرحله ۴ (F09) | `00bee52` |
| مرحله ۵ (F11، F12، F14، F15، F16) | `a8da22a` |
| مرحله ۶ (F13، F17، F20) | `2b92e24` |
| ریسک‌های اضافی و این گزارش | `ecd9651` (به‌علاوه یک Commit مستندات که فقط این جدول و جدول CI را با شواهد نهایی پر کرد) |
| `main` / استقرار | تغییری نکرد؛ Merge انجام نشد. PR #2 پیش‌نویس ماند. هیچ استقرار، ری‌استارت VPS یا تغییر داده تولید انجام نشد |

## ب) ماتریس ۲۲ یافته

| ID | شدت | نقص اصلی | ریشه | فایل/تابع تغییرکرده | آزمون بازتولید (پیش → پس) | وضعیت | ریسک باقی‌مانده |
|---|---|---|---|---|---|---|---|
| F01 | P0 | Viewer/Owner اسرار پیکربندی ابزار را از `/api/memory/revisions` و مسیرهای مرتبط می‌خواندند | Snapshot کامل `config` و `ser()` بدون پاک‌سازی | `redact.py` (`redact_obj`، `seal_secrets`/`open_secrets`، `secret_paths`)، `revisions.py`، روترهای memory/control/integrations/live، installer، Worker، `cli.secrets-inventory` | `test_forensic_s1.py`: ۶ شکست → ۶ موفق | VERIFIED FIXED | سوابق تاریخی و `tools_installed.config` زنده متن‌ساده‌اند (از API پنهان). چرخش اعتبارنامه با کارفرما |
| F02 | P1 | تزریق اسکریپت از Markdown گفتگو | `esc()` نقل‌قول را escape نمی‌کرد؛ innerHTML | `components/markdown.tsx` (`ChatMarkdown`، `safeHref`)، `chat.tsx`، `Settings.tsx`، CI | `test_forensic_s1_markdown.py` (Chromium): JS اجرا شد → ۰ اجرا | VERIFIED FIXED | — |
| F03 | P1 | تأیید/مأموریت/سیاست/کارمند کهنه در لحظه اثر | `expire_on_commit=False`، `state.approved` بدون بازسنجی | `pipeline._authorize_effect`/`_lineage_block`/`_fresh`، `harness.load_rules(fresh)`، `xagent_core/approvals.py`، `chat.decide`، تلگرام | `test_forensic_s2_authz.py` (F03): ۵ شکست → ۵ موفق | VERIFIED FIXED | فاصله TOCTOU کوچک تا فراخوانی سرویس بیرونی |
| F04 | P1 | طبقه‌بندی اثر کلیک با رشته selector، نه عنصر | `_describe` برای selector/text برچسب ساختگی | `computer._INFO_FN`/`_describe_all`/`_locator`/`classify_effect` | `test_forensic_s2_browser.py`: ۴ شکست → ۶ موفق | VERIFIED FIXED | برچسب گمراه‌کننده بدون نشانه DOM |
| F05 | P1 | کارمندان کوکی/حافظه مرورگر یکدیگر و کارفرما را می‌دیدند | یک Context مشترک | `computer._context`/`_identities`/`_default_ctx`/`_save_identity`، `BrowserSharingSettings`، policy | اسکریپت روی کد قبلی: B و کارفرما `A-val` را دیدند → `test_forensic_s2_identity.py` ۵ موفق | VERIFIED FIXED | ذخیره هویت هنگام پایان نشست؛ یک فرایند Chrome |
| F06 | P1 | فراخوانی بعدی Batch پس از STOP_ALL/لغو اجرا شد؛ صف روتین/والد لغوشده اجرا شد | `_guard` یک‌بار در هر دور؛ پرچم TTL؛ لغو بدون انتشار | `pipeline._authorize_effect`/`_lineage_block`، `chat.cancel_run` | `test_forensic_s2_authz.py` (F06): ۵ شکست → ۵ موفق | VERIFIED FIXED | ابزار در حال اجرا قطع نمی‌شود |
| F07 | P1 | `file_read` بی‌ربط ارسال پیام را «اثبات» کرد | اعتبارسنجی فقط موفق بودن شواهد | `validation.objective_report`/`required_objectives`، `pipeline._validate` | `test_forensic_s3_objective.py`: ۳ شکست → ۵ موفق | VERIFIED FIXED | تشخیص هدف الگومحور است |
| F08 | P1 | پاسخ MCP بین کلاینت‌های هم‌زمان جابه‌جا/گم شد | `pending` با شناسه اصلی کلاینت | `infra/desktop/agentd.py` (`StdioServer`) | `test_forensic_s3_mcp.py` (agentd واقعی): ۳ شکست → ۳ موفق | VERIFIED FIXED | حالت داخلی سرور MCP مشترک است |
| F09 | P1 | بودجه ریشه بر نوه/هم‌سطح/رزرو نامعلوم اعمال نمی‌شد | بررسی تک‌سطحی با سقف سراسری | `usage_ledger.begin_attempt`/`_subtree_exposure`/`_run_cap` | `test_forensic_s4_budget.py`: ۳ شکست → ۳ موفق | VERIFIED FIXED | قفل سراسری سریالی‌کننده |
| F10 | P2 | شناسه فراخوانی ارائه‌دهنده از فضای کاری بیرون نوشت | شناسه خام در مسیر | `pipeline.bound_tool_output` | `test_forensic_s3_artifacts.py`: ۲ شکست → ۳ موفق | VERIFIED FIXED | — |
| F11 | P1 | Rollback خودکار داده‌های پس از ارتقا را بی‌صدا حذف می‌کرد | downgrade بدون کپی؛ ادعای نادرست | `xagent-update.sh` (`preserve_failed_release`، `rollback`)، `xagentctl`، e2e «۲ج» | `test_forensic_s5_rollback_preserve.py` (PG محلی): ۲ شکست → ۲ موفق | VERIFIED FIXED (منطق؛ e2e Docker فقط در CI) | داده جدول حذف‌شده فقط در کپی است و ادغام دستی می‌خواهد |
| F12 | P1 | پیام تلگرام هنگام قطعی DB با ری‌استارت گم شد | صف حافظه فرایند | `telegram.IngestSpool`، `_store`/`flush_pending` | `test_forensic_s5_telegram_ingest.py`: ۲ شکست → ۲ موفق | VERIFIED FIXED | پیامی که Telethon دریافت نکرده بازپخش نمی‌شود |
| F13 | P1 | نصب دوباره `reset --hard` و بازنویسی `.env` | نبود نگهبان | `install.sh` (`existing_install_guard`، `--repair`، `repair_code`/`repair_env`) | `test_forensic_s6_installer.py`: ۳ شکست → ۳ موفق | VERIFIED FIXED | گام‌های Docker/systemd در `--repair` روی VPS آزموده نشد |
| F14 | P2 | اعلان کارفرما برای تراکنش برگشتی ارسال شد | ارسال داخل تراکنش | `inbox._deliver`/`dispatch_pending`، اعلان تأیید Pipeline، `notify_owner`، Worker | `test_forensic_s5_notify.py`: ۴ شکست → ۴ موفق | VERIFIED FIXED | رویداد زنده SSE پیش از Commit |
| F15 | P2 | Run پایانی قدیمی (>۱۴ روز) هرگز تسویه نشد | فیلتر سن ایجاد؛ دسته ثابت | `scheduler.settle_terminal_runs`/`_record_settle_error` | `test_forensic_s5_settlement.py`: ۲ شکست → ۳ موفق | VERIFIED FIXED | — |
| F16 | P2 | جریان SSE پس از قطع Redis «سالم ولی بی‌رویداد» | پمپ بدون نظارت | `live.stream` (نظارت پمپ)، `bus.subscribe(ready)`، `live.ts` | `test_forensic_s5_sse.py` (Uvicorn+Redis): شکست → موفق | VERIFIED FIXED | — |
| F17 | P2 | سلامت داخلی به‌جای HTTPS عمومی | فقط `/api/health` داخلی | `infra/scripts/deploy_check.py`، `xagentctl doctor`، `install.sh` | `test_forensic_s6_deploy.py` (TLS محلی): ۴ موفق؛ آزمون doctor روی کد قبلی شکست | VERIFIED FIXED (منطق) | روی VPS و با کاوشگر بیرونی اجرا نشد |
| F18 | P2 | رسید ناقص/بدشکل «ارسال شد» ثبت شد | نبود اعتبارسنجی رسید | `telegram.valid_receipt`/`telegram_send`، `pipeline._deliver_final`، `chat.tsx` | `test_forensic_s3_delivery.py`: ۱۰ شکست → ۱۱ موفق | VERIFIED FIXED | — |
| F19 | P2 | Rollback ابزار با پیکربندی نسخه فعلی و «ready» با اتصال ناموفق | نبود پیکربندی نسخه‌ای | `installer.install`/`rollback` | `test_forensic_s3_rollback.py`: ۴ شکست → ۴ موفق | VERIFIED FIXED | آزمون اتصال فقط `list_tools` است |
| F20 | P2 | قطعی سرویس کاربر را به صفحه ورود برد | `me.isError → setAuthed(false)` | `api.failureKind`، `App.tsx` (`ServiceUnavailable`)، `Shell.tsx` | `test_forensic_s6_outage_ui.py` (Chromium، پنل قدیمی و جدید): شکست → موفق | VERIFIED FIXED | — |
| F21 | P2 | رمز از مشاهدات مرورگر، رونوشت، Checkpoint و تأیید نشت کرد | `el.value` در برچسب؛ `value` تایپ پنهان نمی‌شد | `computer` (`_SENSITIVE_FN`/`_LABEL_FN`/`browser_type`)، Pipeline (`masked_args`/`_mark_sensitive`/`_sealed`/HMAC binding) | `test_forensic_s1_browser.py`: ۴ شکست/۱ کنترل → ۵ موفق | VERIFIED FIXED | `desktop_input` حساسیت را نمی‌داند |
| F22 | P2 | درخواست بزرگ‌تر از ظرفیت ارسال شد | `fits=false` فقط ثبت می‌شد | `pipeline.plan_submission`/`CapacityExceeded` | `test_forensic_s3_capacity.py`: ۲ درخواست ارسال شد → ۰ | VERIFIED FIXED (درخواست اجرایی) | فراخوانی‌های کوچک دیگر پوشش داده نشدند |

**جمع:** ۲۲ از ۲۲ «VERIFIED FIXED» در محدوده آزمون‌های محلی تعریف‌شده. هیچ‌کدام «تأیید زنده» نیستند.

## پ) ریسک‌های معماری اضافی

| ریسک | نقص واقعی؟ | اقدام | وضعیت |
|---|---|---|---|
| بازپخش اثر بیرونی عمومی پس از Crash | بله: پس از Crash میان اثر و Checkpoint، همان فراخوانی دوباره اجرا می‌شد | `effect_intents` پایدار پیش از اثر؛ قصد باز در اجرای بعد → «نامعلوم»، بدون تکرار. آزمون Crash واقعی (BaseException) | VERIFIED FIXED |
| ازدست‌رفتن مالکیت تب پس از ری‌استارت | تب‌های کارمند حالا در Context جدا هستند و با قطع اتصال بسته می‌شوند (F05). تب کارفرما فقط برای اجرای کارفرما قابل امانت است | بدون تغییر بیشتر | PARTIALLY FIXED (با F05) |
| اختیار گسترده کنترل تصویری دسکتاپ | بله: پیش‌فرض `desktop_control=auto` | تغییر سیاست پیش‌فرض بدون تصمیم کارفرما انجام نشد. پیشنهاد: کارفرما آن را «ask» کند، به‌علاوه مرز گروه کارمند (موجود) | NOT STARTED (طرح) |
| خروجی مرورگر و SSRF | بله: `browser_open` محدودیت مقصد خصوصی/loopback/metadata ندارد | طرح: رهگیری `page.route` برای همه درخواست‌ها و هدایت‌ها، رد بازه‌های خصوصی با فهرست مجاز کارفرما. پیاده نشد چون دسترسی سرویس‌های محلی را تغییر می‌دهد و سیاست کارفرما می‌خواهد | NOT STARTED (طرح) |
| ابطال جلسه طولانی VNC | بله: احراز فقط هنگام اتصال | بازسنجی نشست هر ۱۵ ثانیه و بستن با ابطال/غیرفعال‌شدن. آزمون WebSocket واقعی | VERIFIED FIXED |
| ورودی نامحدود فرمان کنترلی | بله: جریان `xagent:control` بی‌حد رشد می‌کرد | `bus.enqueue_control`: سقف صف پردازش‌نشده و ۴۲۹ صریح، با بریدن فقط تاریخچه | VERIFIED FIXED |
| هم‌پوشانی روتین رویدادی | در این مأموریت بازتولید نشد | — | NOT VERIFIED |
| اختیار مشترک فایل‌سیستم کارمندان | بله: فضای کاری مشترک | طرح: زیرپوشه هر کارمند با سیاست مسیر | NOT STARTED (طرح) |
| واگذاری هم‌زمان فرزند و Timeout | فرزند داخل فراخوانی ابزار والد با `TOOL_TIMEOUT` اجرا می‌شود. لغو اکنون منتقل می‌شود (F06) | بدون تغییر بیشتر | PARTIALLY FIXED |
| ادعای بیش از حد Capability Manifest | در A7 قبلی با آزمون واقعی قابلیت اصلاح شده بود؛ این‌جا دوباره سنجیده نشد | — | NOT VERIFIED (در این مأموریت) |
| موفقیت پروتکل MCP ≠ اثر تأییدشده | بله: `_mcp_tool` موفقیت پروتکل را `verified=True` می‌گیرد | طرح: استفاده از annotations (`readOnlyHint`/`destructiveHint`) و رسید ساختاریافته. تغییر بدون تصمیم کارفرما همه ابزارهای MCP اثرگذار را از کار می‌انداخت | NOT STARTED (طرح) |
| نمایش مواجهه مالی حل‌نشده | قبلاً نمایش داده می‌شد (صفحه مصرف: «رزرو/نامشخص») | بدون تغییر | VERIFIED (موجود) |
| اسرار پیش‌فرض ناامن production | بله: کلید پیش‌فرض شناخته‌شده پذیرفته می‌شد | API و Worker با کلید/توکن پیش‌فرض یا خالی اجرا نمی‌شوند. کلید کوتاه سفارشی فقط هشدار می‌دهد تا نصب موجود از کار نیفتد | VERIFIED FIXED |
| رقابت Bootstrap/Migration هم‌زمان | Bootstrap: بله | `pg_advisory_xact_lock` در `bootstrap`. Migration ورودی کانتینر بررسی نشد | PARTIALLY FIXED |
| رفتار اتصال دوباره دسکتاپ | — | — | NOT VERIFIED |

## ت) آزمون‌ها

| دسته | شمار و نتیجه |
|---|---|
| خط مبنا (`bc79cc7`) | ۳۳۳ موفق |
| نهایی محلی (پیش از Commit گزارش) | **۴۲۴ موفق، ۰ ناموفق، ۰ رد** (`pytest -q -rs`). `ruff` تمیز. `tsc` و `npm run build` موفق |
| آزمون‌های تازه | ۹۱: مرحله ۱: ۱۳، مرحله ۲: ۲۱، مرحله ۳: ۲۹، مرحله ۴: ۳، مرحله ۵: ۱۲، مرحله ۶: ۸، ریسک‌ها: ۵ (پیام Commit مرحله ۵ به‌اشتباه ۱۴ نوشته بود؛ شمار درست ۱۲ است) |
| یکپارچه‌سازی واقعی محلی | PostgreSQL، Redis، Chromium (Playwright/CDP)، Uvicorn، فرایند `agentd`، سرور TLS با CA آزمون، پایگاه داده یکبارمصرف PG، مخزن git موقت، و ساخت vite پنل |
| یکپارچه‌سازی بیرونی شبیه‌ساز | مدل (MockLLM)، تلگرام (FakeTelegram)، سرور MCP ساختگی، API پنل ساختگی (F20)، کاوشگر عمومی ساختگی (F17) |
| استیجینگ زنده | **اجرا نشد** |
| اجرانشده محلی | e2e واقعی Docker به‌روزرسان (`tests/updater/e2e.sh`، شامل «۲ج» برای F11) و `caddy_tls.py`: Docker daemon در دسترس نبود. در CI (کار containers) اجرا می‌شوند |
| CI | همه اجراهای push موفق: #180 (`131fe8f`)، #182 (`f8f2fed`)، #184 (`675429e`)، #186 (`00bee52`)، #188 (`a8da22a`)، #190 (`2b92e24`)، #192 (`ecd9651`). در #192: کار tests شامل Lint، `pytest -q -rs` با **۴۲۴ موفق** بدون رد (۲۶۰٫۹ ثانیه) و `npm run build`؛ کار containers شامل ساخت همه Imageها، HTTPS بدون SNI با Caddy واقعی و e2e به‌روزرسان (شامل «۲ج» برای F11) |

## ث) تغییرات پیکربندی و Schema

- **Migration تازه:** ندارد. Schema تغییری نکرد.
- **تنظیمات تازه:** `browser_sharing` (SettingKV، فقط کارفرما).
- **متغیرهای محیطی اختیاری:**
  - `XAGENT_EXTERNAL_PROBE_URL`: کاوشگر عمومی F17.
  - `MCP_REQUEST_TIMEOUT`: agentd.
- **گزینه نصب:** `--repair`.
- **رفتار تازه شروع سرویس:** production با کلید/توکن پیش‌فرض اجرا نمی‌شود. ممکن است نصبی که `.env` آن ناقص است پس از به‌روزرسانی شروع نشود؛ این رفتار عمدی است.
- **CI:** `npm ci` پیش از pytest.
- **قالب‌های تازه داده:**
  - `memory_revisions.after/before.config`: مقادیر `{"$enc"}`.
  - `tool_versions.manifest.exec_config`.
  - `runs.state`: `sensitive_calls` / `effect_intents` / `objective`.
  - `inbox_items.delivery.telegram`: `pending`/`sending`/….
  - `data_dir`: `browser-identities/`، `telegram-spool/`.

## ج) ریسک‌های رگرسیون و امنیت

- **کارمندان و نشست مرورگر:** کارمندانی که از نشست مرورگر کارفرما استفاده می‌کردند باید دوباره وارد شوند، یا کارفرما `browser_sharing` را تنظیم کند.
- **اعتبارسنجی هدف (F07)** ممکن است در جمله‌بندی غیرمعمول هدف را تشخیص ندهد. در این حالت رفتار قبلی برقرار است.
- **سخت‌گیری بیشتر Pipeline:** شکست‌های تازه با علت صریح ممکن است. موارد:
  - سیاست تازه/تأیید باطل → رد؛
  - Capacity → `failed`؛
  - اثر قطع‌شده → «نامعلوم».
- **سرعت:** صفحه بی‌محتوای مرورگر تا ~۲۰ ثانیه منتظر می‌ماند (R1)؛ رزرو بودجه درخت پرس‌وجوی بازگشتی دارد.
- **امنیت:**
  - پاک‌سازی ساختاری خروجی‌ها برای همه نقش‌ها؛
  - مهروموم اسرار در Snapshot؛
  - HMAC برای اثرانگشت تأیید؛
  - تأیید «login» تضعیف نشد؛
  - هیچ سیاست امنیتی برای عبور آزمون‌ها شل نشد.

## چ) پیش‌نیاز استقرار زنده و Rollback امن (فقط با تأیید کارفرما)

1. پیش از استقرار: پشتیبان کامل (`xagent backup`) و اطمینان از کامل بودن `.env`. `XAGENT_SECRET_KEY` و `DESKTOP_TOKEN` باید غیرپیش‌فرض باشند.
2. استقرار فقط با `xagent update` روی همین شاخه، پس از تأیید کارفرما. Migration تازه‌ای وجود ندارد.
3. پس از استقرار:
   - `xagent doctor` (تشخیص لایه‌ای F17)؛
   - `xagent-cli secrets-inventory` و برنامه چرخش اعتبارنامه‌های فهرست‌شده (F01)؛
   - بررسی ورود دوباره کارمندان در مرورگر (F05).
4. **Rollback:** `xagent rollback`. Schema تغییری نکرده است، پس downgrade لازم نیست.
   - داده‌های قالب تازه (مقادیر `$enc` در Snapshotهای تازه، `exec_config`) در نسخه قبلی به‌صورت ناشناخته نادیده گرفته می‌شوند.
   - Undo یک Snapshot مهروموم‌شده در نسخه قبلی، مقدار مهروموم‌شده را بازنمی‌گرداند. پیش از Rollback، Undo ابزارها را در نسخه جدید انجام دهید.
5. **آزمون‌های زنده لازم پیش از ادعای آمادگی تولید:**
   - HTTPS عمومی دامنه/IP با کاوشگر بیرونی؛
   - تلگرام با حساب آزمون جدا (نه حساب واقعی)؛
   - ابزار MCP واقعی؛
   - Rollback به‌روزرسان روی کپی Staging.

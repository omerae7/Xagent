## رفتار اصلاح‌شده

برنامهٔ مدل دیگر نام گروه ابزار یا اقدام آیندهٔ روتین را به شرط نادرست تکمیل تبدیل نمی‌کند. history پیش از clarification بارگذاری می‌شود؛ typing به editable واقعی و مقدار بازخوانی‌شده متکی است. read موفق، شکست UI action را پنهان نمی‌کند؛ recovery عنصر گم‌شده نیازمند تلاش واقعی و مشاهدهٔ تازه است.

ابهام native Telegram پیش از مدل/ارسال بعدی به توقف پایدار می‌انجامد. متن مشابه در history یا cached status بدون receipt دیگر success نیست؛ reconciliation نیازمند message ID ثبت‌شده و outgoing واقعی است. once/mission/standing محدود و قابل لغو با identity، policy revision، expiry و atomic usage اجرا شده‌اند؛ مجوز، proof اثر نیست.

## رابط و عملیات

OS2035 tokens و light/dark، Orbit اختصاصی، Command Center، Chat کاربر راست/ایجنت چپ، وضعیت‌های شواهد، timeline خلاصه/قابل گسترش، history paging، visual viewport و دسترس‌پذیری. سلامت graphical desktop از liveness جداست؛ browser hang باید readiness=503 بدهد. انتخاب Chrome/Chromium و profileهای موجود حفظ شده‌اند.

## اعتبارسنجی

- **۵۴۸ passed، صفر failed، صفر skipped**؛ تمام ۴۶۱ مورد پایه حفظ و ۸۷ مورد اضافه شد.
- پنل واقعی → API → Run → approval → فرم Chromium → یک effect و receipt → پاسخ صادقانه و tab باز.
- ده وقوع Telegram با PostgreSQL/Redis واقعی و model/bridge ساختگی؛ دو reply با receipt، هشت interval خالی با صفر call مدل.
- Chrome/Chromium گرافیکی Docker: reconnect، cookie restart، screenshot، Takeover و hung-browser readiness.
- ۹ عرض صفحه در دو theme، axe WCAG A/AA، XSS، TypeScript/Vite، ruff و shell checks.
- Caddy IP/domain بدون SNI، routing/restart؛ internal CA، public trust محسوب نمی‌شود.

Source آزموده‌شده: [`0dac265d8db1dc24646b709db473f8f3c11d90ed`](https://github.com/omerae7/Xagent/commit/0dac265d8db1dc24646b709db473f8f3c11d90ed). CI دقیق HEAD گزارش در checks همین PR باید جدا بررسی شود.

## سازگاری و حدود

Migration **0008** فقط دو جدول permission اضافه می‌کند؛ هیچ permission قبلی خودکار گسترش نمی‌یابد. downgrade صریح تاریخچهٔ permission جدید را حذف می‌کند؛ قبل از rollback داده‌ها حفظ و هشدار updater بررسی شود. dependency تازه فقط `axe-core@4.14.0` برای تست است.

هیچ VPS، حساب واقعی Telegram یا API پولی استفاده نشد؛ merge/deploy انجام نشده است. private desktop/filesystem/network isolation، verifier عمومی اثرها، secrets تاریخی، dependency advisories و soak زنده همچنان گیت production هستند. این نسخه تأیید unattended production نیست.

گزارش فارسی، تصاویر، محدودیت‌ها، فایل‌های تغییرکرده و دستور دستی VPS در [codex-os2035-engineering.md](https://github.com/omerae7/Xagent/blob/ccr-345833d2-sca9ob/docs/audits/2026-11-05/codex-os2035-engineering.md) و [os2035-verification.txt](https://github.com/omerae7/Xagent/blob/ccr-345833d2-sca9ob/docs/audits/2026-11-05/os2035-verification.txt) ثبت شده‌اند.

**PR باید Draft باقی بماند؛ merge به main یا deployment مجاز نیست.**

fixture مرورگر دیگر guard Takeover را mock نمی‌کند؛ API مالک و Redis واقعی مسدودشدن تایپ را اثبات می‌کنند. checkpoint قبلی 397eec6 در CIهای [206](https://github.com/omerae7/Xagent/actions/runs/42071197477) و [207](https://github.com/omerae7/Xagent/actions/runs/42071209794) موفق بود؛ آخرین HEAD باید checks مستقل خودش را داشته باشد.

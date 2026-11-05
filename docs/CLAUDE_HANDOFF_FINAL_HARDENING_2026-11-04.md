# X Agent — گزارش تحویل به Claude Code (۴ نوامبر ۲۰۲۶)

## محدوده و هویت
- مخزن: `omerae7/Xagent`
- شاخه موجود: `ccr-345833d2-sca9ob`، PR #2 (Draft).
- مبنای این اصلاحات: `d3b7fc91f0d3791775bcc3bbdc3e94b02cd7fff8`.
- **هیچ شاخه جدید، Merge، استقرار روی VPS اصلی یا تماس با API پولی انجام نشده است.**
- کارهای قبلی Claude (۹ اصلاح امنیتی، Host Broker، Dream و ledger مستقل، Migration 0004 و updater) حفظ شده‌اند.

## اصلاحات ثبت‌شده پس از آخرین تحویل Claude

### ۱. Host Broker: کلید HMAC باید Fail-Closed باشد
فایل: `infra/hostd/xagent-hostd.py`
Commit: `fbe87b49`
- مقدار اولیه Cache، کلید خالی محسوب نمی‌شود؛ هر درخواست ابتدا دسترسی به فایل `.env` را بررسی می‌کند.
- در صورت حذف، ناخوانا شدن یا فقدان `XAGENT_SECRET_KEY`، درخواست با `PermissionError` رد می‌شود و کلید Cache پاک می‌شود.
- اثر تغییر فایل با مجموعه `(device, inode, mtime_ns, size)` بررسی می‌شود.
- کلیدهای مشتق‌شده Request و Grant جداگانه باقی مانده‌اند.

### ۲. Host Broker: جلوگیری از SSRF در diag.http
فایل: `infra/hostd/xagent-hostd.py`
Commit: `fbe87b49`
- بررسی همه پاسخ‌های DNS و ردکردن هر IP غیرعمومی، IPv4-mapped، Teredo و 6to4.
- اتصال به IP بررسی‌شده با `curl --resolve` و حفظ hostname اصلی برای SNI و تأیید گواهی TLS.
- اجرای `curl -q --noproxy '*'` برای جلوگیری از اثر فایل تنظیمات و Proxy محیط.
- دنبال نکردن Redirect؛ بدون `-L` و با `--max-redirs 0`.
- هیچ تغییر شبکه‌ای یا فعال‌شدن پورت جدید در VPS ایجاد نشده است.

### ۳. Dream: درخواست دستی در حالت غیرفعال یا STOP_ALL
فایل‌ها: `services/api/xagent_api/routers/memory.py`، `apps/web/src/pages/Dream.tsx`
Commitها: `6bddd444`، `f34278b8`
- API پیش از قرار دادن فرمان در صف، وضعیت STOP_ALL و تنظیمات `DreamSettings.enabled` را بررسی می‌کند.
- پاسخ HTTP 409 با توضیح فارسی برمی‌گردد؛ درخواست نادرست دیگر با پیام موفقیت کاذب ثبت نمی‌شود.
- متن حالت خالی پنل، غیرفعال‌بودن پیش‌فرض Dream و فعال‌سازی لازم را توضیح می‌دهد.
- هیچ تغییری در اصل محدودیت‌های مستقل هزینه، توکن، زمان یا شناسه شبانه Dream انجام نشده است.

### ۴. updater: Dry Run گزارش قبلی را خراب نمی‌کند
فایل: `infra/updater/xagent-update.sh`
Commit: `2565cb74`
- Dry Run هیچ فایل `update.status` یا `update.log` را بازنویسی نمی‌کند.
- `update.request` را حذف نمی‌کند و پوشه‌های Backup/Control ایجاد نمی‌کند.
- در صورت وجود فایل Lock، قفل Shared غیرمسدودکننده برای جلوگیری از تداخل با آپدیت واقعی می‌گیرد.
- عملیات Docker/DB/Deployment اجرا نمی‌کند.
- **استثناء صریح:** `git fetch` هنوز remote-tracking metadata مخزن محلی را تغییر می‌دهد. بنابراین Dry Run از نظر اپلیکیشن بدون استقرار است، نه مطلقاً بدون هرگونه Write.

## تست‌های جدید
- `tests/test_final_hardening.py`: رد کلید گمشده و حذف‌شده پس از Cache، DNS عمومی و خصوصی و ترکیبی، محافظت curl، رد Dream دستی در حالت غیرفعال/توقف اضطراری.
- `tests/updater/e2e.sh`: مقایسه دقیق فایل‌های `update.status` و `update.log` قبل و بعد از Dry Run.
- تنظیم Fixture نوشتن خط جدید: `2d81ea4e`.
- مستندسازی رفتار جدید: `README.md` در `c7a1838f`.

## وضعیت آزمون‌ها و استقرار
- مبنای قبل از این اصلاحات در GitHub Actions: `101 passed`، lint/typecheck/build موفق، و E2E Docker موفق.
- **وضعیت نهایی را از آخرین Commit شاخه و CI مربوط به همان SHA در PR #2 بخوان؛ نتایج Commit قبلی جایگزین آن نیستند.**
- تست مستقل همه وابستگی‌ها و اجرای Docker روی VPS اصلی انجام نشده است.
- راه‌اندازی واقعی ارائه‌دهندگان پولی، STT/TTS و استقرار VPS نیاز به اعتبارسنجی مجزا و اجازه مالک دارد.

## مواردی که Claude هنگام بازگشت باید بررسی کند
1. آخرین Commit، وضعیت CI و هر خطای احتمالی آزمایش‌های جدید را بخوان.
2. اگر خطایی وجود داشت، فقط همان مسیر را بدون تغییر طراحی تثبیت‌شده رفع و دوباره تست کن.
3. DNS resolver و `curl --resolve` را در Ubuntu/Debian VPS واقعی با hostname عمومی، private، IPv6، Redirect و Proxy سفارشی تست کن.
4. دسترسی `SO_PEERCRED`، کلید میزبان و تغییر یا حذف `.env` در systemd واقعی را تأیید کن.
5. رزرو بودجه و تطبیق OpenAI CSV با Usage Ledger را با داده واقعی *فقط با اجازه مالک* تست کن.
6. Backups، Rollback و restore در محیط staging را تست کن، نه سرور اصلی.
7. متن README درباره تعداد تست قدیمی ۸۹ را پس از شمارش جدید اصلاح کن.
8. PR #2 را Draft نگه دار؛ بدون دستور مالک Merge یا Deploy نکن.

## دستورهای بررسی و استقرار مشروط
```bash
sudo xagent version
sudo xagent status
sudo xagent update --ref ccr-345833d2-sca9ob --dry-run
```
فقط پس از موفقیت CI **آخرین Commit**، تست staging، تأیید سلامت سرور و پشتیبان:
```bash
sudo xagent update --ref ccr-345833d2-sca9ob
sudo xagent status
sudo xagent version
sudo xagent doctor
```
برای نتیجه قابل‌تکرار به‌جای شاخه شناور از `--ref <verified-full-commit-sha>` استفاده شود.

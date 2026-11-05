"""ممیزی جرم‌شناختی — مرحله ۶، F13: نصب‌کننده روی نصب موجود به‌طور پیش‌فرض مخرب نیست.

نصب‌کننده واقعی با PATH حاوی «Shim» اجرا می‌شود: هر ابزار تغییر سیستم (apt-get، docker، systemctl، ufw، …)
فقط فراخوانی را ثبت و فوراً با خطا خارج می‌شود؛ هیچ تغییر سیستمی واقعی ممکن نیست. توابع تعمیر از همان اسکریپت
استخراج و روی مخزن git موقت آزموده می‌شوند.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
SYSTEM_TOOLS = ["apt-get", "apt", "docker", "systemctl", "ufw", "swapon", "fallocate", "mkswap", "curl", "chown",
                "install", "ln", "sysctl", "usermod", "openssl"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout


def _existing_install(tmp: Path) -> Path:
    home = tmp / "xagent"
    app = home / "app"
    app.mkdir(parents=True)
    _git(app, "init", "-q")
    (app / "README.md").write_text("base\n")
    _git(app, "add", "-A")
    _git(app, "commit", "-qm", "base")
    (app / "README.md").write_text("base\nlocal-operator-edit\n")          # تغییر محلی
    (app / "operator-notes.txt").write_text("keep me\n")                   # فایل محلی جدید
    (app / ".env").write_text("XAGENT_SECRET_KEY=sentinel-secret\nXAGENT_DEFAULT_SNI=sentinel.example\nCUSTOM=keep\n")
    (home / "pgdata").mkdir()
    (home / "pgdata" / "PG_VERSION").write_text("16\n")                    # داده موجود
    return home


def _shims(tmp: Path) -> tuple[Path, Path]:
    d, log = tmp / "shims", tmp / "shim-calls.log"
    d.mkdir()
    for t in SYSTEM_TOOLS:
        (d / t).write_text(f'#!/bin/sh\necho "{t} $*" >> "{log}"\nexit 97\n')
        (d / t).chmod(0o755)
    return d, log


def _digest(home: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(home.rglob("*")):
        if p.is_file() and ".git/" not in str(p.relative_to(home)) + "/":
            h.update(str(p.relative_to(home)).encode() + p.read_bytes())
    return h.hexdigest()


def test_F13_installer_refuses_existing_installation_without_side_effects(tmp_path):
    home = _existing_install(tmp_path)
    shims, log = _shims(tmp_path)
    before, head = _digest(home), _git(home / "app", "rev-parse", "HEAD")
    env = {**os.environ, "PATH": f"{shims}:{os.environ['PATH']}"}
    res = subprocess.run(["bash", str(INSTALL), "--dir", str(home), "--no-firewall"], env=env, capture_output=True,
                         text=True, timeout=60)
    assert res.returncode != 0, "نصب‌کننده روی نصب موجود بدون --repair ادامه داد"
    assert "نصب موجود" in res.stderr, res.stderr[-400:]
    assert not log.exists(), f"پیش از رد، ابزار تغییر سیستم فراخوانی شد: {log.read_text() if log.exists() else ''}"
    assert _digest(home) == before and _git(home / "app", "rev-parse", "HEAD") == head
    assert "local-operator-edit" in (home / "app" / "README.md").read_text()
    assert "sentinel.example" in (home / "app" / ".env").read_text()


def test_F13_installer_has_no_destructive_reset_or_env_overwrite_path():
    body = INSTALL.read_text()
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    assert "reset --hard" not in code, "نصب‌کننده هنوز git reset --hard اجرا می‌کند"
    assert 'cat > "$ENV_FILE"' not in code, "نصب‌کننده هنوز .env موجود را مستقیم بازنویسی می‌کند"


def _repair_functions() -> str:
    m = re.search(r"# >>> repair_functions.*?# <<< repair_functions", INSTALL.read_text(), re.S)
    assert m, "توابع تعمیر حافظ داده در نصب‌کننده وجود ندارد"
    return m.group(0)


def test_F13_repair_mode_preserves_local_changes_and_configuration(tmp_path):
    home = _existing_install(tmp_path)
    app = home / "app"
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(app), str(origin)], check=True)
    _git(app, "remote", "add", "origin", str(origin))
    work = tmp_path / "upstream"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    (work / "NEW.md").write_text("upstream change\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "upstream")
    branch = _git(work, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _git(work, "push", "-q", "origin", branch)
    new_env = tmp_path / "generated.env"
    new_env.write_text("XAGENT_SECRET_KEY=would-overwrite\nXAGENT_DEFAULT_SNI=\nNEW_KEY=added\n")
    script = f'''
set -Eeuo pipefail
XAGENT_HOME="{home}"; APP="{app}"; BRANCH="{branch}"; ENV_FILE="{app}/.env"
ok() {{ echo "ok $*"; }}; warn() {{ echo "warn $*"; }}; die() {{ echo "die $*" >&2; exit 1; }}
{_repair_functions()}
repair_code
repair_env "{new_env}"
'''
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr + res.stdout
    env_text = (app / ".env").read_text()
    assert "XAGENT_SECRET_KEY=sentinel-secret" in env_text and "would-overwrite" not in env_text
    assert "XAGENT_DEFAULT_SNI=sentinel.example" in env_text and "CUSTOM=keep" in env_text and "NEW_KEY=added" in env_text
    assert list((home / "backups").glob("env-*.bak")), "پشتیبان .env ساخته نشد"
    patch = next((home / "backups").glob("repair-local-*.patch")).read_text()
    assert "local-operator-edit" in patch
    assert "xagent-repair-" in _git(app, "stash", "list"), "تغییرات محلی در stash حفظ نشد"
    assert (app / "NEW.md").exists(), "نسخه upstream به‌صورت fast-forward اعمال نشد"

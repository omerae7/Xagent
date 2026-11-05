"""Disposable graphical desktop integration; only synthetic local pages and profiles.

Run with Python + Playwright: python tests/desktop_browser.py --image xagent/desktop:latest
Creates only uniquely named test containers/volumes; no production Compose project.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid

from playwright.async_api import async_playwright


def docker(*args, stdin=None):
    return subprocess.run(['docker', *args], input=stdin, text=True, check=True,
                          capture_output=True).stdout.strip()


def request(url, token=None, data=None):
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    r = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    # Never send local test credentials through an environment HTTP proxy.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=5) as response:
        return json.load(response)


async def check(image, choice):
    name = f'xa-browser-e2e-{uuid.uuid4().hex[:10]}'
    token = f'synthetic-{uuid.uuid4().hex}'
    volume = f'{name}-home'
    created = False
    docker('volume', 'create', volume)
    started = time.monotonic()
    try:
        docker('run', '-d', '--name', name, '--shm-size=512m', '--memory=2560m',
               '-e', f'DESKTOP_TOKEN={token}', '-e', f'XAGENT_BROWSER={choice}',
               '-v', f'{volume}:/home/agent', '-p', '127.0.0.1::7000', '-p', '127.0.0.1::9222', image)
        created = True
        info = json.loads(docker('inspect', name))[0]
        def port(p):
            entry = info['NetworkSettings']['Ports'][p][0]
            assert entry['HostIp'] == '127.0.0.1'
            return int(entry['HostPort'])
        api, cdp = f'http://127.0.0.1:{port("7000/tcp")}', f'http://127.0.0.1:{port("9222/tcp")}'
        async def ready():
            for _ in range(90):
                try:
                    h = request(api + '/health')
                    if h.get('browser_cdp') and h.get('browser_version'):
                        return h
                except (OSError, ValueError):
                    pass
                await asyncio.sleep(1)
            raise AssertionError('desktop/browser not ready: ' + docker('logs', '--tail', '30', name))
        health = await ready()
        assert health.get('desktop_ready') is True and health.get('display_ready') is True
        assert request(api + '/ready')['desktop_ready'] is True
        startup = round(time.monotonic() - started, 3)
        html = '<title>Synthetic persistent browser</title><h1>Local test only</h1><input id="draft">'
        docker('exec', '-i', name, 'python3', '-c',
               'import pathlib,sys; pathlib.Path("/tmp/index.html").write_text(sys.stdin.read())', stdin=html)
        docker('exec', '-d', name, 'python3', '-m', 'http.server', '17777', '--bind', '127.0.0.1', '--directory', '/tmp')
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(cdp)
            page = await browser.contexts[0].new_page()
            await page.goto('http://127.0.0.1:17777/index.html')
            await page.bring_to_front()
            await page.fill('#draft', 'retained draft')
            await page.evaluate("document.cookie='synthetic=retained; max-age=86400; path=/'")
            await browser.close()  # disconnect client; never terminate the personal browser
            browser = await pw.chromium.connect_over_cdp(cdp)
            page = next(p for p in browser.contexts[0].pages if '/index.html' in p.url)
            assert await page.input_value('#draft') == 'retained draft'
            assert 'synthetic=retained' in await page.evaluate('document.cookie')
            shot = request(api + '/screenshot', token, {})
            assert len(base64.b64decode(shot['png_b64'])) > 1000
            request(api + '/takeover', token, {'on': True})
            try:
                request(api + '/input', token, {'action': 'type', 'text': 'must not be entered'})
                raise AssertionError('takeover allowed competing input')
            except urllib.error.HTTPError as e:
                assert e.code == 423
            request(api + '/takeover', token, {'on': False})
            assert await page.input_value('#draft') == 'retained draft'
            # A running but hung browser must fail readiness while agentd stays live.
            process_name = 'chrome' if choice == 'chrome' else 'chromium'
            docker('exec', name, 'pkill', '-STOP', '-x', process_name)
            try:
                assert request(api + '/health')['ok'] is True
                try:
                    request(api + '/ready')
                    raise AssertionError('hung graphical browser was reported ready')
                except urllib.error.HTTPError as e:
                    assert e.code == 503
            finally:
                docker('exec', name, 'pkill', '-CONT', '-x', process_name)
            # Persist the profile before a graceful container restart.
            await page.context.storage_state()
            # Chrome batches cookie writes. Wait for its own durable store; a
            # freshly set cookie in RAM is not evidence of crash durability.
            durable = False
            probe = ('import glob,sqlite3; paths=glob.glob("/home/agent/.config/xagent-*/Default/**/Cookies", recursive=True); '
                     'print(sum(sqlite3.connect(p).execute("select count(*) from cookies where name=?", ("synthetic",)).fetchone()[0] for p in paths))')
            for _ in range(60):
                if int(docker('exec', name, 'python3', '-c', probe) or 0):
                    durable = True
                    break
                await asyncio.sleep(1)
            assert durable, 'synthetic cookie never reached the persistent profile'
            await browser.close()
            # Rootless Docker without delegated cgroups can report host-wide
            # memory/CPU. Measure only processes inside this container's PID namespace.
            stats = json.loads(docker('exec', name, 'python3', '-c',
                'import json,subprocess; rows=subprocess.check_output(["ps","-eo","comm=,rss=,pcpu="],text=True); '
                'browser=[line.split() for line in rows.splitlines() if line.split()[0].startswith(("chrome","chromium"))]; '
                'print(json.dumps({"rss_kib_sum":sum(int(r[1]) for r in browser),'
                '"lifetime_cpu_percent_sum":sum(float(r[2]) for r in browser),"processes":len(browser)}))'))
            docker('restart', '--time', '15', name)
            # Docker may reallocate automatically published ephemeral host ports.
            info = json.loads(docker('inspect', name))[0]
            api, cdp = f'http://127.0.0.1:{port("7000/tcp")}', f'http://127.0.0.1:{port("9222/tcp")}'
            await ready()
            browser = await pw.chromium.connect_over_cdp(cdp)
            cookies = await browser.contexts[0].cookies('http://127.0.0.1:17777')
            assert any(c['name'] == 'synthetic' and c['value'] == 'retained' for c in cookies)
            await browser.close()
        return {'choice': choice, 'health': health, 'startup_seconds': startup,
                'single_sample_process_stats': stats,
                'checks': ['graphical CDP', 'draft survives reconnect', 'cookie survives restart',
                           'actual desktop screenshot', 'takeover rejects input', 'loopback test ports',
                           'hung browser fails readiness while daemon stays live']}
    finally:
        if created:
            docker('rm', '-f', name)
        docker('volume', 'rm', volume)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='xagent/desktop:latest')
    parser.add_argument('--report')
    args = parser.parse_args()
    results = [await check(args.image, choice) for choice in ('chrome', 'chromium')]
    output = json.dumps(results, indent=2, ensure_ascii=False)
    if args.report:
        from pathlib import Path
        Path(args.report).write_text(output + '\n')
    print(output)


if __name__ == '__main__':
    asyncio.run(main())

# -*- coding: utf-8 -*-
"""One-time hotfix deploy: upload views.py + settings.py to production and restart gunicorn."""
import os
import sys
import time
import paramiko

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HOST = '169.58.154.191'
USER = 'root'
PROJECT_DIR = '/var/www/maorif_zafarobod'
PY = f'{PROJECT_DIR}/venv/bin/python'
BASE = os.path.dirname(os.path.abspath(__file__))
password = os.environ['SSH_PASSWORD']

FILES = [
    ('portal/views.py', f'{PROJECT_DIR}/portal/views.py'),
    ('maorif_portal/settings.py', f'{PROJECT_DIR}/maorif_portal/settings.py'),
]


def run(client, cmd, timeout=120):
    print(f'$ {cmd}', flush=True)
    i, o, e = client.exec_command(cmd, timeout=timeout)
    code = o.channel.recv_exit_status()
    out = o.read().decode('utf-8', 'replace')
    err = e.read().decode('utf-8', 'replace')
    if out:
        print(out, end='', flush=True)
    if err.strip():
        print('STDERR:', err, end='', flush=True)
    return code


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f'Connecting to {HOST} ...', flush=True)
    c.connect(HOST, username=USER, password=password, timeout=20)
    print('Connected.', flush=True)

    ts = int(time.time())
    for local_rel, remote in FILES:
        run(c, f'cp -p {remote} {remote}.bak_{ts}')

    sftp = c.open_sftp()
    for local_rel, remote in FILES:
        sftp.put(os.path.join(BASE, local_rel), remote)
        print(f'Uploaded {local_rel} -> {remote}', flush=True)
    sftp.close()

    code = run(c, f'cd {PROJECT_DIR} && {PY} manage.py check')
    if code != 0:
        print('manage.py check FAILED — rolling back files', file=sys.stderr)
        for local_rel, remote in FILES:
            run(c, f'cp -p {remote}.bak_{ts} {remote}')
        c.close()
        return 1

    print('\n--- Restarting gunicorn ---', flush=True)
    run(c, 'pkill -f gunicorn || true')
    time.sleep(1)
    code = run(c, f'cd {PROJECT_DIR} && {PROJECT_DIR}/venv/bin/gunicorn --workers 3 --bind 127.0.0.1:8000 maorif_portal.wsgi:application --daemon')
    if code != 0:
        print('Gunicorn restart failed!', file=sys.stderr)
        c.close()
        return 1
    time.sleep(1)
    run(c, "ps aux | grep '[g]unicorn' | head -5")
    run(c, "curl -s -o /dev/null -w 'HTTP %{http_code}\\n' http://127.0.0.1:8000/login/")

    c.close()
    print('\nHotfix deploy complete.', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

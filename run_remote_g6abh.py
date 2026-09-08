# -*- coding: utf-8 -*-
"""One-time remote runner: upload settings.py + sync_grade6_abh.py, execute, restart gunicorn."""
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


def run(client, cmd, timeout=300):
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
    run(c, f'cp -p {PROJECT_DIR}/maorif_portal/settings.py {PROJECT_DIR}/maorif_portal/settings.py.bak_{ts}')
    run(c, f'cp -p {PROJECT_DIR}/db.sqlite3 {PROJECT_DIR}/db.sqlite3.bak_sync_grade6_abh')

    sftp = c.open_sftp()
    sftp.put(os.path.join(BASE, 'maorif_portal', 'settings.py'), f'{PROJECT_DIR}/maorif_portal/settings.py')
    print('Uploaded settings.py', flush=True)
    sftp.put(os.path.join(BASE, 'sync_grade6_abh.py'), f'{PROJECT_DIR}/sync_grade6_abh.py')
    print('Uploaded sync_grade6_abh.py', flush=True)
    sftp.close()

    code = run(c, f'cd {PROJECT_DIR} && {PY} manage.py check')
    if code != 0:
        print('manage.py check FAILED вЂ” rolling back settings.py', file=sys.stderr)
        run(c, f'cp -p {PROJECT_DIR}/maorif_portal/settings.py.bak_{ts} {PROJECT_DIR}/maorif_portal/settings.py')
        c.close()
        return 1

    run(c, f'cd {PROJECT_DIR} && {PY} sync_grade6_abh.py')
    run(c, f'rm -f {PROJECT_DIR}/sync_grade6_abh.py')

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
    print('\nDone.', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

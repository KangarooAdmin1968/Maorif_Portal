#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Maorif Portal remote deployment helper."""
import os
import sys
import getpass
import time
import paramiko

HOST = '169.58.154.191'
USER = 'root'
PROJECT_DIR = '/var/www/maorif_zafarobod'
VENV = f'{PROJECT_DIR}/venv/bin'
VENV_GUNICORN = f'{VENV}/gunicorn'
VENV_PYTHON = f'{VENV}/python'
DATA_FILES = ['db.sqlite3', 'schools.db', 'data/school.db']


def run(client, cmd, timeout=60):
    print(f'$ {cmd}', flush=True)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    if out:
        print(out, end='', flush=True)
    if err:
        print(f'STDERR: {err}', end='', flush=True)
    return exit_status, out, err


def check_git(client):
    cmd = f'test -d {PROJECT_DIR}/.git && echo "yes" || echo "no"'
    _, out, _ = run(client, cmd)
    return out.strip() == 'yes'


def main():
    password = os.environ.get('SSH_PASSWORD')
    if not password:
        password = getpass.getpass(f'Enter root password for {HOST}: ')

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f'Connecting to {HOST} as {USER} ...', flush=True)
    try:
        client.connect(HOST, username=USER, password=password, timeout=20)
    except Exception as e:
        print(f'Connection failed: {e}', file=sys.stderr)
        return 1
    print(f'Connected to {HOST} as {USER}.', flush=True)

    # Diagnostic: find where the project is
    print('\n--- Diagnostic: project location ---', flush=True)
    run(client, f'ls -ld {PROJECT_DIR}')
    run(client, f'ps aux | grep "[g]unicorn"')
    run(client, f'cd {PROJECT_DIR} && pwd && git remote -v 2>/dev/null || echo "Git remote not configured"')

    # Ensure git is configured
    print('\n--- Git setup / update ---', flush=True)
    if not check_git(client):
        repo_url = os.environ.get('REPO_URL')
        if not repo_url:
            repo_url = input('Git repository URL: ').strip()
        run(client, f'cd {PROJECT_DIR} && git init && git remote add origin {repo_url}')

    # Backup local data files before a hard reset
    print('\n--- Backing up local data files ---', flush=True)
    backup_dir = f'/root/maorif_backups_{int(time.time())}'
    run(client, f'mkdir -p {backup_dir}')
    for f in DATA_FILES:
        src = f'{PROJECT_DIR}/{f}'
        run(client, f'test -f {src} && cp -p {src} {backup_dir}/ || echo "not found: {f}"')

    # Hard reset to origin/main to ensure all latest templates/views are applied
    print('\n--- Pulling latest code from origin/main ---', flush=True)
    run(client, f'cd {PROJECT_DIR} && git fetch origin main')
    exit_code, _, _ = run(client, f'cd {PROJECT_DIR} && git checkout -f -B main origin/main')
    if exit_code != 0:
        print('git checkout of origin/main failed. Aborting.', file=sys.stderr)
        client.close()
        return 1

    # Restore local data files
    print('\n--- Restoring local data files ---', flush=True)
    for f in DATA_FILES:
        backup = f'{backup_dir}/{f}'
        dst = f'{PROJECT_DIR}/{f}'
        run(client, f'test -f {backup} && cp -p {backup} {dst} || echo "no backup for {f}"')

    # Ensure venv uses the system python and install dependencies
    print('\n--- Preparing Python environment ---', flush=True)
    pyvenv_cfg = f'{PROJECT_DIR}/venv/pyvenv.cfg'
    run(client, f'python3 --version')
    run(client, f'cat > {pyvenv_cfg} <<EOF\nhome = /usr/bin\ninclude-system-site-packages = false\nversion = 3.12.3\nEOF')
    run(client, f'cd {PROJECT_DIR} && {VENV_PYTHON} -m pip --version 2>/dev/null || {VENV_PYTHON} -m ensurepip && {VENV_PYTHON} -m pip install --upgrade pip')
    run(client, f'cd {PROJECT_DIR} && {VENV_PYTHON} -m pip install -r {PROJECT_DIR}/requirements.txt')

    # Apply any new Django migrations
    print('\n--- Applying Django migrations ---', flush=True)
    run(client, f'cd {PROJECT_DIR} && {VENV_PYTHON} manage.py migrate')

    # SQLite WAL: lets readers proceed while a grade write holds the single
    # writer lock — safe here (single host, local disk, short atomic
    # transactions). Idempotent; persists in the DB file header once set.
    print('\n--- Enabling SQLite WAL mode (idempotent) ---', flush=True)
    run(client, f'cd {PROJECT_DIR} && {VENV_PYTHON} -c "import sqlite3; '
                f'c = sqlite3.connect(\'db.sqlite3\'); '
                f'print(\'journal_mode:\', c.execute(\'PRAGMA journal_mode=WAL\').fetchone()[0]); '
                f'c.close()"')

    # Static assets (needed by nginx: /sw.js is served from staticfiles now)
    print('\n--- Collecting static ---', flush=True)
    run(client, f'cd {PROJECT_DIR} && {VENV_PYTHON} manage.py collectstatic --noinput')

    # Gunicorn logs + logrotate + nginx site config (timed log format,
    # /sw.js served statically). Installed before the service restart below.
    print('\n--- Installing ops config (logs, logrotate, nginx) ---', flush=True)
    run(client, 'mkdir -p /var/log/gunicorn')
    sftp = client.open_sftp()
    sftp.put(f'{os.path.dirname(os.path.abspath(__file__))}/deploy/gunicorn.service',
             '/etc/systemd/system/gunicorn.service')
    sftp.put(f'{os.path.dirname(os.path.abspath(__file__))}/deploy/gunicorn-logrotate',
             '/etc/logrotate.d/gunicorn')
    sftp.put(f'{os.path.dirname(os.path.abspath(__file__))}/deploy/nginx-maorif_zafarobod.conf',
             '/etc/nginx/sites-available/maorif_zafarobod')
    sftp.close()
    run(client, 'nginx -t')

    # Align director/zavuch usernames if explicitly requested
    if os.environ.get('UPDATE_USERNAMES'):
        print('\n--- Aligning usernames ---', flush=True)
        run(client, f'cd {PROJECT_DIR} && {VENV_PYTHON} update_usernames.py')

    # Restart Gunicorn under systemd (auto-restart, journald logs, no stray
    # daemon). fuser -k clears any manually started process holding :8000.
    print('\n--- Restarting Gunicorn (systemd) ---', flush=True)
    run(client, 'fuser -k 8000/tcp || true')
    time.sleep(1)
    run(client, 'systemctl daemon-reload')
    run(client, 'systemctl enable gunicorn')
    exit_code, _, _ = run(client, 'systemctl restart gunicorn')
    if exit_code != 0:
        print('Gunicorn restart failed.', file=sys.stderr)
        run(client, 'journalctl -u gunicorn --no-pager -n 20')
        client.close()
        return 1

    time.sleep(1)

    # Restart nginx
    print('\n--- Restarting Nginx ---', flush=True)
    exit_code, _, _ = run(client, 'systemctl restart nginx')
    if exit_code != 0:
        print('Nginx restart failed.', file=sys.stderr)
        client.close()
        return 1

    # Verify
    print('\n--- Verification ---', flush=True)
    run(client, 'ps aux | grep "[g]unicorn"')
    run(client, 'date')
    print('\nDeployment completed successfully.', flush=True)

    # Generate and download master keys table
    print('\n--- Generating master keys table ---', flush=True)
    remote_master = '/tmp/Zafarobod_Maorif_Master_Keys.html'
    run(client, f'cd {PROJECT_DIR} && {VENV_PYTHON} generate_master_keys.py --output {remote_master}')
    local_master = r'D:\Loihalar\Zafarobod_Maorif_Master_Keys.html'
    try:
        sftp = client.open_sftp()
        sftp.get(remote_master, local_master)
        sftp.close()
        print(f'Master keys downloaded to {local_master}', flush=True)
    except Exception as exc:
        print(f'Could not download master keys: {exc}', file=sys.stderr)

    client.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())

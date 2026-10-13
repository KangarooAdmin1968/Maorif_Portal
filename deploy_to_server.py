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

    # Backup local data files before a hard reset. SQLite's online backup
    # API is WAL-safe (a plain cp can capture a torn copy while gunicorn
    # writes). Each backup is validated with quick_check; any failure aborts
    # the deploy before code changes. mkdir (no -p) aborts rather than reuse
    # an existing backup dir; backups are never deleted automatically.
    print('\n--- Backing up local data files ---', flush=True)
    backup_dir = f'/root/maorif_backups_{int(time.time())}'
    exit_code, _, _ = run(client, f'mkdir {backup_dir}')
    if exit_code != 0:
        print('Could not create unique backup directory. Aborting.', file=sys.stderr)
        client.close()
        return 1
    for f in DATA_FILES:
        src = f'{PROJECT_DIR}/{f}'
        dst = f'{backup_dir}/{os.path.basename(f)}'
        bk = (
            f'test ! -f {src} || {VENV_PYTHON} -c "'
            f"import sqlite3; s = sqlite3.connect('{src}'); "
            f"d = sqlite3.connect('{dst}'); s.backup(d); d.close(); s.close()\""
        )
        qc = (
            f'test ! -f {dst} || {VENV_PYTHON} -c "'
            f"import sqlite3; c = sqlite3.connect('{dst}'); "
            f"ok = c.execute('PRAGMA quick_check').fetchone()[0]; c.close(); "
            f"print(ok); raise SystemExit(0 if ok == 'ok' else 1)\""
        )
        exit_code, _, _ = run(client, bk)
        if exit_code != 0:
            print(f'Backup failed for {f}. Aborting.', file=sys.stderr)
            client.close()
            return 1
        exit_code, _, _ = run(client, qc)
        if exit_code != 0:
            print(f'Backup validation failed for {f}. Aborting.', file=sys.stderr)
            client.close()
            return 1
    run(client, f'cd {backup_dir} && for f in *.sqlite3 *.db; do test -f "$f" && sha256sum "$f" | tee -a sha256sums.txt; done 2>/dev/null || true')

    # Capture the pre-deploy revision for code-only rollback. Rolling back
    # code never restores the database and never affects the running
    # gunicorn process until a restart; old-code/schema compatibility is
    # proven only for the additive migrations reviewed in this release.
    _, old_sha, _ = run(client, f'cd {PROJECT_DIR} && git rev-parse HEAD')
    old_sha = old_sha.strip()
    if not old_sha:
        print('Could not read pre-deploy revision. Aborting.', file=sys.stderr)
        client.close()
        return 1

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
    exit_code, _, _ = run(client, f'cd {PROJECT_DIR} && {VENV_PYTHON} -m pip install -r {PROJECT_DIR}/requirements.txt')
    if exit_code != 0:
        print('Dependency install failed. Aborting.', file=sys.stderr)
        client.close()
        return 1

    # Apply any new Django migrations. On failure, roll code back to the
    # pre-deploy revision so disk matches the running process; the database
    # is left at its last applied migration and is never auto-restored.
    print('\n--- Applying Django migrations ---', flush=True)
    exit_code, _, _ = run(client, f'cd {PROJECT_DIR} && {VENV_PYTHON} manage.py migrate')
    if exit_code != 0:
        print('Migration failed — rolling code back to previous revision.',
              file=sys.stderr)
        exit_code, _, _ = run(
            client, f'cd {PROJECT_DIR} && git checkout -f -B main {old_sha}')
        if exit_code != 0:
            print('ROLLBACK FAILED — MANUAL INTERVENTION REQUIRED.',
                  file=sys.stderr)
            client.close()
            return 2
        client.close()
        return 1

    # SQLite WAL: lets readers proceed while a grade write holds the single
    # writer lock — safe here (single host, local disk, short atomic
    # transactions). Idempotent; persists in the DB file header once set.
    print('\n--- Enabling SQLite WAL mode (idempotent) ---', flush=True)
    exit_code, _, _ = run(client, f'cd {PROJECT_DIR} && {VENV_PYTHON} -c "import sqlite3; '
                f'c = sqlite3.connect(\'db.sqlite3\'); '
                f'print(\'journal_mode:\', c.execute(\'PRAGMA journal_mode=WAL\').fetchone()[0]); '
                f'c.close()"')
    if exit_code != 0:
        print('WAL pragma failed. Aborting.', file=sys.stderr)
        client.close()
        return 1

    # Static assets (needed by nginx: /sw.js is served from staticfiles now)
    print('\n--- Collecting static ---', flush=True)
    exit_code, _, _ = run(client, f'cd {PROJECT_DIR} && {VENV_PYTHON} manage.py collectstatic --noinput')
    if exit_code != 0:
        print('collectstatic failed. Aborting.', file=sys.stderr)
        client.close()
        return 1

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
    exit_code, _, _ = run(client, 'nginx -t')
    if exit_code != 0:
        print('nginx config test failed. Aborting before any restart.', file=sys.stderr)
        client.close()
        return 1

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

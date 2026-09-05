# -*- coding: utf-8 -*-
"""One-time remote runner: upload assign_pe_grade2.py to production and execute it."""
import os
import sys
import paramiko

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

HOST = '169.58.154.191'
USER = 'root'
PROJECT_DIR = '/var/www/maorif_zafarobod'
VENV_PYTHON = f'{PROJECT_DIR}/venv/bin/python'
LOCAL_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assign_pe_grade2.py')
REMOTE_SCRIPT = f'{PROJECT_DIR}/assign_pe_grade2.py'


def run(client, cmd, timeout=120):
    print(f'$ {cmd}', flush=True)
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    if out:
        print(out, end='', flush=True)
    if err:
        print(f'STDERR: {err}', end='', flush=True)
    return exit_status


def main():
    password = os.environ.get('SSH_PASSWORD')
    if not password:
        print('SSH_PASSWORD env var is required', file=sys.stderr)
        return 1

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f'Connecting to {HOST} as {USER} ...', flush=True)
    try:
        client.connect(HOST, username=USER, password=password, timeout=20)
    except Exception as e:
        print(f'Connection failed: {e}', file=sys.stderr)
        return 1
    print('Connected.', flush=True)

    # Backup the database before the data change
    run(client, f'cp -p {PROJECT_DIR}/db.sqlite3 {PROJECT_DIR}/db.sqlite3.bak_pe_grade2')

    # Upload the script
    try:
        sftp = client.open_sftp()
        sftp.put(LOCAL_SCRIPT, REMOTE_SCRIPT)
        sftp.close()
        print(f'Uploaded {LOCAL_SCRIPT} -> {REMOTE_SCRIPT}', flush=True)
    except Exception as exc:
        print(f'Upload failed: {exc}', file=sys.stderr)
        client.close()
        return 1

    # Django system check, then run the assignment
    run(client, f'cd {PROJECT_DIR} && {VENV_PYTHON} manage.py check')
    code = run(client, f'cd {PROJECT_DIR} && {VENV_PYTHON} assign_pe_grade2.py')

    # Cleanup the temp script on the server
    run(client, f'rm -f {REMOTE_SCRIPT}')

    client.close()
    return code


if __name__ == '__main__':
    sys.exit(main())

import os, sys, paramiko
sys.stdout.reconfigure(encoding='utf-8')
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('169.58.154.191', username='root', password=os.environ['SSH_PASSWORD'], timeout=20)
i, o, e = c.exec_command("curl -s -o /dev/null -w 'readiness: %{http_code} redirect=%{redirect_url}' http://127.0.0.1:8000/schools/readiness/")
print(o.read().decode())
c.close()

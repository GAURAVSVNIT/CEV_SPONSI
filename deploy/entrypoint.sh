#!/bin/sh
# Make the mounted data directory writable, then drop root for good.
set -e
if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data
  chown -R sponsy:sponsy /app/data 2>/dev/null || true
  exec python -c 'import os, pwd, sys
u = pwd.getpwnam("sponsy")
os.setgroups([])
os.setgid(u.pw_gid)
os.setuid(u.pw_uid)
os.environ["HOME"] = u.pw_dir
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
fi
exec "$@"

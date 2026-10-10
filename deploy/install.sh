#!/usr/bin/env bash
# Install or upgrade Paperless Ask as a systemd service (Debian/Ubuntu and similar).
# Run from a checkout of this repository:  sudo ./deploy/install.sh
# Idempotent. Never overwrites an existing /etc/paperless-ask/env.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo $0)" >&2; exit 1; }
SRC=$(cd "$(dirname "$0")/.." && pwd)
PREFIX=/opt/paperless-ask

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python 3.10+ is required" >&2; exit 1; }
python3 -m venv --help >/dev/null 2>&1 || { echo "python3-venv is required (apt install python3-venv)" >&2; exit 1; }

id paperless-ask >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin paperless-ask
mkdir -p "$PREFIX" /etc/paperless-ask
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --disable-pip-version-check --upgrade "$SRC"

if [ ! -f /etc/paperless-ask/env ]; then
  sed -e 's#^OLLAMA_URL=http://ollama:11434#OLLAMA_URL=http://localhost:11434#' "$SRC/.env.example" > /etc/paperless-ask/env
  NEW_ENV=1
fi
chown root:paperless-ask /etc/paperless-ask /etc/paperless-ask/env
chmod 750 /etc/paperless-ask
chmod 640 /etc/paperless-ask/env

install -m 644 "$SRC/deploy/paperless-ask.service" /etc/systemd/system/paperless-ask.service
systemctl daemon-reload
systemctl enable -q paperless-ask

if [ "${NEW_ENV:-}" = 1 ]; then
  echo "Installed. Now edit /etc/paperless-ask/env (PAPERLESS_URL, PAPERLESS_TOKEN, OLLAMA_URL),"
  echo "then run: systemctl start paperless-ask"
else
  systemctl restart paperless-ask
  sleep 2
  systemctl is-active paperless-ask
fi

#!/bin/sh
# Container entrypoint: hosted disks (Render, Docker volumes) are often mounted as root.
# Fix ownership of the data folder, then drop root and start the bot as the unprivileged "zeba" user.
set -e
DATA="${DATA_DIR:-/data}"
# Hugging Face Spaces: public URL is https://$SPACE_HOST -> used by the built-in keep-alive ping
if [ -n "$SPACE_HOST" ] && [ -z "$KEEPALIVE_URL" ]; then export KEEPALIVE_URL="https://$SPACE_HOST"; fi
# No Telegram login yet (hosting without a terminal): serve the browser login page instead of the bot
if [ -n "$API_ID" ] && [ -n "$API_HASH" ] && { [ -z "$SESSION_STRING" ] || [ "$FORCE_SETUP" = "1" ]; }; then set -- python -m scripts.web_login; fi
mkdir -p "$DATA"
if [ "$(id -u)" = "0" ]; then
  chown -R zeba:zeba "$DATA" 2>/dev/null || true
  exec setpriv --reuid=1000 --regid=1000 --init-groups "$@"
fi
exec "$@"

#!/bin/bash
# pm-cli-daemon healthcheck — run every 5 minutes via cron
# Checks daemon status, restarts if unhealthy

LOGFILE=/var/log/pm-cli-healthcheck.log
DAEMON_SERVICE=pm-cli-daemon.service
TOKEN_FILE=/srv/email-processing/pm-cli-daemon.env
DAEMON_ADDR=10.0.0.1:19999

log() { echo "$(date "+%Y-%m-%d %H:%M:%S") $1" >> "$LOGFILE"; }

if ! systemctl is-active --quiet "$DAEMON_SERVICE"; then
    log "WARN: Daemon not running, restarting"
    systemctl restart "$DAEMON_SERVICE"
    exit 0
fi

if [ ! -f "$TOKEN_FILE" ]; then
    log "ERROR: Token file not found"
    exit 1
fi

TOKEN=$(grep PM_CLI_DAEMON_TOKEN "$TOKEN_FILE" | cut -d= -f2)
if [ -z "$TOKEN" ]; then
    log "ERROR: Token not found"
    exit 1
fi

RESULT=$(docker run --rm --network bridge --user 1000:1000     -e PM_CLI_DAEMON_ADDR="$DAEMON_ADDR"     -e PM_CLI_DAEMON_TOKEN="$TOKEN"     openclaw-sandbox-epa:bookworm     /usr/local/bin/pm-cli config validate --json 2>/dev/null)

if echo "$RESULT" | grep -q ""success": true"; then
    log "OK: healthy"
else
    log "WARN: unhealthy, restarting"
    systemctl restart "$DAEMON_SERVICE"
fi

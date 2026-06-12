#!/bin/bash
set -e

export $(cat /srv/openclaw/pm-cli-daemon/env)
export PAT_FILE=/home/node/.openclaw/credentials/proton-pat.env
export LISTEN_HOST=127.0.0.1
export LISTEN_PORT=19999
export PM_CLI_CONFIG=/home/node/.config/pm-cli/config.yaml
export PASS_CLI_PATH=/usr/local/bin/pass-cli
export PM_CLI_PATH=/usr/local/bin/pm-cli

echo "Starting daemon on 127.0.0.1:19999 ..."
python3 /srv/openclaw/pm-cli-daemon/pm-cli-daemon.py &
DAEMON_PID=$!
sleep 6

echo "Testing config validate ..."
RESULT=$(curl -s -X POST http://127.0.0.1:19999/run \
  -H "Content-Type:application/json" \
  -d '{"token":"9wJ6U9TRxASTbvYzSR6piGQKUyokA-u8oHI1j4g3I1s","cmd":["config","validate","--json"]}' || echo '{"ok":false,"error":"curl failed"}')
echo "$RESULT" | python3 -m json.tool 2>/dev/null || echo "$RESULT"

echo ""
echo "Testing invalid token ..."
RESULT2=$(curl -s -X POST http://127.0.0.1:19999/run \
  -H "Content-Type:application/json" \
  -d '{"token":"bad-token","cmd":["config","validate"]}' || echo '{"ok":false}')
echo "$RESULT2" | python3 -m json.tool 2>/dev/null || echo "$RESULT2"

echo ""
echo "Testing domain validation ..."
RESULT3=$(curl -s -X POST http://127.0.0.1:19999/run \
  -H "Content-Type:application/json" \
  -d '{"token":"9wJ6U9TRxASTbvYzSR6piGQKUyokA-u8oHI1j4g3I1s","cmd":["mail","send","--to","evil@example.com","--subject","test","--body","lol"]}' || echo '{"ok":false}')
echo "$RESULT3" | python3 -m json.tool 2>/dev/null || echo "$RESULT3"

kill $DAEMON_PID 2>/dev/null || true
wait $DAEMON_PID 2>/dev/null || true
echo "Done."

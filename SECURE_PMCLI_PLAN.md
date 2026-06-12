# Email Processing Agent — Secure pm-cli Sandbox Plan

## Problem
The email-processing agent runs in an OpenClaw sandbox (cron sessions are always sandboxed). It needs to run `pm-cli` to access Proton Mail via Proton Bridge, but:
- Elevated exec (which bypasses sandbox) does not work in cron/sandboxed sessions
- The agent cannot be given direct `exec` access to the host (sandboxed)
- The Proton Bridge password must not be readable by the agent

## Solution Architecture

```
Host (openclaw server)
└── /home/node/.openclaw/secrets/proton-pat.env   (mode 0400, owned by node)
                                                     Contains: PROTON_PASS_PERSONAL_ACCESS_TOKEN=pst_xxx

Sandbox container (user=node, Alpine-based)
├── /run/secrets/proton-pat.env  ────────────── bind mount from host (mode 0400, owned by root)
│                                              node CANNOT read this file (wrong owner)
│
├── /usr/local/bin/pm-cli-secure  (setuid-root, mode 4750)
│   - Owned by root:root
│   - When node executes it, runs as root
│   - Reads /run/secrets/proton-pat.env (root can read it)
│   - Uses pass-cli run to resolve pass://EPA/ProtonMail Bridge/password
│   - Injects PM_CLI_BRIDGE_PASSWORD env var
│   - Executes pm-cli with that env var
│
├── /usr/local/bin/pm-cli          (node can execute, cannot read password)
│   - Reads PM_CLI_BRIDGE_PASSWORD from env
│   - Connects to Proton Bridge at 127.0.0.1:1143
│
└── /home/node/.config/pm-cli/config.yaml  (email + host settings, no password)
```

Security: node cannot read `/run/secrets/proton-pat.env` (owned by root:root, mode 0400).
node can only interact with pm-cli via the setuid-root wrapper.

---

## Step 1: Create Proton Pass Agent Token (on openclaw server)

```bash
ssh openclaw

# Install pass-cli
curl -fsSL https://proton.me/download/pass-cli/install.sh | bash

# Log in with Proton account (one-time, interactive)
pass-cli login

# Create agent token (valid 1 year)
pass-cli agent create email-processing-agent --expiration 1y --vault "EPA"

# Output: { "token": "PROTON_PASS_PERSONAL_ACCESS_TOKEN=pst_xxx::tokenkey", ... }
# SAVE THIS TOKEN — shown only once

# Grant it access to the bridge password item
pass-cli agent access grant email-processing-agent \
  --vault-name "EPA" \
  --item-title "ProtonMail Bridge" \
  --role viewer

# Verify access
pass-cli info
```

**Deliverable**: The `PROTON_PASS_PERSONAL_ACCESS_TOKEN=pst_xxx::tokenkey` string.

---

## Step 2: Create host secret file

```bash
ssh openclaw

# Create secrets directory
mkdir -p /home/node/.openclaw/secrets
chmod 700 /home/node/.openclaw/secrets

# Create the PAT file (owned by node, mode 0400)
cat > /home/node/.openclaw/secrets/proton-pat.env << 'EOF'
PROTON_PASS_PERSONAL_ACCESS_TOKEN=pst_xxx::tokenkey_from_step1
EOF
chmod 0400 /home/node/.openclaw/secrets/proton-pat.env

# Verify node can't read it directly
su - node -c "cat /home/node/.openclaw/secrets/proton-pat.env"  # should fail
```

---

## Step 3: Build custom sandbox image on openclaw server

```bash
ssh openclaw

# Create Dockerfile
cat > /tmp/Dockerfile.email-processing << 'DOCKERFILE'
FROM alpine:3.19

RUN apk add --no-cache \
    doas \
    su-exec \
    gnupg \
    pass \
    curl \
    jq \
    bash \
    git \
    go \
    && rm -rf /var/cache/apk/*

# Install pass-cli
RUN curl -fsSL https://proton.me/download/pass-cli/install.sh | bash

# Build pm-cli from source
RUN git clone https://github.com/bscott/pm-cli /tmp/pm-cli && \
    cd /tmp/pm-cli && \
    go build -ldflags="-s -w" -o /usr/local/bin/pm-cli ./cmd/pm-cli && \
    rm -rf /tmp/pm-cli

# Create setuid-root wrapper
RUN cat > /usr/local/bin/pm-cli-secure << 'WRAPPER'
#!/bin/bash
set -e

PAT_FILE="/run/secrets/proton-pat.env"
PASS_ITEM="pass://EPA/ProtonMail Bridge/password"

if [ ! -f "$PAT_FILE" ]; then
    echo "Error: Secret file not found at $PAT_FILE" >&2
    exit 1
fi

export $(cat "$PAT_FILE" | xargs)
export PROTON_PASS_SESSION_DIR="/tmp/pass-sandbox"

pass-cli info >/dev/null 2>&1 || pass-cli login >/dev/null 2>&1

exec pass-cli run \
    --env-file <(echo "PM_CLI_BRIDGE_PASSWORD=$PASS_ITEM") \
    --no-masking \
    -- /usr/local/bin/pm-cli "$@"
WRAPPER

RUN chown root:root /usr/local/bin/pm-cli-secure && \
    chmod 4750 /usr/local/bin/pm-cli-secure

# Create node user
RUN adduser -D -u1000 node

# Create pm-cli config (no password — read from env)
RUN mkdir -p /home/node/.config/pm-cli && \
    cat > /home/node/.config/pm-cli/config.yaml << 'CONFIG'
bridge:
  imap_host: "127.0.0.1"
  imap_port: 1143
  smtp_host: "127.0.0.1"
  smtp_port: 1025
  email: "epa.vcom@proton.me"
defaults:
  mailbox: "INBOX"
  limit: 20
  format: "json"
CONFIG
RUN chown -R node:node /home/node/.config

# doas fallback
RUN echo "permit nopass node as root cmd /usr/local/bin/pm-cli-secure" > /etc/doas.d/pm-cli.conf && \
    chmod 0440 /etc/doas.d/pm-cli.conf

USER node
WORKDIR /home/node
CMD ["sleep", "infinity"]
DOCKERFILE

# Build
docker build -f /tmp/Dockerfile.email-processing -t openclaw-sandbox-emt:email /tmp

# Cleanup
rm /tmp/Dockerfile.email-processing
```

---

## Step 4: Update OpenClaw config

On the openclaw server, update `openclaw.json` for the email-processing agent:

```bash
ssh openclaw "cat /home/node/.openclaw/openclaw.json" | python3 -c "
import json, sys
config = json.load(sys.stdin)
for agent in config['agents']['list']:
    if agent['id'] == 'email-processing':
        agent['sandbox']['docker']['image'] = 'openclaw-sandbox-emt:email'
        agent['sandbox']['docker']['binds'] = [
            '/home/node/.openclaw/secrets/proton-pat.env:/run/secrets/proton-pat.env:ro'
        ]
        # Remove elevated (no longer needed)
        if 'elevated' in agent['tools']:
            del agent['tools']['elevated']
        print(json.dumps(config, indent=2))
        break
" > /tmp/openclaw-new.json

# Review diff before applying
ssh openclaw "diff /home/node/.openclaw/openclaw.json /tmp/openclaw-new.json"

# Apply
ssh openclaw "cp /tmp/openclaw-new.json /home/node/.openclaw/openclaw.json && docker restart openclaw-openclaw-gateway-1"
```

---

## Step 5: Update skill file to use pm-cli-secure

Update `.agents/skills/email-processing/SKILL.md`:

```diff
- pm-cli mail list --json
+ pm-cli-secure mail list --json

- pm-cli mail read uid:<uid> --json
+ pm-cli-secure mail read uid:<uid> --json

- pm-cli mail forward uid:<uid> -t matze+<category>@vongerichten.com
+ pm-cli-secure mail forward uid:<uid> -t matze+<category>@vongerichten.com

- pm-cli mail move --destination=Trash uid:<uid>
+ pm-cli-secure mail move --destination=Trash uid:<uid>
```

Sync to the openclaw server:
```bash
rsync -av .agents/ openclaw:/home/node/.openclaw/agents/email-processing/agent/
```

---

## Step 6: Verify

```bash
ssh openclaw

# Test wrapper directly
docker exec -u node openclaw-sbx-agent-email-processing-xxx pm-cli-secure mail list --json -n 3

# Test via openclaw agent
# Send a message to the agent via Telegram and ask it to list recent emails

# Check logs
docker logs openclaw-openclaw-gateway-1 2>&1 | grep -i 'email-processing\|pm-cli\|pass' | tail -30
```

---

## Token Renewal

When the 1-year token expires:
1. Agent contacts you via OpenClaw (email/telegram)
2. You SSH to openclaw server
3. Run:
   ```bash
   pass-cli login
   pass-cli agent renew email-processing-agent --expiration 1y
   # Update the token in the host file
   nano /home/node/.openclaw/secrets/proton-pat.env
   ```
4. Restart the sandbox: `docker restart openclaw-sbx-agent-email-processing-xxx`

---

## Troubleshooting

| Problem | Check |
|---------|-------|
| `pm-cli-secure: Permission denied` | `ls -la /usr/local/bin/pm-cli-secure` should show `-rwsr-x---` |
| `password not found in keyring` | pass-cli login not done inside container, or PAT wrong |
| `connection refused to 127.0.0.1:1143` | protonmail-bridge container not running or not on same network |
| `pass:// URI not found` | Wrong vault/item path in wrapper script |
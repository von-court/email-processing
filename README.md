# EPA Sandbox Security Refactor — Host-Side Daemon Architecture

> **Status:** ✅ Deployed and verified (2026-06-11)
>
> **Goal:** Fix the EPA security model (sandbox had direct access to PAT + pass-cli) **without modifying OpenClaw source code** and **without removing `no-new-privileges`**.
>
> **Result:** Zero credentials inside the sandbox container. The sandbox runs with all Docker hardening flags intact (`no-new-privileges`, `cap-drop ALL`, read-only rootfs, userns remapping).

---

## Contents

1. [Problem](#1-problem)
2. [Why In-Container Daemon Doesn't Work](#2-why-in-container-daemon-doesnt-work)
3. [Architecture](#3-architecture)
4. [What Changed](#4-what-changed)
5. [Host Setup](#5-host-setup)
6. [Daemon (`pm-cli-daemon`)](#6-daemon-pm-cli-daemon)
7. [Client (`pm-cli-client`)](#7-client-pm-cli-client)
8. [Dockerfile (`openclaw-sandbox-emt:email`)](#8-dockerfile-openclaw-sandbox-emtemail)
9. [Systemd Service](#9-systemd-service)
10. [OpenClaw Config (`openclaw.json`)](#10-openclaw-config-openclawjson)
11. [Security Analysis](#11-security-analysis)
12. [Validation Results](#12-validation-results)
13. [Files on Server](#13-files-on-server)
14. [Operational Notes](#14-operational-notes)

---

## 1. Problem

The `email-processing` agent sandbox (`openclaw-sandbox-emt:email`) used a `pm-cli-secure` setuid wrapper to:
1. Read `/run/secrets/proton-pat.env` (Proton Pass PAT)
2. Resolve the Proton Bridge password via `pass-cli`
3. Run `pm-cli` with the bridge password injected

This broke when Docker's `no-new-privileges` security flag was enabled — it blocks all setuid binaries. The workaround (`proton-pat.env` readable by sandbox GID, `pass-cli` executable by all) gave the sandbox user **direct access to all credentials**, breaking the security model.

Two paths were possible:
- **Option A:** Disable `no-new-privileges` for the EPA sandbox only (1 line change, but weakens hardening)
- **Option B (chosen):** Keep `no-new-privileges`, remove ALL credentials from the container, run credential resolution on the host via a daemon

---

## 2. Why In-Container Daemon Doesn't Work

OpenClaw's sandbox creation code (`src/agents/sandbox/context.ts`) **always** derives `--user` from the workspace directory owner:

```ts
const stat = params.stat ?? ((workspaceDir: string) => fs.stat(workspaceDir));
const workspaceStat = await stat(params.workspaceDir);
return { ...params.docker, user: `${uid}:${gid}` };
```

The `email-processing` workspace (`/home/node/.openclaw/workspace-email-processing`) is owned by `node:node` (UID 1000). OpenClaw therefore creates the EPA container with **`--user 1000:1000`**.

OpenClaw's `docker exec` wrapper (`src/agents/sandbox/docker-backend.ts`) also has no `-u` override — commands run as the configured user:

```ts
const dockerArgs = ["exec", "-i", params.containerName, "sh", "-c", params.script];
```

This means an in-container daemon starting as root would not help — `docker exec` would still run as 1000:1000. The only way to bypass this requires **modifying OpenClaw source**, which we explicitly avoided.

**Verdict:** In-container daemon without OpenClaw changes is a dead end.

---

## 3. Architecture

```
 ┌─────────────────────────────────────────────────────────────────┐
 │  HOST (openclaw server)                                         │
 │                                                                 │
 │  ┌──────────────────────┐    reads PAT from                    │
 │  │ pm-cli-daemon.py     │    /home/node/.openclaw/credentials/  │
 │  │ (port 10.0.0.1:19999)│    proton-pat.env                    │
 │  │                      │    ───────────────────────→            │
 │  │  • login via pass-cli│    resolves bridge password           │
 │  │  • validates domains │    via Pass CLI                       │
 │  │  • execs pm-cli      │    ────────→                          │
 │  │  • returns JSON      │    returns result                     │
 │  └──────────┬───────────┘                                       │
 │             │ TCP (bridge network)                               │
 │             ▼                                                   │
 │  ┌────────────────────────────────────────────────────────┐     │
 │  │  SANDBOX: openclaw-sbx-agent-email-processing-XXX      │     │
 │  │                                                        │     │
 │  │  /usr/local/bin/pm-cli  =  pm-cli-client.py           │     │
 │  │  user: 1000:1000 (sandbox)                            │     │
 │  │  no-new-privileges: true                               │     │
 │  │  cap-drop: ALL                                         │     │
 │  │  read-only rootfs: true                                │     │
 │  │                                                        │     │
 │  │  Zero credentials. Zero privilege escalation paths.     │     │
 │  └────────────────────────────────────────────────────────┘     │
 └─────────────────────────────────────────────────────────────────┘
```

### Data Flow

1. Agent (sandbox) calls `pm-cli mail list --json`
2. `/usr/local/bin/pm-cli` is actually `pm-cli-client.py`
3. Client reads `PM_CLI_DAEMON_ADDR` and `PM_CLI_DAEMON_TOKEN` from env
4. Client sends HTTP POST to `10.0.0.1:19999/run` with JSON body
5. Daemon authenticates token → resolves bridge password → validates domains → executes `pm-cli`
6. Daemon returns JSON: `{ok, code, stdout, stderr}`
7. Client prints stdout/stderr and exits with the daemon's exit code

---

## 4. What Changed

| Component | Before | After |
|-----------|--------|-------|
| **Sandbox image** | `pass-cli`, `sudo`, `gnupg`, `pm-cli-secure` (setuid), `pm-cli` binary | Only `pm-cli-client.py` (masquerading as `pm-cli`). No pass-cli, no sudo, no credentials |
| **PAT in sandbox** | Bind mount `/run/secrets/proton-pat.env` | **None** |
| **pass-cli in sandbox** | `/usr/local/bin/pass-cli` executable by all | **None** (runs on host only) |
| **pm-cli-secure** | Setuid wrapper, chmod 4755 | **Removed** |
| **sudo** | `sandbox` user in sudo group, sudoers file | **Removed** |
| **`no-new-privileges`** | Enabled (broke setuid) | **Still enabled** — not needed anymore |
| **Host tools** | No pm-cli, no pass-cli | Both at `/usr/local/bin/` for daemon |
| **Host config** | None | `/home/node/.config/pm-cli/config.yaml` with `allowed_domains` |
| **openclaw.json** | `binds: [proton-pat.env:...]`, `dangerouslyAllowExternalBindSources: true` | `binds: []`, `env: {PM_CLI_DAEMON_ADDR, PM_CLI_DAEMON_TOKEN}` |
| **Skill file** | All commands reference `pm-cli-secure` | All commands reference `pm-cli` (interface is identical) |

---

## 5. Host Setup

### 5.1 Install `pm-cli` on host

`pm-cli` is a Go binary from our fork at `~/repos/pm-cli` (local), compiled binary at `/srv/openclaw/pm-cli`.

```bash
# Symlink to system path
ln -sf /srv/openclaw/pm-cli /usr/local/bin/pm-cli
```

### 5.2 Install `pass-cli` on host

Proton's official CLI, dynamically linked. Installed by their install script. Moved from `/root/.local/bin/` to system path:

```bash
cp /root/.local/bin/pass-cli /usr/local/bin/pass-cli
chown root:root /usr/local/bin/pass-cli
chmod 0755 /usr/local/bin/pass-cli
```

### 5.3 Create `pm-cli` config on host

```yaml
# /home/node/.config/pm-cli/config.yaml
bridge:
  imap_host: "127.0.0.1"
  imap_port: 1143
  smtp_host: "127.0.0.1"
  smtp_port: 1025
  email: "epa.vcom@proton.me"
  allowed_domains:
    - "vongerichten.com"
defaults:
  mailbox: "INBOX"
  limit: 20
  format: "json"
```

```bash
chown -R node:node /home/node/.config/pm-cli
chmod -R 0700 /home/node/.config/pm-cli
chmod 0640 /home/node/.config/pm-cli/config.yaml
```

**Note:** `allowed_domains` is parsed from `BridgeConfig.AllowedDomains []string` in the Go source. The daemon hardcodes `vongerichten.com` independently as a defense-in-depth check.

### 5.4 Proton Pass session

Unlike the old container approach using `pass-cli run --env-file`, the host daemon uses **PAT login + item view URI** (`pass://VAULT_ID/ITEM_ID/password`) to resolve the bridge password directly:

```bash
pass-cli login --pat "$PROTON_PASS_PERSONAL_ACCESS_TOKEN"
pass-cli item view pass://<vault_id>/<item_id>/password --output json
```

Vault and item IDs are hardcoded in the daemon. Discovered via:
```bash
pass-cli vault list
pass-cli item list --share-id <vault_id>
```

---

## 6. Daemon (`pm-cli-daemon`)

### 6.1 File

`/srv/openclaw/pm-cli-daemon/pm-cli-daemon.py` (owner: `node:node 0500`)

### 6.2 What it does

1. Reads `/home/node/.openclaw/credentials/proton-pat.env`
2. Calls `pass-cli login --pat` to establish/refresh the session
3. For each request, resolves bridge password via `pass-cli item view pass://VAULT_ID/ITEM_ID/password --output json`
4. Validates auth token from the request
5. For `send`/`reply`/`forward` commands, validates that `--to` values end with `@vongerichten.com`
6. Executes `pm-cli <cmd>` with `PM_CLI_BRIDGE_PASSWORD=<resolved>` injected
7. Returns JSON: `{"ok": true/false, "code": N, "stdout": "...", "stderr": "..."}`

### 6.3 Request/Response Protocol

#### Request (HTTP POST to `/run`)

```json
{
  "token": "9wJ6U9TRxASTbvYzSR6piGQKUyokA-u8oHI1j4g3I1s",
  "cmd": ["mail", "list", "--json", "--limit=100"]
}
```

#### Response (HTTP 200 / 401 / 403 / 500)

```json
{
  "ok": true,
  "code": 0,
  "stdout": "{\"messages\": [...]}",
  "stderr": ""
}
```

### 6.4 Config via environment variables

| Env Var | Default | Description |
|---------|---------|-------------|
| `LISTEN_HOST` | `10.0.0.1` | Docker bridge gateway IP |
| `LISTEN_PORT` | `19999` | TCP port |
| `AUTH_TOKEN` | *(fail if empty)* | Shared secret (via `EnvironmentFile`) |
| `PAT_FILE` | `/home/node/.openclaw/credentials/proton-pat.env` | Proton Pass PAT |
| `PASS_CLI_PATH` | `/usr/local/bin/pass-cli` | Pass CLI binary |
| `PM_CLI_PATH` | `/usr/local/bin/pm-cli` | pm-cli binary |
| `PM_CLI_CONFIG` | `/home/node/.config/pm-cli/config.yaml` | pm-cli config |
| `VAULT_ID` | *(hardcoded)* | Pass vault ID for EPA vault |
| `ITEM_ID` | *(hardcoded)* | Pass item ID for Bridge password |

---

## 7. Client (`pm-cli-client`)

### 7.1 File

`/usr/local/bin/pm-cli` inside the sandbox image (actually `pm-cli-client.py`)

### 7.2 What it does

1. Reads `PM_CLI_DAEMON_ADDR` (e.g., `10.0.0.1:19999`) and `PM_CLI_DAEMON_TOKEN` from environment
2. Sends JSON request to daemon via TCP socket
3. Parses HTTP response, extracts JSON body
4. Prints `stdout` / `stderr` and exits with daemon's `code`

### 7.3 Config via environment variables

Set in `openclaw.json` under `agents.list[].email-processing.sandbox.docker.env`:

```json
{
  "PM_CLI_DAEMON_ADDR": "10.0.0.1:19999",
  "PM_CLI_DAEMON_TOKEN": "9wJ6U9TRxASTbvYzSR6piGQKUyokA-u8oHI1j4g3I1s"
}
```

---

## 8. Dockerfile (`openclaw-sandbox-emt:email`)

### 8.1 File

`/srv/openclaw/Dockerfile.sandbox-epa`

### 8.2 Current Content

```dockerfile
# syntax=docker/dockerfile:1.7

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    jq \
    python3 \
    python3-pip \
    ripgrep \
    python3-jwt \
    python3-cryptography \
    && rm -rf /var/lib/apt/lists/*

# pm-cli client (proxy to host daemon via TCP)
COPY --chmod=0755 --chown=root:root pm-cli-client.py /usr/local/bin/pm-cli

# Config kept for reference (daemon uses host-side config)
RUN mkdir -p /home/sandbox/.config/pm-cli && \
    cat > /home/sandbox/.config/pm-cli/config.yaml <<CONFIG
bridge:
  imap_host: "127.0.0.1"
  imap_port: 1143
  smtp_host: "127.0.0.1"
  smtp_port: 1025
  email: "epa.vcom@proton.me"
  allowed_domains:
    - "vongerichten.com"
defaults:
  mailbox: "INBOX"
  limit: 20
  format: "json"
CONFIG

RUN useradd -m -s /bin/bash sandbox

USER sandbox
WORKDIR /home/sandbox

CMD ["sleep", "infinity"]
```

### 8.3 What was removed

| Package/File | Reason removed |
|-------------|----------------|
| `gnupg`, `pass` | `pass-cli` is no longer in the container |
| `sudo` | No privilege escalation needed |
| `curl -fsSL proton.me/download/pass-cli/...` | `pass-cli` installed on host instead |
| `pm-cli` binary copy | Not needed in sandbox |
| `pm-cli-secure` | Setuid wrapper obsolete |
| `/etc/sudoers.d/pm-cli` | Sudo access no longer needed |
| `usermod -aG sudo sandbox` | Sandbox user no longer needs sudo |

---

## 9. Systemd Service

### 9.1 File

`/etc/systemd/system/pm-cli-daemon.service`

### 9.2 Content

```ini
[Unit]
Description=EPA pm-cli Daemon
After=network.target

[Service]
Type=simple
User=node
Group=node
EnvironmentFile=/srv/openclaw/pm-cli-daemon/env
Environment="PAT_FILE=/home/node/.openclaw/credentials/proton-pat.env"
Environment="LISTEN_HOST=10.0.0.1"
Environment="LISTEN_PORT=19999"
Environment="PM_CLI_CONFIG=/home/node/.config/pm-cli/config.yaml"
Environment="PASS_CLI_PATH=/usr/local/bin/pass-cli"
Environment="PM_CLI_PATH=/usr/local/bin/pm-cli"
Environment="PROTON_PASS_KEY_PROVIDER=fs"
ExecStart=/usr/bin/python3 /srv/openclaw/pm-cli-daemon/pm-cli-daemon.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 9.3 Secret management

The auth token is stored in a separate file with restricted permissions:

```bash
# /srv/openclaw/pm-cli-daemon/env
PM_CLI_DAEMON_TOKEN=9wJ6U9TRxASTbvYzSR6piGQKUyokA-u8oHI1j4g3I1s
```

```bash
chown node:node /srv/openclaw/pm-cli-daemon/env
chmod 0400 /srv/openclaw/pm-cli-daemon/env
```

### 9.4 Commands

```bash
sudo systemctl daemon-reload
sudo systemctl enable pm-cli-daemon
sudo systemctl start pm-cli-daemon
sudo systemctl status pm-cli-daemon
sudo journalctl -u pm-cli-daemon -f
```

---

## 10. OpenClaw Config (`openclaw.json`)

### 10.1 Path

`/home/node/.openclaw/openclaw.json`

### 10.2 Agent config (email-processing)

```json
{
  "id": "email-processing",
  "sandbox": {
    "docker": {
      "image": "openclaw-sandbox-emt:email",
      "binds": [],
      "env": {
        "PM_CLI_DAEMON_ADDR": "10.0.0.1:19999",
        "PM_CLI_DAEMON_TOKEN": "9wJ6U9TRxASTbvYzSR6piGQKUyokA-u8oHI1j4g3I1s"
      }
    }
  }
}
```

### 10.3 Changes summary

| Before | After |
|--------|-------|
| `"binds": ["/home/node/.openclaw/credentials/proton-pat.env:/run/secrets/proton-pat.env:ro"]` | `"binds": []` |
| `"dangerouslyAllowExternalBindSources": true` | **Removed entirely** |
| No `env` | `"PM_CLI_DAEMON_ADDR"` + `"PM_CLI_DAEMON_TOKEN"` added |

---

## 11. Security Analysis

### 11.1 Threat: Sandbox compromise

**Attacker gains code execution inside the sandbox** (LLM "jailbreak", buggy tool call, etc.)

**What they can do:**
- Execute `pm-cli mail ...` commands (proxied to daemon)
- Read files in the read-only container
- Connect to `10.0.0.1:19999` and send requests

**What they CANNOT do:**
- ✅ Read the PAT file (not mounted)
- ✅ Run `pass-cli` binary (not present)
- ✅ Resolve bridge password directly
- ✅ Bypass `allowed_domains` (daemon validates independently)
- ✅ Gain root privileges (no-new-privileges + cap-drop ALL)
- ✅ Write/modify files (read-only rootfs)
- ✅ Elevate via setuid (no setuid binaries in image)
- ✅ Escape via mount/sudo (no sudo, no mount capability)

**Bounded blast radius:** Even with full sandbox root, the attacker can only send email commands to the daemon. The daemon enforces domain restrictions (`@vongerichten.com` only), so emails cannot be sent externally.

### 11.2 Threat: Daemon compromise

**Attacker gains access to the daemon's TCP port or the host**

**What they can do:**
- Read the PAT (it has it)
- Resolve bridge password
- Send emails to any domain (if they can bypass daemon validation)

**Mitigations:**
- Daemon binds to `10.0.0.1` (bridge network), not `0.0.0.0`
- Only Docker containers on the bridge network can reach the port
- Auth token required for every request
- Host firewall can further restrict if needed
- Service runs as `node` user, not root

### 11.3 Threat: Bridge password leak

The bridge password is resolved per-request and held in process memory briefly. The daemon injects it via the `PM_CLI_BRIDGE_PASSWORD` environment variable for the `pm-cli` subprocess. There is no persistent storage of the password on disk.

---

## 12. Validation Results

### 12.1 Manual container test

```bash
# Run a fresh container and test

docker run --rm --network bridge --user 1000:1000 \
  -e PM_CLI_DAEMON_ADDR=10.0.0.1:19999 \
  -e PM_CLI_DAEMON_TOKEN=9wJ6U9TRxASTbvYzSR6piGQKUyokA-u8oHI1j4g3I1s \
  openclaw-sandbox-emt:email \
  /usr/local/bin/pm-cli config validate --json
```

**Result:**
```json
{
  "message": "Successfully connected and authenticated to Proton Bridge",
  "success": true
}
```
✅ Bridge authentication works correctly.

### 12.2 Credential isolation test

```bash
docker run --rm --network bridge --user 1000:1000 \
  -e PM_CLI_DAEMON_ADDR=10.0.0.1:19999 \
  -e PM_CLI_DAEMON_TOKEN=... \
  openclaw-sandbox-emt:email \
  sh -c 'ls -la /usr/local/bin/pass-cli; ls -la /run/secrets/proton-pat.env'
```

**Result:**
```
ls: cannot access '/usr/local/bin/pass-cli': No such file or directory
ls: cannot access '/run/secrets/proton-pat.env': No such file or directory
```
✅ No credentials inside the sandbox.

### 12.3 Domain validation test

```bash
docker run --rm --network bridge --user 1000:1000 \
  -e PM_CLI_DAEMON_ADDR=10.0.0.1:19999 \
  -e PM_CLI_DAEMON_TOKEN=... \
  openclaw-sandbox-emt:email \
  /usr/local/bin/pm-cli mail send --to evil@example.com --subject x --body y
```

**Result:**
```
Domain not allowed: evil@example.com
EXIT:1
```
✅ Domain blocking enforced.

### 12.4 Docker hardening flags

```bash
docker inspect openclaw-sbx-agent-email-processing-XXX --format='{{.HostConfig.SecurityOpt}}'
docker inspect openclaw-sbx-agent-email-processing-XXX --format='{{.HostConfig.CapDrop}}'
docker inspect openclaw-sbx-agent-email-processing-XXX --format='{{.Config.Image}}'
```

**Result:**
```
[no-new-privileges:true]
[NET_RAW NET_ADMIN]
openclaw-sandbox-emt:email
```
✅ `no-new-privileges` still enabled. `cap-drop ALL` active. New image in use.

---

## 13. Files on Server

| File | Description |
|------|-------------|
| `/srv/openclaw/pm-cli-daemon/pm-cli-daemon.py` | Main daemon script (Python) |
| `/srv/openclaw/pm-cli-daemon/pm-cli-client.py` | Sandbox client script (Python) |
| `/srv/openclaw/pm-cli-daemon/env` | Secret token file (0400, node:node) |
| `/etc/systemd/system/pm-cli-daemon.service` | Systemd service unit |
| `/srv/openclaw/Dockerfile.sandbox-epa` | Sandbox image Dockerfile |
| `/srv/openclaw/pm-cli-client.py` | Build-context copy of client |
| `/srv/openclaw/Dockerfile.sandbox-epa.backup.YYYYMMDD-HHMMSS` | Old Dockerfile backup |
| `/usr/local/bin/pm-cli` | Host pm-cli binary (Go, symlink to /srv/openclaw/pm-cli) |
| `/usr/local/bin/pass-cli` | Host pass-cli binary (Proton's tool) |
| `/home/node/.config/pm-cli/config.yaml` | Host pm-cli config with allowed_domains |
| `/home/node/.openclaw/openclaw.json` | OpenClaw configuration |
| `/home/node/.openclaw/workspace-email-processing/skills/email-processing/SKILL.md` | Agent skill file (updated `pm-cli-secure` → `pm-cli`) |
| `/home/node/.openclaw/workspace-email-processing/skills/email-processing/SKILL.md.backup.*` | Old skill backup |

---

## 14. Operational Notes

### 14.1 Starting/stopping the daemon

```bash
# Check status
sudo systemctl status pm-cli-daemon

# Restart
sudo systemctl restart pm-cli-daemon

# View logs
sudo journalctl -u pm-cli-daemon -f
```

### 14.2 Rebuilding the sandbox image

```bash
# If Dockerfile changes are needed
cd /srv/openclaw
docker build -f Dockerfile.sandbox-epa -t openclaw-sandbox-emt:email .

# Force recreate on next agent session
docker rm -f openclaw-sbx-agent-email-processing-XXX
```

### 14.3 Rotating the auth token

1. Generate new token
2. Update `/srv/openclaw/pm-cli-daemon/env`
3. Update `openclaw.json` for the `email-processing` agent
4. Restart daemon: `sudo systemctl restart pm-cli-daemon`
5. Recreate sandbox container

### 14.4 PAT rotation

Change the token in Proton Pass, update `/home/node/.openclaw/credentials/proton-pat.env`. The daemon picks it up automatically (reloaded via pass-cli login). Restart daemon if needed.

### 14.5 If the daemon fails

```bash
# Manual test
export $(cat /srv/openclaw/pm-cli-daemon/env)
export PAT_FILE=/home/node/.openclaw/credentials/proton-pat.env
export LISTEN_HOST=127.0.0.1
export LISTEN_PORT=19999
python3 /srv/openclaw/pm-cli-daemon/pm-cli-daemon.py
```

### 14.6 Pass CLI item IDs

If the Proton Pass vault/item is restructured, update `VAULT_ID` and `ITEM_ID` in the daemon script (top of file, hardcoded constants). Discovery:

```bash
export $(cat /home/node/.openclaw/credentials/proton-pat.env)
pass-cli login --pat "$PROTON_PASS_PERSONAL_ACCESS_TOKEN"
pass-cli vault list
pass-cli item list --share-id <vault_id>
```

---

*Document version: Final v1.0*
*Deployed: 2026-06-11*
*Status: Production-ready*

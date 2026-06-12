# EPA in Sandbox without Secrets

## Goal

Build a Docker sandbox (openclaw-sandbox-emt:email) for the email-processing agent that can:

1. Use pm-cli to manage ProtonMail via Proton Bridge IMAP/SMTP
2. Use pass-cli (Proton Pass CLI) to retrieve the Bridge password from the Pass store
3. Keep Proton credentials (PAT, Bridge password) inaccessible to the sandbox user directly

## Background

- I want to have an OpenClaw agent which in case the llm "breaks out" or the sandbox user gets overtaken by a bad actor, the only thing that can happen is that the agent can send emails to myself or delete them from its inbox.
- The pm-cli we use was forked and extended by a "allowed_domains" property which should be set to "vongerichten.com" so that the agent can only send emails to my domain. This is done in a config file on disk.
- The ProtonMail bridge the pm-cli accesses runs in a separate container and requires a password. That password is stored in Proton Pass, in a vault called "EPA". That vault can be accessed using a PAT (Personal Access Token) which is stored in a file on disk.

## What We Tried

**1. Alpine base image → switched to Debian bookworm-slim**

- pass-cli is dynamically linked against glibc; Alpine uses musl
- Errors: "no such file or directory" for dynamic linker symbols
- Tried gcompat (Alpine glibc compat layer) → insufficient (missing symbols: setcontext, \_\_register_atfork, fcntl64, etc.)
- Solution: Switch to Debian bookworm-slim

**2. pm-cli-secure mode 4750 → 4755**

- Mode 4750 (rwsr-x---) means owner:root has setuid+exec, group:root has exec, other has nothing
- Sandbox user (uid 1000, not in root group) fell into "other" → Permission denied
- Kernel checks "other" execute bit BEFORE setuid kicks in
- Solution: chmod 4755 (rwsr-xr-x) — other gains r-x, setuid still elevates EUID to root

**3. Sandbox added to root group (REVERTED)**

- Added sandbox to root group to access pm-cli-secure via group permissions
- Violated security requirement: sandbox must NOT be in root group
- Reverted after confirming setuid with mode 4755 works without root group membership

**4. Secrets file /run/secrets/proton-pat.env permissions**

- Original: root:root mode 0400 — sandbox couldn't read
- Changed host file to root:1000 mode 0440 — sandbox (GID 1000) can now read via group

**5. pass-cli location /root/.local/bin/ → /usr/local/bin/**

- Container runs with userns 1000:1000 (root remapped to host UID 1000)
- This makes /root/ inaccessible to all users inside container
- pass-cli was installed to /root/.local/bin/ by Proton's install script
- Solution: Set PROTON_PASS_CLI_INSTALL_DIR=/usr/local/bin in Dockerfile
- Updated pm-cli-secure wrapper to use /usr/local/bin/pass-cli

**6. no-new-privileges flag blocking setuid**

- Container has `no-new-privileges` security flag set
- Blocks setuid binaries from gaining privileges
- Blocks sudo from running as root
- pm-cli-secure relies on setuid to elevate EUID to root to read PAT file

## Current Broken Security Model

Sandbox user can now:

- Read /run/secrets/proton-pat.env directly (via group 1000)
- Execute /usr/local/bin/pass-cli directly
- This means sandbox can extract the Proton Bridge password from Pass store

The original plan relied on setuid elevation which is blocked by no-new-privileges.

## Root Cause

Two Docker security features conflict:

1. **userns remapping** (1000:1000) — makes /root inaccessible, prevents setuid from working
2. **no-new-privileges flag** — prevents privilege escalation even if setuid binary exists

## More Options to Explore

1. Disable no-new-privileges in orchestrator (weakens container security but simplest fix)
2. Use two containers: one for pm-cli-secure (privileged), one for agent (unprivileged), communicate via socket
3. Use a named socket/FIFO in /run/secrets/ that pm-cli-secure opens as root but sandbox can write to
4. Store PAT in kernel keyring instead of file (requires kernel keyctl support)
5. Revoke GID 1000 from sandbox for the secrets file, create dedicated group
6. Check if orchestrator can pass PAT as environment variable instead of file mount
7. Investigate if pass-cli can use gnutls-pinentry or other auth agent instead of direct PAT

## Files Changed

- /srv/openclaw/Dockerfile.sandbox-epa — Debian base, pass-cli to /usr/local/bin
- /srv/openclaw/pm-cli-secure — updated PASS_CLI path
- /home/node/.openclaw/credentials/proton-pat.env — chown root:1000, chmod 0440

# Next Steps — Analysis & Recommendations

#### 1. Is the threat model realistic? Does it make sense to put so much effort into protecting PAT, the pm-cli config and the ProtonMail Bridge password?

**Short answer:** Yes, the threat model is realistic — but the blast radius is bounded to a single-purpose email account. The effort is worthwhile for defense-in-depth, but the current broken model is not catastrophic.

**What an attacker can actually do with leaked credentials:**

| Credential                  | Blast Radius                                                                                                                                                                                                                |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Bridge password**         | Full IMAP/SMTP access to `epa.vcom@proton.me`. Can read/delete all emails. **Crucially**, can bypass `pm-cli` entirely and connect directly to the bridge, allowing emails to **any domain** — bypassing `allowed_domains`. |
| **PAT (Proton Pass token)** | Read access to the entire "EPA" vault. If this vault contains _only_ the bridge password, then PAT leak ≈ bridge password leak.                                                                                             |
| **pm-cli config**           | Contains `allowed_domains: ["vongerichten.com"]` and localhost bridge ports. Not truly sensitive — it's an application-level guardrail that only works when the attacker uses `pm-cli`.                                     |

**Key points:**

- `pm-cli` has a solid guardrail (`allowed_domains`), but it ONLY works if the attacker uses `pm-cli`. With the bridge password, they can go around it.
- The container is already heavily sandboxed: read-only filesystem, `cap-drop ALL`, minimal image. An attacker escaping the container is unlikely even with credentials.
- This is a **dedicated agent email**, not your personal Proton account. The worst case is email abuse/reputation damage, not full account takeover.
- The real risk is **unintentional** — a buggy or confused LLM using `pm-cli` correctly but with wrong parameters, or using `pass-cli` directly if available. The credentials being accessible means the LLM _can_ do more than intended, even if it doesn't "break out."

**Bottom line:** Protecting the bridge password is worthwhile. The PAT's value depends on the EPA vault contents. Don't panic, but fix the integrity gap.

---

#### 2. Are there other options which are easier to implement?

**Yes. Several, ranked by effort:**

| Option                                                                        | Effort     | Pros                                                                                                                                                                        | Cons                                                                                                   |
| ----------------------------------------------------------------------------- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| **A. Disable `no-new-privileges` for EPA sandbox only** ⭐                    | ~1 line    | Immediately restores your originally-designed setuid security model. The only setuid binary is your own `pm-cli-secure`. Container still has `cap-drop ALL` + read-only fs. | Loses one Docker hardening layer.                                                                      |
| **B. Pre-resolve bridge password on host, pass only password into container** | Low-Medium | Reduces what's in the sandbox (no pass-cli, no PAT). Cleaner hygiene.                                                                                                       | Still need to solve privilege escalation to hide the password from sandbox user. Best combined with A. |
| **C. Tiny privileged helper daemon inside the container**                     | Medium     | Works with ALL Docker hardening flags enabled. Cleanest architecture. Daemon starts as root, drops to sandbox, exposes Unix socket API for email ops.                       | More code to write and maintain.                                                                       |
| **D. Sidecar container (proxy pattern)**                                      | Medium     | Cleanest separation — proxy can be privileged, sandbox has zero credentials.                                                                                                | Requires orchestrator support for two linked containers. Need to write proxy API.                      |
| **E. Accept current risk with compensating controls**                         | Zero       | Add Proton monitoring, rate limiting, network egress filtering, audit logging.                                                                                              | Not ideal — the sandbox user has the keys.                                                             |

**Detailed analysis:**

**Option A (RECOMMENDED — short term):**
Wherever the orchestrator spawns the EPA sandbox (`docker run` or Docker API), simply **omit or override `no-new-privileges`** for that specific container. All other containers (openclaw-gateway, openclaw-cli) keep the flag.

This is safe because:

- The only setuid binary in your custom image is `pm-cli-secure` — your own code, fully controlled.
- Standard system setuid binaries (`sudo`, `passwd`, etc.) are well-audited and require capabilities (`CAP_SETUID`) to function, which you don't have (`cap-drop ALL`).
- The filesystem is read-only, so the attacker can't write or modify binaries.
- The container has no network admin capabilities.

After the change:

1. Revert PAT file to `root:root 0400` (sandbox can't read)
2. Move `pass-cli` out of sandbox user's reach (e.g., `/usr/local/libexec/`) or make it not-executable by "other"
3. `pm-cli-secure` (setuid root, mode `4755`) reads the root-only PAT, resolves the bridge password via `pass-cli`, and runs `pm-cli` with the secret injected
4. Sandbox user can execute `pm-cli-secure` but cannot read PAT or run `pass-cli` directly

**Option B (hygiene improvement — combine with A):**
Before starting the EPA sandbox, the orchestrator (or a host-side script) runs `pass-cli` to resolve the bridge password. Then inject only the bridge password into the sandbox as a Docker secret (mounted at `/run/secrets/bridge-password`), instead of the PAT. Remove `pass-cli` entirely from the sandbox image. This reduces the container's attack surface even further, but you still need the setuid wrapper (and therefore still need Option A) to keep the bridge password hidden from the sandbox user.

**Option C (long term — if you want perfection):**
Modify `Dockerfile.sandbox-epa` so the container starts as `root` (via an entrypoint). The entrypoint launches a tiny daemon that:

1. Reads the PAT file (as root)
2. Resolves the bridge password via `pass-cli`
3. Opens a Unix socket at `/run/pm-cli.sock`
4. Only accepts connections from `uid 1000` (the sandbox user)
5. Accepts a simplified API: `{"action": "list"}`, `{"action": "forward", "uid": 57, "to": "..."}`, etc.
6. Validates `allowed_domains` internally
7. Executes `pm-cli` with the credentials and returns stdout/stderr

Then the entrypoint drops to the sandbox user and runs the agent. The agent talks to the daemon via the Unix socket instead of running `pm-cli` directly.

This works with `no-new-privileges` because the daemon **starts** privileged and stays privileged. `no-new-privileges` only blocks privilege escalation via `execve`, not a process that started privileged.

**Recommended path forward:**

1. **Tomorrow:** Implement Option A (disable `no-new-privileges` for EPA sandbox only) — 1 line change, immediate fix.
2. **Soon after:** Implement Option B (pre-resolve bridge password on host, remove pass-cli + PAT from container) — reduces container attack surface.
3. **Eventually:** Consider Option C (helper daemon) if you want full hardening with all Docker security flags intact.

---

_To-do: Update orchestrator to remove `no-new-privileges` for EPA sandbox; revert PAT file to `root:root 0400`; move pass-cli out of PATH for sandbox user; test pm-cli-secure setuid flow._

# LOG

TL;DR

1. Threat model: Realistic but bounded — this is a dedicated agent email, not your personal account. The blast radius is email abuse. The effort is worthwhile for defence-in-depth, but the current broken model isn't catastrophic.
2. Easier options: Yes. The cleanest is simply disable no-new-privileges for the EPA sandbox only (~1 line). The container already runs read-only + cap-drop ALL, so the risk is minimal. This immediately makes your setuid wrapper design work as intended.
   Below is the full analysis and alternative options ranked by effort.
3. Is the Threat Model Realistic and Worth the Effort?
   Current State
   From what I gathered:

- Container: debian:bookworm-slim, read-only filesystem, cap-drop ALL, no-new-privileges
- Sandbox user: uid 1000 in container (maps to host uid 1000)
- Secrets accessible to sandbox: /run/secrets/proton-pat.env (root:node 0440) and /usr/local/bin/pass-cli
- Broken invariant: sandbox can directly read PAT, run pass-cli, and extract the Proton Bridge password
  What an Attacker Can Actually Do If Credentials Leak
  Credential Blast Radius
  Bridge password Full IMAP/SMTP access to epa.vcom@proton.me. Can read/delete all emails in that account. Crucially, can bypass pm-cli entirely and connect directly to the bridge, allowing emails to any domain — bypassing allowed_domains.
  PAT (Proton Pass token) Read access to the entire "EPA" vault. If this vault contains only the bridge password, then leaking the PAT ≈ leaking the bridge password. If it contains other secrets, it's worse.
  pm-cli config Contains allowed_domains: ["vongerichten.com"] and localhost bridge ports. Not sensitive itself — it's an application-level guardrail that only works when the attacker uses pm-cli.
  Realism Assessment
  Yes, the threat model is realistic, but with important scope limits:
- pm-cli has a good guardrail (allowed_domains), but it ONLY works if the attacker uses pm-cli. With the bridge password, they can go around it.
- The account is single-purpose (epa.vcom@proton.me). The worst case is not "someone takes over my Proton account" — it's "someone sends spam/phishing from my agent email or reads its inbox."
- The no-new-privileges + cap-drop ALL + read-only fs stack is already strong container isolation. An attacker escaping the container is unlikely even with credentials.
  Is It Worth the Effort?
  Yes, but proportionally. Protecting the bridge password is worthwhile because:

1. Even a dedicated account sending unauthorized emails from your domain is a reputation/phishing risk.
2. The PAT's value depends on the EPA vault contents. If it's a single-purpose vault, the incremental risk of PAT exposure is low but not zero.
   However, the current "broken" state isn't a catastrophic breach. It's a design integrity gap: the sandbox has the keys, but the container is still heavily locked down (read-only, no capabilities, no network raw sockets). An attacker would need to break out of the container AND abuse the credentials.
   Bottom line: The security model should be fixed, but don't panic. The blast radius is contained to a single-purpose email account.
3. Easier Options (Ranked by Effort)
   Option A: Remove no-new-privileges for the EPA Sandbox Only ⭐ RECOMMENDED
   Effort: ~1 line change in the orchestrator (however it spawns the sandbox container).
   What to do:
   Wherever the orchestrator spawns the EPA sandbox (docker run or Docker API), simply omit or override no-new-privileges for that specific container. All other containers (openclaw-gateway, openclaw-cli) keep the flag.
   Why this is safe enough:

- The only setuid binary in your custom image is pm-cli-secure — your own code, fully controlled.
- The base Debian slim image has standard system setuid binaries (sudo, passwd, mount, etc.), but these are well-audited and you have cap-drop ALL. Even if sudo is setuid, it requires CAP_SETUID capability to function, which you don't have.
- The filesystem is read-only, so the attacker can't write or modify binaries.
- The container has no network admin capabilities.
  After the change:

1. Revert the PAT file to root:root 0400 (sandbox can't read)
2. Move pass-cli somewhere not in PATH for sandbox (e.g., /usr/local/libexec/) or ensure it's not executable by "other"
3. pm-cli-secure (setuid root, mode 4755) reads the root-only PAT, resolves the bridge password via pass-cli, and runs pm-cli with the secret injected
4. Sandbox user can execute pm-cli-secure but cannot read PAT or run pass-cli directly
   Trade-off: You lose one layer of Docker hardening (no-new-privileges), but you keep all others (cap-drop ALL, read-only, minimal image, user namespace). This is the path of least resistance and restores your originally-designed security model.
   Option B: Resolve Bridge Password on Host, Pass Only the Password Into Container
   Effort: Low-Medium (modifies orchestrator startup logic).
   What to do:
   Before starting the EPA sandbox, the orchestrator (or a host-side script) runs:

# On the host (or init container with full privileges)

export PROTON_PASS_ACCESS_TOKEN=$(cat /run/secrets/proton-pat.env)
BRIDGE_PASSWORD=$(pass-cli read "pass://EPA/ProtonMail Bridge/password")
Then inject BRIDGE_PASSWORD into the sandbox as a Docker secret (mounted as /run/secrets/bridge-password) instead of the PAT. Remove pass-cli and the PAT file from the sandbox image entirely.
The problem:
Even with this, the bridge password is still inside the container and the sandbox user can read the Docker secret file. You'd still need setuid (and therefore still need to remove no-new-privileges) to hide it from the sandbox user. Or you'd need a daemon/proxy approach.
Verdict: Worth doing as a hygiene improvement (reduces what's in the container), but it doesn't solve the core privilege escalation problem by itself. Combine with Option A for best effect.
Option C: Tiny Privileged Helper Daemon Inside the Container
Effort: Medium (build a small Go/Python service).
What to do:
Modify Dockerfile.sandbox-epa so the container starts as root (or via an entrypoint that starts as root), launches a tiny daemon that:

1. Reads the PAT file
2. Resolves the bridge password via pass-cli
3. Opens a Unix socket at /run/pm-cli.sock
4. Only accepts connections from uid 1000 (the sandbox user)
5. Accepts a simplified API: {"action": "list"}, {"action": "forward", "uid": 57, "to": "..."}, etc.
6. Validates allowed_domains internally
7. Executes pm-cli with the credentials and returns stdout/stderr
   The main entrypoint then drops to the sandbox user and runs the agent. The agent talks to the daemon via the Unix socket instead of running pm-cli directly.
   Why this works with no-new-privileges:
   The daemon starts with root privileges from the container startup. It never escalates — it starts privileged and stays privileged. no-new-privileges only blocks privilege escalation via execve, not a process that started privileged.
   Trade-offs:

- More moving parts (need to build + maintain the daemon)
- Need to handle concurrency, error propagation, and socket security
- The agent's pm-cli commands need to be translated to the daemon API
  If the orchestrator can't easily disable no-new-privileges for just this sandbox, this is the cleanest architecture.
  Option D: Sidecar Container (Proxy Pattern)
  Effort: Medium (similar to C, but across container boundaries).
  What to do:
  Run two containers:

1. EPA Proxy (openclaw-sandbox-emt:email-proxy): privileged container with PAT, pass-cli, bridge password. Exposes a restricted API (Unix socket or TCP) for email operations only.
2. EPA Sandbox (openclaw-sandbox-emt:email): unprivileged, no credentials, no new privileges. Talks to the proxy over a shared network namespace or volume-mounted Unix socket.
   Trade-offs:

- Cleanest separation (proxy can even run on the host network namespace)
- But requires orchestrator support for two linked containers
- You'd need to write and maintain the proxy API
  Option E: Accept the Current Risk with Compensating Controls
  Effort: Zero. Monitor instead of block.
  What to do:
  Leave the security model as-is. The sandbox has the PAT and bridge password. Accept it. Add:

1. Proton account monitoring for unusual login/activity on epa.vcom@proton.me
2. Rate limiting in Proton Bridge or at the network level for SMTP sends
3. Network-level egress filtering (e.g., only allow outbound SMTP to vongerichten.com MX)
4. Audit logging of all pm-cli commands via the orchestrator
   Verdict: Given that this is a dedicated email account for an AI agent, and the container is already heavily sandboxed, this is not crazy. But it's not ideal either.
   My Recommendation
   Given the context (your own custom image, read-only fs, cap-drop ALL, minimal software), I recommend:
5. Short term: Disable no-new-privileges for the EPA sandbox container only. This is 1-2 lines in the orchestrator and immediately restores your designed security model.
6. Hygiene: Pre-resolve the bridge password on the host (or in the orchestrator init) and pass it into the container via a Docker secret. Remove pass-cli and the PAT from the sandbox image entirely. The container only needs: pm-cli, pm-cli-secure, and the bridge password.
7. Long term (if you want perfection): Option C — a small privileged helper daemon inside the container. This is the architecture that makes everything work cleanly with all Docker hardening flags enabled. But it's more code.
   Does this trade-off sound reasonable to you? I can implement whichever option you prefer. Or if you have constraints I'm not seeing, let me know.

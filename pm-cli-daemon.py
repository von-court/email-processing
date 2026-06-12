#!/usr/bin/env python3
"""
pm-cli-daemon --- Host-side privileged helper for the EPA sandbox.
Listens on a TCP port, accepts authenticated JSON requests,
resolves the bridge password via pass-cli, validates allowed_domains,
and proxies to the real pm-cli.
"""
import os
import sys
import json
import socket
import logging
import subprocess as sp
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------- Configurables via env ----------
LISTEN_HOST     = os.environ.get("LISTEN_HOST", "10.0.0.1")
LISTEN_PORT     = int(os.environ.get("LISTEN_PORT", "19999"))
AUTH_TOKEN      = os.environ.get("PM_CLI_DAEMON_TOKEN", "")
PAT_FILE        = os.environ.get("PAT_FILE",
    "/home/node/.openclaw/credentials/proton-pat.env")
PM_CLI_PATH     = os.environ.get("PM_CLI_PATH",
    "/usr/local/bin/pm-cli")
PASS_CLI_PATH   = os.environ.get("PASS_CLI_PATH",
    "/usr/local/bin/pass-cli")
PM_CLI_CONFIG   = os.environ.get("PM_CLI_CONFIG",
    "/home/node/.config/pm-cli/config.yaml")

# Hardcoded from vault/item discovery
VAULT_ID  = os.environ.get(
    "VAULT_ID",
    "Cih-X_UHRKbDRk6D3sVPdi9dL7DOyPyT7gJWSQgtXYFlGyN_hh_FdjiljZvPOMh5"
    "ZiLF64bPgkO4VZwyJKn5Bw==")
ITEM_ID   = os.environ.get(
    "ITEM_ID",
    "cT5jlbazEUicY_wWatbNXvsign5fay_B9VN_d6Rmw-EhBhcN97pOHPSCz-PV95"
    "cAJrap2_Y-ZduKed7ZWELjcg==")

# For env var substitution inside the daemon
AGENT_REASON = "pm-cli email operations"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pm-cli-daemon")


# ---------- PAT loading ----------
def _load_pat_env():
    log.info("Loading PAT from %s", PAT_FILE)
    with open(PAT_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            os.environ[key] = value


# ---------- pass-cli session init ----------
def _ensure_login():
    """Personal-access-token login (idempotent within a session)."""
    token = os.environ.get("PROTON_PASS_PERSONAL_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("PROTON_PASS_PERSONAL_ACCESS_TOKEN not set")
    env = os.environ.copy()
    env["PROTON_PASS_AGENT_REASON"] = AGENT_REASON

    # Check if already logged in first
    cp = sp.run(
        [PASS_CLI_PATH, "info"],
        capture_output=True, text=True, env=env,
    )
    if cp.returncode == 0:
        log.info("pass-cli session already active")
        return

    cp = sp.run(
        [PASS_CLI_PATH, "login", "--pat", token],
        capture_output=True, text=True, env=env,
    )
    out = cp.stdout.strip()
    err = cp.stderr.strip()
    if cp.returncode != 0:
        if "already" not in (out + err).lower():
            raise RuntimeError(f"pass-cli login failed: {err or out}")
    log.info("pass-cli session ready")


# ---------- bridge password resolution ----------
def _resolve_bridge_password():
    """Resolve the ProtonMail Bridge password via pass-cli."""
    env = os.environ.copy()
    env["PROTON_PASS_AGENT_REASON"] = AGENT_REASON
    uri = f"pass://{VAULT_ID}/{ITEM_ID}/password"
    cp = sp.run(
        [PASS_CLI_PATH, "item", "view", uri, "--output", "json"],
        capture_output=True, text=True, env=env,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            f"pass-cli failed: {cp.stderr.strip()}")
    password = cp.stdout.strip()
    if not password:
        raise RuntimeError("empty password")
    return password


# ---------- domain validation ----------
def _validate_domains(cmd_args):
    """Ensure --to or to= values only go to allowed domains."""
    for i, arg in enumerate(cmd_args):
        if arg == "--to" and i + 1 < len(cmd_args):
            if not cmd_args[i + 1].endswith("@vongerichten.com"):
                return (False,
                    f"Domain not allowed: {cmd_args[i + 1]}")
        if arg.startswith("to="):
            if not arg[3:].endswith("@vongerichten.com"):
                return (False,
                    f"Domain not allowed: {arg[3:]}")
    return True, ""


# ---------- HTTP server ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(fmt % args)

    def do_POST(self):
        if self.path != "/run":
            self._respond(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req = json.loads(body)
        except json.JSONDecodeError as e:
            self._respond(400, {"error": f"invalid json: {e}"})
            return

        token = req.get("token", "")
        cmd_args = req.get("cmd", [])

        if token != AUTH_TOKEN:
            self._respond(401, {"error": "invalid token"})
            return

        if not isinstance(cmd_args, list) or not cmd_args:
            self._respond(400, {
                "error": "cmd must be a non-empty list"})
            return

        # Validate domain restrictions on sending commands
        sendy_cmds = {"send", "reply", "forward",
            "mail send", "mail reply", "mail forward"}
        candidate = cmd_args[0]
        if candidate in sendy_cmds:
            ok, msg = _validate_domains(cmd_args)
            if not ok:
                self._respond(403, {"error": msg})
                return
        if len(cmd_args) > 1:
            candidate = f"{cmd_args[0]} {cmd_args[1]}"
            if candidate in sendy_cmds:
                ok, msg = _validate_domains(cmd_args)
                if not ok:
                    self._respond(403, {"error": msg})
                    return

        try:
            password = _resolve_bridge_password()
        except RuntimeError as e:
            log.error("Password resolution failed: %s", e)
            self._respond(500, {"error": str(e)})
            return

        env = os.environ.copy()
        env["PM_CLI_BRIDGE_PASSWORD"] = password
        if PM_CLI_CONFIG:
            env["PM_CLI_CONFIG"] = PM_CLI_CONFIG

        log.info("Executing: %s %s",
            PM_CLI_PATH, " ".join(cmd_args))
        cp = sp.run(
            [PM_CLI_PATH] + cmd_args,
            capture_output=True, text=True, env=env,
        )

        self._respond(200 if cp.returncode == 0 else 500, {
            "ok": cp.returncode == 0,
            "code": cp.returncode,
            "stdout": cp.stdout,
            "stderr": cp.stderr,
        })

    def _respond(self, status, body):
        data = json.dumps(body, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---------- Main ----------
if __name__ == "__main__":
    if not AUTH_TOKEN:
        log.error("PM_CLI_DAEMON_TOKEN is not set. Exiting.")
        sys.exit(1)

    if not os.path.isfile(PAT_FILE):
        log.error("PAT file not found: %s. Exiting.", PAT_FILE)
        sys.exit(1)

    _load_pat_env()
    _ensure_login()

    log.info("Starting pm-cli-daemon on %s:%d",
        LISTEN_HOST, LISTEN_PORT)
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
        server.shutdown()

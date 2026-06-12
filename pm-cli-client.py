#!/usr/bin/env python3
"""pm-cli-client --- Sandbox-side client that proxies to host daemon."""
import os
import sys
import json
import socket


DAEMON_ADDR = os.environ.get("PM_CLI_DAEMON_ADDR", "10.0.0.1:19999")
AUTH_TOKEN  = os.environ.get("PM_CLI_DAEMON_TOKEN", "")


def send_request(cmd):
    host, port = DAEMON_ADDR.rsplit(":", 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect((host, int(port)))

    body = json.dumps({"token": AUTH_TOKEN, "cmd": cmd}).encode("utf-8")
    headers = (
        b"POST /run HTTP/1.1\r\n"
        b"Host: %s\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: %d\r\n"
        b"\r\n"
        % (DAEMON_ADDR.encode(), len(body))
    )
    s.sendall(headers + body)

    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
        # Parse Content-Length
        if b"Content-Length:" in data:
            for line in data.split(b"\r\n"):
                if line.startswith(b"Content-Length:"):
                    length = int(line.split(b":")[1].strip())
                    header_end = data.index(b"\r\n\r\n") + 4
                    if len(data) >= header_end + length:
                        s.close()
                        body = data[header_end:header_end + length]
                        return json.loads(body)
    s.close()
    raise RuntimeError("Incomplete response from daemon")


def main():
    if not AUTH_TOKEN:
        print("Error: PM_CLI_DAEMON_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    resp = send_request(sys.argv[1:])

    if "stdout" in resp:
        print(resp["stdout"], end="")
    if "stderr" in resp and resp["stderr"]:
        print(resp["stderr"], end="", file=sys.stderr)

    sys.exit(resp.get("code", 1))


if __name__ == "__main__":
    main()

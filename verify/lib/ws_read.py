#!/usr/bin/env python3
"""Read a jRemote session's PTY WebSocket the way the app does, and print what
the terminal actually shows. Stdlib only — runs on a clean guest's python.

    ws_read.py <sid> <bearer-token> <seconds> [host] [port]

Connects to /api/jremote/v1/sessions/<sid>/pty with an Authorization: Bearer
header (the same gate the app uses), reads frames for <seconds>, and writes
every text and binary payload to stdout as raw bytes — exactly the stream the
app paints on the glass. The verdict is read from this by the scenario: a
terminal flooding "can't find terminfo database" fails there, not here.
"""
import base64, os, socket, sys, time

sid, token, secs = sys.argv[1], sys.argv[2], float(sys.argv[3])
host = sys.argv[4] if len(sys.argv) > 4 else "127.0.0.1"
port = int(sys.argv[5]) if len(sys.argv) > 5 else 9090

key = base64.b64encode(os.urandom(16)).decode()
req = (
    f"GET /api/jremote/v1/sessions/{sid}/pty?cols=120&rows=40 HTTP/1.1\r\n"
    f"Host: {host}:{port}\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {key}\r\n"
    "Sec-WebSocket-Version: 13\r\n"
    f"Authorization: Bearer {token}\r\n"
    "\r\n"
)
s = socket.create_connection((host, port), timeout=secs + 5)
s.sendall(req.encode())

# Read the handshake response headers.
buf = b""
while b"\r\n\r\n" not in buf:
    chunk = s.recv(4096)
    if not chunk:
        sys.stderr.write("WS-CLOSED-BEFORE-HANDSHAKE\n"); sys.exit(3)
    buf += chunk
head, _, rest = buf.partition(b"\r\n\r\n")
if b" 101 " not in head.split(b"\r\n")[0]:
    sys.stderr.write("WS-HANDSHAKE-FAILED: " + head.split(b"\r\n")[0].decode(errors="replace") + "\n")
    sys.exit(4)

pending = rest
deadline = time.time() + secs
s.settimeout(0.5)

def need(n):
    global pending
    while len(pending) < n:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            if time.time() > deadline:
                return False
            continue
        if not chunk:
            return False
        pending += chunk
    return True

out = sys.stdout.buffer
while time.time() < deadline:
    if not need(2):
        break
    b0, b1 = pending[0], pending[1]
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    ln = b1 & 0x7F
    hdr = 2
    if ln == 126:
        if not need(4):
            break
        ln = int.from_bytes(pending[2:4], "big"); hdr = 4
    elif ln == 127:
        if not need(10):
            break
        ln = int.from_bytes(pending[2:10], "big"); hdr = 10
    mask = b""
    if masked:
        if not need(hdr + 4):
            break
        mask = pending[hdr:hdr + 4]; hdr += 4
    if not need(hdr + ln):
        break
    payload = pending[hdr:hdr + ln]
    pending = pending[hdr + ln:]
    if masked:
        payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    if opcode == 0x8:      # close
        break
    if opcode in (0x1, 0x2):  # text or binary — both are what the user sees
        out.write(payload)
out.flush()

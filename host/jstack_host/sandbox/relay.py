"""One link's relay: TCP and UDP from one guest, at one address, to one target.

It binds the host's address on the guest's network alone, so nothing on the
host's own network can reach it, and drops anything not from the guest it was
opened for. It prints `ready` once both sockets are bound, or why not, then
carries traffic until it is killed with its lease.
"""
from __future__ import annotations

import select
import socket
import sys
import threading


def _pump(a: socket.socket, b: socket.socket) -> None:
    try:
        while data := a.recv(65536):
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _tcp(server: socket.socket, allow: str, target: tuple) -> None:
    while True:
        conn, (ip, _) = server.accept()
        if ip != allow:
            conn.close()
            continue
        try:
            up = socket.create_connection(target, timeout=10)
        except OSError:
            conn.close()
            continue
        up.settimeout(None)
        for a, b in ((conn, up), (up, conn)):
            threading.Thread(target=_pump, args=(a, b), daemon=True).start()


def _udp(server: socket.socket, allow: str, target: tuple) -> None:
    up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    up.connect(target)
    peer = None
    while True:
        ready, _, _ = select.select([server, up], [], [])
        if server in ready:
            data, src = server.recvfrom(65536)
            if src[0] == allow:
                peer = src
                up.send(data)
        if up in ready:
            try:
                data = up.recv(65536)
            except OSError:
                continue
            if peer:
                server.sendto(data, peer)


def serve(listen: str, port: int, allow: str, to: str, to_port: int) -> int:
    try:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind((listen, port))
        tcp.listen(16)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind((listen, port))
    except OSError as exc:
        print(f"cannot bind {listen}:{port}: {exc.strerror}", flush=True)
        return 1
    print("ready", flush=True)
    sys.stdout.close()
    target = (to, to_port)
    threading.Thread(target=_udp, args=(udp, allow, target), daemon=True).start()
    _tcp(tcp, allow, target)
    return 0

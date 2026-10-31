#!/usr/bin/env python3
"""Tell the Presto where Music Assistant is, so the Pi needs no fixed IP.

Every BEACON_INTERVAL seconds, broadcasts one UDP datagram to port BEACON_PORT:

    {"service": "albumbox", "url": "http://192.168.1.236:8095"}

It also answers the datagram b"albumbox?" (sent by the Presto at boot, so it
need not wait for the next broadcast) with the same payload, sent back to the asker.

Standard library only. Runs in the "beacon" service of docker-compose.yml.
"""
import json
import os
import socket
import time

PORT = int(os.environ.get("BEACON_PORT", "50505"))
INTERVAL = float(os.environ.get("BEACON_INTERVAL", "5"))
MA_PORT = int(os.environ.get("MA_PORT", "8095"))
QUERY = b"albumbox?"


def lan_ip():
    """The address of the interface that carries the default route (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1; connect() on UDP only picks a route
        return s.getsockname()[0]


def payload():
    return json.dumps({"service": "albumbox", "url": f"http://{lan_ip()}:{MA_PORT}"}).encode()


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", PORT))
    sock.settimeout(INTERVAL)
    last_ip = None
    next_broadcast = 0.0
    while True:
        now = time.monotonic()
        if now >= next_broadcast:
            try:
                data = payload()
                ip = json.loads(data)["url"]
                if ip != last_ip:
                    print("announcing", ip, flush=True)
                    last_ip = ip
                sock.sendto(data, ("255.255.255.255", PORT))
            except OSError as e:  # no network yet; try again next round
                print("broadcast failed:", e, flush=True)
            next_broadcast = now + INTERVAL
        try:
            msg, addr = sock.recvfrom(64)
        except socket.timeout:
            continue
        except OSError:
            time.sleep(1)
            continue
        if msg.strip() == QUERY:
            try:
                sock.sendto(payload(), addr)
            except OSError as e:
                print("reply failed:", e, flush=True)


if __name__ == "__main__":
    main()

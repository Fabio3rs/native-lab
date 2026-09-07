#!/usr/bin/env bash


# dentro do sandbox
python3 - <<'PY'
import socket
s = socket.socket()
s.settimeout(1)
for host, port in [
    ("127.0.0.1", 22),
    ("1.1.1.1", 443),
]:
    try:
        s.connect((host, port))
        print(host, port, "CONNECTED")
    except Exception as e:
        print(host, port, type(e).__name__, e)
PY



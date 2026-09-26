"""Small local RPC boundary. Only the native tick executes control operations."""

import fcntl
import json
import os
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path

from drivingbench.shared.contracts import NATIVE_WATCHDOG_S, PROTOCOL

MAX_PACKET = 65536


def encode(value):
    data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    if len(data) > MAX_PACKET:
        raise ValueError("native_request_too_large")
    return data


class NativeServer:
    """Nonblocking datagrams; an OS lock permits cleanup of a crashed server's socket."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(str(self.path) + ".lock", "a")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if self.path.exists():
            if not stat.S_ISSOCK(self.path.lstat().st_mode):
                raise ValueError("native_socket_path_occupied")
            self.path.unlink()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.setblocking(False)
        self.socket.bind(str(self.path))
        os.chmod(self.path, 0o600)

    def poll(self, handler):
        for _ in range(32):
            try:
                data, peer = self.socket.recvfrom(MAX_PACKET + 1)
            except BlockingIOError:
                break
            try:
                if len(data) > MAX_PACKET:
                    raise ValueError("native_request_too_large")
                request = json.loads(data)
                if not isinstance(request, dict) or request.get("protocol") != PROTOCOL:
                    result = {"error": "incompatible_protocol", "protocol": PROTOCOL}
                else:
                    result = handler(request)
            except (ValueError, TypeError, KeyError):
                result = {"error": "invalid_request"}
            try:
                self.socket.sendto(encode(result), peer)
            except OSError:
                pass  # A caller can disappear after acceptance. Never replay its operation.

    def close(self):
        self.socket.close()
        self.path.unlink(missing_ok=True)
        self.lock.close()


class NativeClient:
    """The producer's local heartbeat is independent of HTTP/image requests."""

    def __init__(self, path, *, timeout=5, on_status=None):
        self.path, self.timeout, self.on_status = str(path), timeout, on_status
        self._stop = threading.Event()
        self._thread = None
        self._snapshot = {"protocol": PROTOCOL, "state": "unavailable", "reason": "native_offline"}

    def call(self, body, *, timeout=None):
        with tempfile.TemporaryDirectory(prefix="db-ipc-") as directory:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
                client.bind(str(Path(directory) / "reply"))
                client.settimeout(self.timeout if timeout is None else timeout)
                client.sendto(encode(body), self.path)
                data = client.recv(MAX_PACKET)
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("invalid_native_response")
        return result

    def status(self):
        return dict(self._snapshot)

    def start(self):
        def poll():
            while not self._stop.is_set():
                try:
                    value = self.call({"protocol": PROTOCOL, "operation": "heartbeat"}, timeout=0.2)
                    value["received_at"] = time.monotonic()
                    self._snapshot = value
                    if self.on_status:
                        self.on_status(value)
                except (OSError, ValueError):
                    if time.monotonic() - self._snapshot.get("received_at", 0) > NATIVE_WATCHDOG_S:
                        self._snapshot = {
                            "protocol": PROTOCOL,
                            "state": "unavailable",
                            "reason": "native_offline",
                        }
                self._stop.wait(0.05)

        self._thread = threading.Thread(target=poll, name="native-status", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.timeout + 1)

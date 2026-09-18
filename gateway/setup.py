"""Laptop connection configuration and the single managed SSH forward."""

import hashlib
import json
import os
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from drivingbench.shared.contracts import PROTOCOL


def config_path() -> Path:
    return Path.home() / ".config/drivingbench-v01/config.json"


@dataclass(frozen=True)
class Config:
    ssh_host: str = "comma"
    ssh_user: str = "comma"
    ssh_key: str | None = None
    producer_port: int = 8876
    tunnel_port: int = 8877
    web_port: int = 8766
    device_root: str = "/data/drivingbench-v01"
    # Shared Hugging Face dataset for labeled-session artifacts; None disables uploads.
    dataset: str | None = None
    repo: str | None = None  # checkout whose traces/ the viewer reads; set by install
    # Extra transcript locations, label -> glob (e.g. a second Codex home).
    extra_transcripts: dict[str, str] = field(default_factory=dict)
    client_configs: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if set(self.client_configs) - {"codex", "claude", "cursor"} or not all(
            isinstance(path, str) and path for path in self.client_configs.values()
        ):
            raise ValueError("client_configs must map supported clients to file paths")
        if not self.ssh_host or self.ssh_host.startswith("-"):
            raise ValueError("ssh_host must be a hostname or SSH alias")
        if not self.ssh_user or self.ssh_user.startswith("-"):
            raise ValueError("ssh_user must be a username")
        if not self.device_root.startswith("/data/") or self.device_root.rstrip("/") == "/data":
            raise ValueError("device_root must be a directory below /data")
        for value in (self.producer_port, self.tunnel_port, self.web_port):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
                raise ValueError("ports must be integers between 1 and 65535")

    @property
    def producer_url(self):
        return f"http://127.0.0.1:{self.tunnel_port}"

    @property
    def client_paths(self):
        from drivingbench.gateway.install import client_paths

        return client_paths() | {
            name: Path(path).expanduser().resolve() for name, path in self.client_configs.items()
        }

    @classmethod
    def load(cls, path: Path | None = None):
        source = path or config_path()
        return cls(**json.loads(source.read_text())) if source.exists() else cls()

    def save(self, path: Path | None = None):
        from drivingbench.gateway.install import atomic_write

        atomic_write(path or config_path(), json.dumps(asdict(self), indent=2).encode() + b"\n")


def ssh_args(config: Config) -> list[str]:
    args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=30",
    ]
    if config.ssh_key:
        args.extend(["-i", str(Path(config.ssh_key).expanduser())])
    return args + ["-l", config.ssh_user, config.ssh_host]


def mirror_recordings(config: Config, mirror: Path) -> Path:
    """Incrementally copy the comma's recordings, full-size images included, into an ignored dir."""
    mirror = mirror.expanduser().resolve()
    mirror.mkdir(parents=True, exist_ok=True)
    remote = shlex.quote(config.device_root.rstrip("/") + "/state/recordings/")
    subprocess.run(
        [
            "rsync",
            "-a",
            "--partial",
            "-e",
            shlex.join(ssh_args(config)[:-1]),
            "--",
            f"{config.ssh_host}:{remote}",
            str(mirror) + "/",
        ],
        check=True,
    )
    return mirror


def bring_online(config: Config) -> dict:
    """Start only the installed release; never deploy while connecting."""
    directory = config_path().parent
    directory.mkdir(parents=True, exist_ok=True)
    identity = f"{config.ssh_user}@{config.ssh_host}:{config.producer_port}:{config.tunnel_port}"
    control = str(directory / f"ssh-{hashlib.sha256(identity.encode()).hexdigest()[:8]}.sock")
    base = ssh_args(config)
    check = subprocess.run(
        base[:-1] + ["-S", control, "-O", "check", base[-1]], capture_output=True
    )
    if check.returncode:
        subprocess.run(
            base[:-1]
            + [
                "-M",
                "-S",
                control,
                "-fNT",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=3",
                "-L",
                f"127.0.0.1:{config.tunnel_port}:127.0.0.1:{config.producer_port}",
                base[-1],
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    launcher = shlex.quote(config.device_root.rstrip("/") + "/current/start-producer")
    subprocess.run(
        base + [f"{launcher} --ensure-running"], check=True, capture_output=True, timeout=60
    )
    deadline = time.monotonic() + 15
    while True:
        try:
            response = httpx.get(config.producer_url + "/status", timeout=5, trust_env=False)
            response.raise_for_status()
            health = response.json()
            break
        except httpx.HTTPError:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Installed producer did not start; inspect its producer.log"
                ) from None
            time.sleep(0.2)
    if health.get("protocol") != PROTOCOL:
        raise RuntimeError("Installed producer protocol differs; deploy a compatible release")
    return {
        "status": "online",
        "protocol": health["protocol"],
        "release": health.get("release"),
        "state": health.get("state"),
        "reason": health.get("reason"),
    }


def gateway_environment() -> dict[str, str]:
    """launchd has a sparse PATH; preserve common SSH/uv locations explicitly."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}

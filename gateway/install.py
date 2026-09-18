"""Small, backed-up edits to client configuration and the laptop service."""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import tomlkit

SERVER_NAME = "drivingbench_sandbox"
GATEWAY_LABEL = "com.drivingbench.v01.gateway"


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def client_paths(home: Path | None = None) -> dict[str, Path]:
    home = home or Path.home()
    return {
        "codex": home / ".codex/config.toml",
        "claude": home / ".claude.json",
        "cursor": home / ".cursor/mcp.json",
    }


def render_config(client: str, original: bytes, command: str, gateway_url: str) -> bytes:
    entry = {"command": command, "args": ["--gateway-url", gateway_url, "--client", client]}
    if client == "codex":
        document = tomlkit.parse(original.decode())
        servers = document.setdefault("mcp_servers", tomlkit.table())
    else:
        document = json.loads(original or b"{}")
        if not isinstance(document, dict):
            raise ValueError("Client configuration must be an object")
        servers = document.setdefault("mcpServers", {})
    if not hasattr(servers, "items"):
        raise ValueError("MCP server configuration must be an object")
    servers[SERVER_NAME] = entry
    return (
        tomlkit.dumps(document) if client == "codex" else json.dumps(document, indent=2) + "\n"
    ).encode()


RUNTIME = Path.home() / ".local/share/drivingbench-v01"


def install_runtime(repo: Path, runtime: Path = RUNTIME) -> Path:
    """Copy this checkout into a venv outside ~/Documents and friends.

    macOS refuses launchd agents and GUI apps access to protected folders, so the
    gateway service and the MCP executables the chat apps spawn must live elsewhere.
    Re-running install updates the copy; the checkout itself can move afterwards.
    """
    runtime.mkdir(parents=True, exist_ok=True)
    venv = runtime / "venv"
    subprocess.run(["uv", "venv", "--quiet", "--allow-existing", str(venv)], check=True)
    subprocess.run(
        ["uv", "pip", "install", "--quiet", "--python", str(venv / "bin/python"), str(repo)],
        check=True,
    )
    return venv / "bin"


def install_clients(clients, command: Path, gateway_url: str, *, paths=None) -> list[dict]:
    command = command.resolve()
    if not command.is_file() or not os.access(command, os.X_OK):
        raise ValueError(f"Missing executable: {command}; run uv sync --locked first")
    paths = paths or client_paths()
    prepared = []
    # Parse every requested config before editing any of them.
    for client in dict.fromkeys(clients):
        path = paths[client].expanduser().resolve()
        original = path.read_bytes() if path.exists() else b""
        content = render_config(client, original, str(command), gateway_url)
        prepared.append((client, path, original, content))
    results = []
    for client, path, original, content in prepared:
        current = path.read_bytes() if path.exists() else b""
        if current != original:
            raise RuntimeError(f"Client configuration changed during installation: {path}; retry")
        backup = None
        if content != original:
            if path.exists():
                backup = path.with_name(f"{path.name}.drivingbench-{time.time_ns()}.bak")
                shutil.copy2(path, backup)
            atomic_write(path, content)
        results.append(
            {"client": client, "path": str(path), "backup": str(backup) if backup else None}
        )
    return results


def installation_status(*, paths=None, executables: Path = RUNTIME / "venv/bin") -> dict:
    clients = {}
    expected_command = (executables / "drivingbench-sandbox").resolve()
    for client, path in (paths or client_paths()).items():
        try:
            document = (
                tomlkit.parse(path.read_text())
                if client == "codex"
                else json.loads(path.read_text())
            )
            entry = document.get("mcp_servers" if client == "codex" else "mcpServers", {}).get(
                SERVER_NAME
            )
            installed = bool(
                entry
                and Path(entry.get("command", "")).is_file()
                and Path(entry["command"]).resolve() == expected_command
                and "--gateway-url" in entry.get("args", [])
            )
            clients[client] = {"installed": installed, "path": str(path)}
        except (OSError, ValueError, TypeError):
            clients[client] = {"installed": False, "path": str(path)}
    return {"clients": clients}


def install_gateway(executable: Path, config_file: Path):
    if sys.platform != "darwin":
        return {
            "status": "foreground",
            "command": [str(executable), "serve", "--config", str(config_file)],
        }
    from drivingbench.gateway.setup import gateway_environment

    label = GATEWAY_LABEL
    agents = Path.home() / "Library/LaunchAgents"
    plist = agents / f"{label}.plist"
    logs = config_file.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    value = {
        "Label": label,
        "ProgramArguments": [
            str(executable.resolve()),
            "serve",
            "--config",
            str(config_file.resolve()),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": gateway_environment(),
        "StandardOutPath": str(logs / "gateway.log"),
        "StandardErrorPath": str(logs / "gateway.log"),
    }
    atomic_write(plist, plistlib.dumps(value))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
    # bootout is asynchronous; bootstrap fails with EIO (5) until the old job is torn down.
    for attempt in range(10):
        done = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True)
        if done.returncode == 0:
            break
        if attempt == 9:
            raise RuntimeError(f"launchctl bootstrap failed: {done.stderr.decode().strip()}")
        time.sleep(0.5)
    return {"status": "installed", "plist": str(plist)}

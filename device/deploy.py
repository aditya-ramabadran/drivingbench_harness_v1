"""Build a versioned bundle; activate it on a parked comma with checked native edits.

Run ``drivingbench bundle`` on the laptop: it stages the complete aarch64 Python
tree, the modified openpilot files from ``native/`` with their upstream digests,
the reviewed settings and this script. Copy and extract that archive on the
comma, then run its activate.py with the native Python. Restart openpilot after
activation; Bring online only starts the installed producer. See
docs/deployment.md.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

# native/upstream/ holds the stock openpilot files at the pinned commit and
# native/drivingbench/ the modified versions; docs/deployment.md records the changes.
NATIVE = Path("native")
# comma four: aarch64 Linux, /usr/local/venv/bin/python 3.12.
DEVICE_PLATFORM = [
    "--python-platform",
    "aarch64-manylinux_2_31",
    "--python-version",
    "3.12",
    "--only-binary",
    ":all:",
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build_bundle(repo: Path, output: Path, settings_file: Path) -> Path:
    from drivingbench.shared.contracts import Settings, SpeedSettings

    settings = json.loads(settings_file.read_text())
    value = settings.get("settings", settings)
    if set(value.get("speed_control", {})) != set(SpeedSettings.model_fields):
        raise ValueError("Settings must explicitly include all deployed speed_control coefficients")
    Settings.model_validate(value)
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)
    if dirty.strip():
        raise ValueError("Commit the release before bundling; source checkout must be clean")
    release = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError(f"Bundle already exists: {output}")
    with tempfile.TemporaryDirectory(prefix="drivingbench-bundle-") as temporary:
        stage = Path(temporary)
        wheels = stage / "wheel"
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(wheels)], cwd=repo, check=True)
        subprocess.run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(stage / "requirements.txt"),
            ],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        # Stage the complete device Python tree here, so activation on the comma
        # needs neither network nor pip: only exact, hash-checked wheels.
        target = ["uv", "pip", "install", "--target", str(stage / "python")] + DEVICE_PLATFORM
        subprocess.run(
            target + ["--require-hashes", "-r", str(stage / "requirements.txt")],
            cwd=repo,
            check=True,
        )
        (wheel,) = wheels.glob("*.whl")
        subprocess.run(target + ["--no-deps", str(wheel)], cwd=repo, check=True)
        native = stage / "native"
        upstream = json.loads((repo / NATIVE / "upstream.json").read_text())
        manifest = {"release": release, "protocol": 2, "upstream": upstream["commit"], "native": {}}
        for name in upstream["files"]:
            original = (repo / NATIVE / "upstream" / name).read_bytes()
            content = (repo / NATIVE / "drivingbench" / name).read_bytes()
            destination = native / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            manifest["native"][name] = {"baseline": digest(original), "installed": digest(content)}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        shutil.copy2(settings_file, stage / "initial-settings.json")
        shutil.copy2(__file__, stage / "activate.py")
        with tarfile.open(output, "w:gz") as archive:
            for path in sorted(stage.iterdir()):
                archive.add(path, arcname=path.name)
    return output


def require_park(native_root: Path):
    """Read new native publications, including before our adapter is installed."""
    import sys

    sys.path.insert(0, str(native_root))
    import openpilot.cereal.messaging as messaging

    samples = messaging.SubMaster(["carState", "selfdriveState"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        samples.update(100)
        if all(samples.seen.values()) and samples.all_checks():
            car = samples["carState"]
            drive = samples["selfdriveState"]
            if str(car.gearShifter) != "park" or abs(car.vEgo) >= 0.05 or drive.enabled:
                raise RuntimeError("Activation requires Park, zero speed, and openpilot disengaged")
            return
    raise RuntimeError("Fresh native Park/disengaged telemetry unavailable; no files changed")


def verify_native(bundle: Path, native_root: Path, previous: Path | None):
    manifest = json.loads((bundle / "manifest.json").read_text())
    prior = json.loads((previous / "manifest.json").read_text()) if previous else {"native": {}}
    for name, expected in manifest["native"].items():
        content = (bundle / "native" / name).read_bytes()
        if digest(content) != expected["installed"]:
            raise RuntimeError(f"Bundle content mismatch: {name}")
        allowed = {expected["baseline"], expected["installed"]}
        if name in prior["native"]:
            allowed.add(prior["native"][name]["installed"])
        if digest((native_root / name).read_bytes()) not in allowed:
            raise RuntimeError(f"Unknown native edits in {name}; compare them before activation")
    return manifest


def atomic_bytes(path: Path, content: bytes):
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


def point_link(path: Path, target: Path):
    temporary = path.with_name(path.name + ".next")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, path)


def stop_previous_producer(root: Path, previous: Path | None):
    """Stop only the process that owns our lock and imports our previous release."""
    import fcntl

    path = root / "state/producer.lock"
    if not path.exists():
        return
    with path.open("r") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return  # No process currently owns the producer lock.
        except BlockingIOError:
            pass
        pid = int(lock.read().strip())
        process = Path("/proc") / str(pid)
        command = (process / "cmdline").read_bytes().split(b"\0")
        environment = (process / "environ").read_bytes().split(b"\0")
        expected = f"PYTHONPATH={previous / 'python'}:".encode() if previous else b""
        if (
            not previous
            or b"drivingbench.device.service" not in command
            or not any(item.startswith(expected) for item in environment)
        ):
            raise RuntimeError("Producer lock belongs to an unknown process; stop it explicitly")
        # The start-producer loop restarts a producer it did not see stop cleanly; end it first.
        parent = int((process / "stat").read_text().split(")")[-1].split()[1])
        if b"start-producer" in b" ".join(
            Path(f"/proc/{parent}/cmdline").read_bytes().split(b"\0")
        ):
            os.kill(parent, signal.SIGTERM)
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                time.sleep(0.1)
        raise RuntimeError("Prior producer did not exit; activation has not changed native files")


def require_free_port(port: int, timeout: float = 10.0):
    """The retired field producer also used 8876; never race it for the port.

    A just-stopped producer's forked camera workers hold its listening socket for a
    moment after it exits, so the port is given a little time to clear.
    """
    import socket

    deadline = time.monotonic() + timeout
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # Like uvicorn's own bind: the gateway's polling leaves TIME-WAIT sockets on
            # this port for a minute after a stop, and only a listener is a real conflict.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Port {port} is in use; stop the previous DrivingBench producer first"
                    ) from None
        time.sleep(0.2)


SMOKE_IMPORTS = (
    "drivingbench.device.service",
    "drivingbench.device.native",
    "drivingbench.controller.core",
    "openpilot.cereal.messaging",
    "msgq.visionipc",
    "PIL.Image",
    "numpy",
)


def smoke_imports(python: Path, staged: Path, native_root: Path):
    """Import the staged runtime with the native interpreter before any native file changes."""
    code = "import importlib, sys\nfor name in sys.argv[1:]:\n    importlib.import_module(name)\n"
    code += "from msgq.visionipc import VisionIpcClient  # noqa: F401\n"
    result = subprocess.run(
        [str(python), "-c", code, *SMOKE_IMPORTS],
        env={
            **os.environ,
            "PYTHONPATH": f"{staged / 'python'}:{native_root}",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise RuntimeError(f"Staged runtime failed to import: {detail}")


def identical(bundle: Path, release: Path) -> bool:
    """Every bundle file is present byte-for-byte in the release (activation adds extras)."""
    import filecmp

    return all(
        filecmp.cmp(path, release / path.relative_to(bundle), shallow=False)
        for path in bundle.rglob("*")
        if path.is_file()
    )


def stage_release(bundle, release, checks):
    """Copy into releases/ atomically, re-checking before the rename; identical is reused."""
    if release.exists():
        if identical(bundle, release):
            return
        raise RuntimeError(f"{release} holds a different build of this commit; remove it first")
    release.parent.mkdir(parents=True, exist_ok=True)
    partial = release.with_name(release.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    try:
        shutil.copytree(bundle, partial)
        checks(partial)
        os.replace(partial, release)
    finally:
        shutil.rmtree(partial, ignore_errors=True)


def activate(bundle: Path, root: Path, native_root: Path, python: Path, port: int = 8876):
    if not root.is_absolute() or root.parent == Path("/"):
        raise ValueError("Choose an absolute dedicated release directory")
    if not (bundle / "python/drivingbench/device/service.py").is_file():
        raise RuntimeError("Bundle is missing its staged python tree; rebuild it with bundle")
    previous = (root / "current").resolve() if (root / "current").exists() else None
    # Every precondition before anything is written, so a failed attempt is simply retried.
    manifest = verify_native(bundle, native_root, previous)
    require_park(native_root)
    stop_previous_producer(root, previous)
    require_free_port(port)
    release = root / "releases" / manifest["release"]

    def checks(_staged):
        require_park(native_root)  # Copying may take a moment; re-read Park.
        verify_native(bundle, native_root, previous)

    stage_release(bundle, release, checks)
    smoke_imports(python, release, native_root)
    environment = native_root / "launch_env.sh"
    original = environment.read_bytes()
    backup = release / "native-backup"
    if previous != release:  # Re-activating the current release must keep its rollback copy.
        for name in manifest["native"]:
            destination = backup / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(native_root / name, destination)
        (backup / "launch_env.sh").write_bytes(original)
    exports = (
        f"\n# DrivingBench v0.1 release {manifest['release']}\n"
        f"export DRIVINGBENCH_MODE=experimental\n"
        f"export DRIVINGBENCH_PYTHON_ROOT={shlex.quote(str(release / 'python'))}\n"
        f"export DRIVINGBENCH_NATIVE_SOCKET={shlex.quote(str(root / 'native.sock'))}\n"
    )
    # Replace retired exports, including the now-unconditional DM setting.
    cleaned = re.sub(
        rb"^(# DrivingBench[^\n]*|export DRIVINGBENCH_[^\n]*)\n", b"", original, flags=re.M
    )
    cleaned = cleaned.rstrip(b"\n") + b"\n" if cleaned.strip() else b""
    command = [
        str(python),
        "-m",
        "drivingbench.device.service",
        "--state-dir",
        str(root / "state"),
        "--native-socket",
        str(root / "native.sock"),
        "--port",
        str(port),
        "--settings-file",
        str(release / "initial-settings.json"),
        "--release",
        manifest["release"],
    ]
    launcher = (
        "#!/bin/sh\nset -eu\n"
        f"export PYTHONPATH={shlex.quote(str(release / 'python') + ':' + str(native_root))}\n"
        f"mkdir -p {shlex.quote(str(root / 'state'))}\n"
        f"{shlex.quote(str(python))} - {shlex.quote(str(root / 'state/producer.lock'))} <<'PY' || status=$?\n"
        "import fcntl, sys\n"
        "with open(sys.argv[1], 'a') as lock:\n"
        "    try:\n"
        "        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    except BlockingIOError:\n"
        "        sys.exit(42)\n"
        "PY\n"
        'if [ "${status:-0}" = 42 ]; then exit 0; fi\n'
        'if [ "${status:-0}" != 0 ]; then exit "$status"; fi\n'
        # A crashing producer is restarted a few times, then left down for the operator.
        # A clean exit or a SIGTERM (143: activation, rollback) is deliberate and never restarted.
        "run() {\n"
        "  trap '' HUP\n"
        "  attempt=0\n"
        '  while [ "$attempt" -lt 5 ]; do\n'
        "    attempt=$((attempt + 1))\n"
        f"    {shlex.join(command)} && exit 0\n"
        '    status=$?; if [ "$status" = 143 ]; then exit 0; fi\n'
        '    echo "producer exited abnormally (status $status, start $attempt of 5); restarting in 2 s"\n'
        "    sleep 2\n"
        "  done\n"
        '  echo "producer gave up after 5 starts; inspect this log, then Bring online again"\n'
        "}\n"
        f"run >>{shlex.quote(str(root / 'state/producer.log'))} 2>&1 </dev/null &\n"
    )
    (release / "start-producer").write_text(launcher)
    (release / "start-producer").chmod(0o755)
    try:
        for name in manifest["native"]:
            atomic_bytes(native_root / name, (release / "native" / name).read_bytes())
        atomic_bytes(environment, cleaned + exports.encode())
        if previous and previous != release:
            point_link(root / "previous", previous)
        point_link(root / "current", release)
    except OSError:
        # Restore what this attempt found; on re-activation that is the current release.
        for name in manifest["native"]:
            source = backup / name if previous != release else release / "native" / name
            atomic_bytes(native_root / name, source.read_bytes())
        atomic_bytes(environment, original)
        raise
    return {
        "release": manifest["release"],
        "restart_required": True,
        "next": "Restart openpilot while parked; then Bring online.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/data/drivingbench-v01"))
    parser.add_argument("--native-root", type=Path, default=Path("/data/openpilot"))
    parser.add_argument("--python", type=Path, default=Path("/usr/local/venv/bin/python"))
    args = parser.parse_args()
    print(
        json.dumps(
            activate(Path(__file__).resolve().parent, args.root, args.native_root, args.python),
            indent=2,
        )
    )

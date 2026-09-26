import json
import subprocess
from functools import partial
from pathlib import Path

import pytest
import tomlkit
from drivingbench.gateway.install import install_clients, render_config
from drivingbench.gateway.setup import Config, bring_online


@pytest.mark.parametrize("client", ["codex", "claude", "cursor"])
def test_install_preserves_other_servers_and_replaces_ours(client, tmp_path):
    servers = {
        "unrelated": {"command": "/keep"},
        "drivingbench_sandbox": {"command": "/old", "enabled_tools": ["old"]},
    }
    key = "mcp_servers" if client == "codex" else "mcpServers"
    document = {key: servers, "unrelated_setting": "keep"}
    original = (tomlkit.dumps(document) if client == "codex" else json.dumps(document)).encode()
    path = tmp_path / "config"
    path.write_bytes(original)
    executable = tmp_path / "drivingbench-sandbox"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    result = install_clients([client], executable, "http://127.0.0.1:8766", paths={client: path})
    updated = tomlkit.parse(path.read_text()) if client == "codex" else json.loads(path.read_text())
    assert updated["unrelated_setting"] == "keep"
    assert set(updated[key]) == {"unrelated", "drivingbench_sandbox"}
    assert updated[key]["unrelated"] == {"command": "/keep"}
    assert "enabled_tools" not in updated[key]["drivingbench_sandbox"]
    assert Path(result[0]["backup"]).read_bytes() == original
    assert (
        install_clients([client], executable, "http://127.0.0.1:8766", paths={client: path})[0][
            "backup"
        ]
        is None
    )


def test_codex_comments_preserved():
    result = render_config(
        "codex", b'# Keep this comment\nmodel = "test"\n', "/new", "http://localhost"
    )
    assert b"# Keep this comment" in result


def test_invalid_second_config_does_not_edit_first(tmp_path):
    executable = tmp_path / "exe"
    executable.touch(mode=0o700)
    paths = {"codex": tmp_path / "codex", "claude": tmp_path / "claude"}
    paths["codex"].write_text('model = "keep"\n')
    paths["claude"].write_text("not json")
    with pytest.raises(ValueError):
        install_clients(["codex", "claude"], executable, "http://localhost", paths=paths)
    assert paths["codex"].read_text() == 'model = "keep"\n'


def test_connection_starts_only_installed_release(monkeypatch, tmp_path):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr("drivingbench.gateway.setup.config_path", lambda: tmp_path / "config.json")

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1 if "check" in args else 0)

    monkeypatch.setattr("drivingbench.gateway.setup.subprocess.run", run)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"protocol": 2, "release": "abc"}

    monkeypatch.setattr("drivingbench.gateway.setup.httpx.get", lambda *a, **kw: Response())
    assert bring_online(Config(ssh_host="my-comma"))["release"] == "abc"
    assert any("StrictHostKeyChecking=yes" in call for call in calls)
    assert calls[-1][-1] == "/data/drivingbench-v01/current/start-producer --ensure-running"
    assert not any("install" in arg or "arm" in arg for call in calls for arg in call)


def test_config_roundtrip_and_port_validation(tmp_path):
    path = tmp_path / "config.json"
    config = Config(ssh_host="host", tunnel_port=9000)
    config.save(path)
    assert Config.load(path) == config
    assert config.producer_url == "http://127.0.0.1:9000"
    with pytest.raises(ValueError):
        Config(web_port=True)


def test_custom_client_path_persists_and_is_used_by_status(monkeypatch, tmp_path, capsys):
    from drivingbench.gateway import cli

    installed = []
    inspected = []
    runtime = []
    monkeypatch.setattr(
        cli, "install_runtime", lambda repo: runtime.append(repo) or tmp_path / "bin"
    )
    monkeypatch.setattr(
        cli, "install_clients", lambda *args, **kw: installed.append((args[1], kw["paths"])) or []
    )
    monkeypatch.setattr(
        cli, "installation_status", lambda **kw: inspected.append(kw["paths"]) or {}
    )
    config = tmp_path / "config.json"
    custom = tmp_path / "my-codex/config.toml"
    cli.main(
        [
            "install",
            "--client",
            "codex",
            "--config",
            str(config),
            "--codex-config",
            str(custom),
            "--no-service",
        ]
    )
    assert Config.load(config).client_configs == {"codex": str(custom)}
    assert (
        runtime[0].name == "drivingbench_harness_v0.1" or (runtime[0] / "pyproject.toml").is_file()
    )
    assert (
        installed[0][0] == tmp_path / "bin/drivingbench-sandbox"
    )  # runtime venv, not the checkout
    cli.main(["status", "--config", str(config)])
    assert installed[0][1]["codex"] == inspected[0]["codex"] == custom
    assert {"claude", "cursor"} <= inspected[0].keys()
    capsys.readouterr()


def test_bundle_ships_the_native_files_with_upstream_and_installed_digests(tmp_path, monkeypatch):
    """The manifest's digests come from native/upstream and native/drivingbench of this checkout."""
    import tarfile

    from drivingbench.device import deploy

    repo = Path(__file__).resolve().parents[1]
    upstream = json.loads((repo / "native/upstream.json").read_text())
    assert all(
        (repo / "native/upstream" / f).is_file() and (repo / "native/drivingbench" / f).is_file()
        for f in upstream["files"]
    )

    def run(command, **kw):
        if command[:2] == ["uv", "build"]:
            Path(command[command.index("--out-dir") + 1]).mkdir(parents=True, exist_ok=True)
            (Path(command[command.index("--out-dir") + 1]) / "x.whl").write_bytes(b"")
        if command[:3] == ["uv", "pip", "install"]:
            Path(command[command.index("--target") + 1]).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    monkeypatch.setattr(
        deploy.subprocess,
        "check_output",
        lambda command, **kw: "" if "status" in command else "abc123\n",
    )
    output = deploy.build_bundle(
        repo, tmp_path / "release.tgz", repo / "device/initial-settings.json"
    )
    with tarfile.open(output) as archive:
        names = archive.getnames()
        manifest = json.loads(archive.extractfile("manifest.json").read())
    assert manifest["release"] == "abc123" and manifest["upstream"] == upstream["commit"]
    for name in upstream["files"]:
        assert f"native/{name}" in names
        entry = manifest["native"][name]
        assert entry["baseline"] == deploy.digest((repo / "native/upstream" / name).read_bytes())
        assert entry["installed"] == deploy.digest(
            (repo / "native/drivingbench" / name).read_bytes()
        )
        assert entry["baseline"] != entry["installed"]
    assert {"activate.py", "initial-settings.json", "requirements.txt"} <= set(names)


def test_native_deploy_refuses_unknown_files_before_changes(tmp_path):
    from drivingbench.device.deploy import digest, verify_native

    bundle, native = tmp_path / "bundle", tmp_path / "openpilot"
    (bundle / "native").mkdir(parents=True)
    native.mkdir()
    (bundle / "native/controlsd.py").write_bytes(b"new")
    (native / "controlsd.py").write_bytes(b"unknown local customization")
    manifest = {
        "native": {"controlsd.py": {"baseline": digest(b"old"), "installed": digest(b"new")}}
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="Unknown native edits"):
        verify_native(bundle, native, None)
    assert (native / "controlsd.py").read_bytes() == b"unknown local customization"
    (native / "controlsd.py").write_bytes(b"old")
    assert verify_native(bundle, native, None) == manifest
    (bundle / "native/controlsd.py").write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="Bundle content mismatch"):
        verify_native(bundle, native, None)


def test_native_deploy_accepts_only_recorded_previous_release(tmp_path):
    from drivingbench.device.deploy import digest, verify_native

    bundle, native, previous = (tmp_path / name for name in ("bundle", "native", "previous"))
    for path in (bundle / "native", native, previous):
        path.mkdir(parents=True)
    (bundle / "native/controlsd.py").write_bytes(b"new")
    (native / "controlsd.py").write_bytes(b"previous release")
    manifest = {
        "native": {"controlsd.py": {"baseline": digest(b"baseline"), "installed": digest(b"new")}}
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    (previous / "manifest.json").write_text(
        json.dumps({"native": {"controlsd.py": {"installed": digest(b"previous release")}}})
    )
    assert verify_native(bundle, native, previous) == manifest


def test_bundle_requires_explicit_longitudinal_settings(tmp_path):
    from drivingbench.device.deploy import build_bundle

    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    with pytest.raises(ValueError, match="all deployed speed_control"):
        build_bundle(tmp_path, tmp_path / "bundle.tgz", settings)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    from drivingbench.device import deploy

    bundle, native, root = (tmp_path / name for name in ("bundle", "native", "release-root"))
    (bundle / "native").mkdir(parents=True)
    native.mkdir()
    changes = {}
    for name in ("controlsd.py", "drive_helpers.py"):
        (bundle / "native" / name).write_bytes(b"new " + name.encode())
        (native / name).write_bytes(b"old " + name.encode())
        changes[name] = {
            "baseline": deploy.digest((native / name).read_bytes()),
            "installed": deploy.digest((bundle / "native" / name).read_bytes()),
        }
    (bundle / "manifest.json").write_text(
        json.dumps({"release": "abc123", "protocol": 2, "native": changes})
    )
    (bundle / "initial-settings.json").write_text("{}")
    (bundle / "requirements.txt").write_text("")
    staged = bundle / "python/drivingbench/device"
    staged.mkdir(parents=True)
    (staged / "service.py").write_text("# staged on the laptop\n")
    (native / "launch_env.sh").write_bytes(b"export UNRELATED=keep\n")
    checks, smoked = [], []
    monkeypatch.setattr(deploy, "require_park", lambda path: checks.append(path))
    monkeypatch.setattr(deploy, "smoke_imports", lambda *args: smoked.append(args))
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **kw: pytest.fail("no subprocess"))
    return deploy, bundle, native, root, checks, smoked


def test_activation_preserves_native_backup_and_needs_no_network(deployment):
    deploy, bundle, native, root, checks, smoked = deployment
    result = deploy.activate(bundle, root, native, Path("/usr/bin/python3"))
    assert result["restart_required"]
    assert len(checks) == 2  # before staging and again right before the rename
    release = (root / "current").resolve()
    assert smoked == [(Path("/usr/bin/python3"), release, native)]
    assert (release / "python/drivingbench/device/service.py").is_file()
    assert not list((root / "releases").glob("*.partial"))
    assert (native / "controlsd.py").read_bytes() == b"new controlsd.py"
    assert (release / "native-backup/controlsd.py").read_bytes() == b"old controlsd.py"
    assert (release / "native-backup/launch_env.sh").read_bytes() == b"export UNRELATED=keep\n"
    assert b"export UNRELATED=keep" in (native / "launch_env.sh").read_bytes()
    assert b"DRIVINGBENCH_MODE=experimental" in (native / "launch_env.sh").read_bytes()
    launcher = (release / "start-producer").read_text()
    assert "drivingbench.device.service" in launcher
    assert '"$attempt" -lt 5' in launcher and "gave up after 5 starts" in launcher
    assert 'if [ "$status" = 143 ]; then exit 0; fi' in launcher  # SIGTERM is deliberate
    assert subprocess.check_call(["sh", "-n", str(release / "start-producer")]) == 0


def test_activation_replaces_the_previous_release_exports(deployment):
    deploy, bundle, native, root, _, _ = deployment
    (native / "launch_env.sh").write_bytes(
        b"#!/usr/bin/bash\nexport UNRELATED=keep\n"
        b"# DrivingBench v0.1 release old\n"
        b"export DRIVINGBENCH_MODE=experimental\n"
        b"export DRIVINGBENCH_PYTHON_ROOT=/data/drivingbench-v01/releases/old/python\n"
        b"export DRIVINGBENCH_NATIVE_SOCKET=/data/drivingbench-v01/native.sock\n"
    )
    deploy.activate(bundle, root, native, Path("/usr/bin/python3"))
    lines = (native / "launch_env.sh").read_text().splitlines()
    assert lines[:2] == ["#!/usr/bin/bash", "export UNRELATED=keep"]
    exports = [line for line in lines if line.startswith("export DRIVINGBENCH_")]
    assert exports.count("export DRIVINGBENCH_MODE=experimental") == 1
    assert not any("releases/old" in line for line in lines)
    python_root = (root / "current").resolve() / "python"
    assert f"export DRIVINGBENCH_PYTHON_ROOT={python_root}" in exports


def test_activation_refuses_busy_producer_port_before_native_changes(deployment, monkeypatch):
    import socket

    deploy, bundle, native, root, _, _ = deployment
    monkeypatch.setattr(deploy, "require_free_port", partial(deploy.require_free_port, timeout=0.2))
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        with pytest.raises(RuntimeError, match="Port .* in use"):
            deploy.activate(bundle, root, native, Path("/usr/bin/python3"), port)
    assert (native / "controlsd.py").read_bytes() == b"old controlsd.py"
    assert (native / "launch_env.sh").read_bytes() == b"export UNRELATED=keep\n"
    assert not (root / "current").exists() and not (root / "releases").exists()
    # The port is free now: the same command simply succeeds.
    assert deploy.activate(bundle, root, native, Path("/usr/bin/python3"), port)["release"]
    assert (native / "controlsd.py").read_bytes() == b"new controlsd.py"


def test_activation_is_idempotent_for_an_identical_release_and_refuses_a_different_one(deployment):
    deploy, bundle, native, root, checks, _ = deployment
    first = deploy.activate(bundle, root, native, Path("/usr/bin/python3"))
    release = (root / "current").resolve()
    again = deploy.activate(bundle, root, native, Path("/usr/bin/python3"))
    assert again == first and (root / "current").resolve() == release
    assert (release / "native-backup/controlsd.py").read_bytes() == b"old controlsd.py"
    (bundle / "initial-settings.json").write_text('{"changed": true}')
    with pytest.raises(RuntimeError, match="different build"):
        deploy.activate(bundle, root, native, Path("/usr/bin/python3"))


def test_import_smoke_failure_leaves_native_files_untouched(deployment, monkeypatch):
    deploy, bundle, native, root, _, _ = deployment
    monkeypatch.setattr(
        deploy,
        "smoke_imports",
        lambda *a: (_ for _ in ()).throw(RuntimeError("Staged runtime failed to import: numpy")),
    )
    with pytest.raises(RuntimeError, match="failed to import: numpy"):
        deploy.activate(bundle, root, native, Path("/usr/bin/python3"))
    assert (native / "controlsd.py").read_bytes() == b"old controlsd.py"
    assert (native / "launch_env.sh").read_bytes() == b"export UNRELATED=keep\n"
    assert not (root / "current").exists()


def test_import_smoke_runs_the_native_interpreter_with_the_staged_path(tmp_path):
    import sys

    from drivingbench.device.deploy import smoke_imports

    staged, native = tmp_path / "release", tmp_path / "openpilot"
    (staged / "python").mkdir(parents=True)
    native.mkdir()
    with pytest.raises(RuntimeError, match="failed to import: ModuleNotFoundError.*openpilot"):
        smoke_imports(Path(sys.executable), staged, native)


def test_activation_restores_native_files_on_partial_write_failure(deployment, monkeypatch):
    deploy, bundle, native, root, _, _ = deployment
    original_writer = deploy.atomic_bytes
    failed = False

    def fail_once(path, content):
        nonlocal failed
        if path.name == "drive_helpers.py" and not failed:
            failed = True
            raise OSError("write failed")
        original_writer(path, content)

    monkeypatch.setattr(deploy, "atomic_bytes", fail_once)
    with pytest.raises(OSError, match="write failed"):
        deploy.activate(bundle, root, native, Path("/usr/bin/python3"))
    assert (native / "controlsd.py").read_bytes() == b"old controlsd.py"
    assert (native / "drive_helpers.py").read_bytes() == b"old drive_helpers.py"
    assert (native / "launch_env.sh").read_bytes() == b"export UNRELATED=keep\n"
    assert not (root / "current").exists()


def test_install_runtime_builds_a_venv_outside_the_checkout(tmp_path, monkeypatch):
    from drivingbench.gateway.install import install_runtime

    calls = []
    monkeypatch.setattr(
        "drivingbench.gateway.install.subprocess.run", lambda args, **kw: calls.append(args)
    )
    executables = install_runtime(Path("/repo/checkout"), tmp_path / "runtime")
    assert executables == tmp_path / "runtime/venv/bin"
    assert calls[0][:2] == ["uv", "venv"] and calls[0][-1] == str(tmp_path / "runtime/venv")
    assert calls[1][:3] == ["uv", "pip", "install"] and calls[1][-1] == "/repo/checkout"
    assert "--python" in calls[1] and str(tmp_path / "runtime/venv/bin/python") in calls[1]


def test_install_gateway_retries_bootstrap_while_launchd_tears_down_the_old_job(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from drivingbench.gateway.install import install_gateway

    monkeypatch.setattr("drivingbench.gateway.install.time.sleep", lambda s: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    calls = []

    def run(args, **kw):
        calls.append(args)
        busy = args[1] == "bootstrap" and sum(c[1] == "bootstrap" for c in calls) < 3
        return SimpleNamespace(returncode=5 if busy else 0, stderr=b"Input/output error")

    monkeypatch.setattr("drivingbench.gateway.install.subprocess.run", run)
    result = install_gateway(tmp_path / "bin/drivingbench", tmp_path / "config.json")
    assert result["status"] == "installed"
    assert [c[1] for c in calls] == ["bootout", "bootstrap", "bootstrap", "bootstrap"]

    calls.clear()
    monkeypatch.setattr(
        "drivingbench.gateway.install.subprocess.run",
        lambda args, **kw: SimpleNamespace(returncode=5, stderr=b"Input/output error"),
    )
    with pytest.raises(RuntimeError, match="Input/output error"):
        install_gateway(tmp_path / "bin/drivingbench", tmp_path / "config.json")


def test_require_free_port_waits_for_a_stopping_producer_to_release_it():
    import socket
    import threading

    from drivingbench.device import deploy

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    with pytest.raises(RuntimeError, match="in use"):
        deploy.require_free_port(port, timeout=0.3)
    threading.Timer(0.4, holder.close).start()
    deploy.require_free_port(port, timeout=3)  # released while waiting

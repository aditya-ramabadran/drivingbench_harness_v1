"""Traces: publishing segments and labeled sessions, transcripts, bulk artifacts, sync."""

import base64
import hashlib
import io
import json
from pathlib import Path

import pytest
from drivingbench.gateway import artifacts, cli, traces
from drivingbench.gateway.setup import Config, mirror_recordings
from PIL import Image


def event(kind, **data):
    return json.dumps(
        {"kind": kind, "timestamp": data.pop("timestamp", "2026-09-16T10:00:00+00:00"), **data}
    )


def write_segment(root, name, kinds, reason="Cone gap opens to the left; ease in.", image=None):
    folder = root / "segments" / name
    (folder / "thumbs").mkdir(parents=True)
    lines = []
    for i, kind in enumerate(kinds):
        extra = {"timestamp": f"2026-09-16T10:0{i}:00+00:00", "segment": name}
        if kind == "tool":
            extra.update(tool="set_motion", arguments={"reason": reason})
            if image:
                extra["images"] = [{"url": f"/api/recordings/{image}", "camera": "narrow"}]
        lines.append(event(kind, **extra))
    (folder / "events.jsonl").write_text("\n".join(lines) + "\n")
    (folder / "thumbs/aa.jpg").write_bytes(b"small")
    return folder


def test_mirror_uses_configured_ssh_and_never_deletes(monkeypatch, tmp_path):
    import shlex

    calls = []
    monkeypatch.setattr(
        "drivingbench.gateway.setup.subprocess.run", lambda args, **kw: calls.append(args)
    )
    config = Config(ssh_host="comma-alias", ssh_key="/tmp/key with spaces")
    mirror = mirror_recordings(config, tmp_path / "runs/recordings")
    args = calls[0]
    assert args[:3] == ["rsync", "-a", "--partial"] and "--delete" not in args
    assert args[-2] == "comma-alias:/data/drivingbench-v01/state/recordings/"
    assert args[-1] == str(mirror) + "/"
    ssh = shlex.split(args[args.index("-e") + 1])
    assert "StrictHostKeyChecking=yes" in ssh and ssh[ssh.index("-i") + 1] == "/tmp/key with spaces"


def test_publish_segments_copies_only_finished_ones_and_never_rewrites(tmp_path):
    mirror, root = tmp_path / "runs/recordings", tmp_path / "traces"
    done = write_segment(
        mirror, "2026-09-16-abc123", ["segment_start", "tool", "telemetry", "segment_end"]
    )
    open_ = write_segment(mirror, "2026-09-16-def456", ["segment_start", "tool"])
    (open_ / "events.jsonl").open("a").write('{"kind": "tel')  # still being written
    (mirror / "idle").mkdir()
    (mirror / "idle/events.jsonl").write_text('{"kind": "tool"}\n')
    result = traces.publish_segments(mirror, root)
    assert result == {"published": [done.name], "running": [open_.name], "conflicts": []}
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) == [
        f"segments/{done.name}/events.jsonl",
        f"segments/{done.name}/thumbs/aa.jpg",
    ]
    (root / "segments" / done.name / "thumbs/aa.jpg").write_bytes(b"locally annotated")
    assert traces.publish_segments(mirror, root)["published"] == [done.name]
    assert (root / "segments" / done.name / "thumbs/aa.jpg").read_bytes() == b"locally annotated"
    committed = (root / "segments" / done.name / "events.jsonl").read_bytes()
    (done / "events.jsonl").open("a").write('{"kind": "telemetry", "late": true}\n')
    result = traces.publish_segments(mirror, root)
    assert result["conflicts"] == [done.name] and result["published"] == []
    assert (root / "segments" / done.name / "events.jsonl").read_bytes() == committed


def session_json(mirror, **fields):
    session = {
        "id": "2026-09-16-gpt-5-codex-1a2b3c",
        "model": "gpt-5",
        "harness": "codex",
        "notes": "",
        "started_at": "2026-09-16T10:00:00+00:00",
        "ended_at": None,
        "outcome": None,
        "note": "",
        "segments": ["2026-09-16-abc123"],
        **fields,
    }
    (mirror / "sessions").mkdir(exist_ok=True, parents=True)
    (mirror / "sessions" / f"{session['id']}.json").write_text(json.dumps(session))
    return session


def test_publish_sessions_waits_for_end_and_keeps_artifacts(tmp_path):
    mirror, root = tmp_path / "runs/recordings", tmp_path / "traces"
    session = session_json(mirror)
    assert traces.publish_sessions(mirror, root) == {"published": [], "running": [session["id"]]}
    session = session_json(mirror, ended_at="2026-09-16T10:05:00+00:00", outcome="completed")
    assert traces.publish_sessions(mirror, root)["published"] == [session["id"]]
    manifest = root / "sessions" / f"{session['id']}.json"
    published = json.loads(manifest.read_text())
    assert published["outcome"] == "completed" and published["artifacts"] is None
    published["artifacts"] = {"repo": "x/y", "files": []}
    manifest.write_text(json.dumps(published))
    traces.publish_sessions(mirror, root)
    assert json.loads(manifest.read_text())["artifacts"] == {"repo": "x/y", "files": []}


def test_label_after_the_fact_from_published_segments(tmp_path):
    root = tmp_path / "traces"
    write_segment(root, "2026-09-16-abc123", ["segment_start", "tool", "segment_end"])
    write_segment(root, "2026-09-16-def456", ["segment_start", "segment_end"])
    with pytest.raises(ValueError, match="not a published segment"):
        traces.label_session(root, ["2026-09-16-zzz999"], "m", "codex", "", "completed")
    session = traces.label_session(
        root,
        ["2026-09-16-abc123", "2026-09-16-def456"],
        "Claude Opus",
        "claude",
        "lot A",
        "collision",
        "cone",
    )
    assert session["id"].startswith("2026-09-16-claude-opus-claude-")
    assert (
        session["labeled_after"] and session["outcome"] == "collision" and session["note"] == "cone"
    )
    assert session["started_at"] == "2026-09-16T10:00:00+00:00"
    assert (root / "sessions" / f"{session['id']}.json").is_file()
    with pytest.raises(ValueError, match="already exists"):
        traces.label_session(
            root,
            ["2026-09-16-abc123", "2026-09-16-def456"],
            "Claude Opus",
            "claude",
            "",
            "completed",
        )


def jpeg(color):
    data = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(data, "JPEG")
    return data.getvalue()


def test_transcripts_are_found_by_reason_and_filed_with_images_stripped(
    tmp_path, monkeypatch, capsys
):
    root, mirror = tmp_path / "traces", tmp_path / "runs/recordings"
    recorded = jpeg((200, 30, 30))
    (mirror / "images").mkdir(parents=True)
    (mirror / "images/f00d.jpg").write_bytes(recorded)
    write_segment(
        root, "2026-09-16-abc123", ["segment_start", "tool", "segment_end"], image="f00d.jpg"
    )
    codex, claude = tmp_path / "codex/2026/09/16", tmp_path / "claude/proj"
    codex.mkdir(parents=True)
    claude.mkdir(parents=True)
    observed = "2026-09-16T10:00:03.123456+00:00"
    (root / "segments/2026-09-16-abc123/events.jsonl").open("a").write(
        event("tool", tool="observe", segment="2026-09-16-abc123", outcome={"timestamp": observed})
        + "\n"
    )
    driver = codex / "rollout-driver.jsonl"
    driver.write_text(
        json.dumps({"result": {"text": json.dumps({"timestamp": observed, "state": "held"})}})
        + "\n"
        + json.dumps(
            {"call": "set_motion", "arguments": {"reason": "Cone gap opens to the left; ease in."}}
        )
        + "\n"
        + json.dumps({"image": base64.b64encode(recorded).decode()})
        + "\n"
        + json.dumps({"image": base64.b64encode(jpeg((0, 0, 200))).decode()})
        + "\n"
    )
    (codex / "rollout-coordinator.jsonl").write_text(
        '{"text": "let us discuss set_motion limits"}\n'
    )
    (claude / "s1.jsonl").write_text('{"text": "Done."}\n')
    roots = {
        "codex": str(tmp_path / "codex/*/*/*/rollout-*.jsonl"),
        "claude": str(tmp_path / "claude/*/*.jsonl"),
    }
    assert traces.find_chats(root / "segments/2026-09-16-abc123", roots) == [
        ("codex", driver, {"timestamps": 1, "reasons": 1})
    ]
    monkeypatch.setattr(traces, "TRANSCRIPT_ROOTS", roots)
    cli.main(
        [
            "attach-chat",
            "2026-09-16-abc123",
            "--find",
            "--traces",
            str(root),
            "--mirror",
            str(mirror),
        ]
    )
    filed = root / "segments/2026-09-16-abc123/chat" / f"codex-{driver.name}"
    attached = json.loads(capsys.readouterr().out)[0]
    assert attached["status"] == "attached" and attached["matched"] == {
        "timestamps": 1,
        "reasons": 1,
    }
    text = filed.read_text()
    assert "[image f00d.jpg]" in text  # matched a recorded frame by content hash
    assert "[image unrecorded-" in text  # unknown image still stripped
    assert "base64" not in text and len(text) < 400
    assert traces.segments_without_chat(root) == []


def test_route_selection_by_overlap_and_upload_records_hashes(tmp_path, monkeypatch):
    listing = "1000.0 route--0\n1060.0 route--1\n1120.0 route--2\n1500.0 route--7\nbad line\n"
    assert artifacts.overlapping_routes(listing, started=1050, ended=1070) == [
        "route--0",
        "route--1",
        "route--2",
    ]
    assert artifacts.overlapping_routes(listing, started=1300, ended=1310) == []

    root, mirror = tmp_path / "traces", tmp_path / "runs/recordings"
    (mirror / "images").mkdir(parents=True)
    (mirror / "images/f00d.jpg").write_bytes(b"frame")
    write_segment(
        root, "2026-09-16-abc123", ["segment_start", "tool", "segment_end"], image="f00d.jpg"
    )
    write_segment(
        root, "2026-09-16-def456", ["segment_start", "tool", "segment_end"], image="f00d.jpg"
    )
    session = {
        "id": "s1",
        "model": "GPT-6",
        "harness": "codex",
        "notes": "trial",
        "outcome": "completed",
        "started_at": "2026-09-16T10:00:00+00:00",
        "ended_at": "2026-09-16T10:05:00+00:00",
        "segments": ["2026-09-16-abc123", "2026-09-16-def456"],
        "artifacts": None,
    }
    (root / "sessions").mkdir()
    (root / "sessions/s1.json").write_text(json.dumps(session))
    twin = {**session, "id": "s2", "segments": []}
    (root / "sessions/s2.json").write_text(json.dumps(twin))
    monkeypatch.setattr(
        artifacts,
        "list_routes",
        lambda config: f"{artifacts.epoch(session['ended_at'])} 2026-09-16--10-02-00--3\n",
    )
    pulled = []

    def fake_rsync(config, remote, local):
        pulled.append(remote)
        local.mkdir(parents=True, exist_ok=True)
        (local / "fcamera.hevc").write_bytes(b"video")

    monkeypatch.setattr(artifacts, "rsync", fake_rsync)
    config = Config(dataset="example/drivingbench-traces")
    staging = tmp_path / "staging"
    artifacts.stage(config, root, mirror, session, staging)
    artifacts.stage(config, root, mirror, twin, staging)
    assert (
        pulled == ["/data/media/0/realdata/2026-09-16--10-02-00--3/"] * 2
    )  # rsync re-run (idempotent) ...
    assert (
        staging / "s2/routes/2026-09-16--10-02-00--3/fcamera.hevc"
    ).stat().st_nlink >= 3  # ... bytes stored once
    assert artifacts.pending(root, ["s1", "s2"]) == [session, twin]

    uploads = []

    class FakeApi:
        def upload_folder(self, **kw):
            uploads.append(
                (
                    kw["path_in_repo"],
                    sorted(
                        p.relative_to(kw["folder_path"]).as_posix()
                        for p in Path(kw["folder_path"]).rglob("*")
                        if p.is_file()
                    ),
                )
            )

    monkeypatch.setattr("huggingface_hub.HfApi", FakeApi)
    assert artifacts.upload(Config(dataset=None), root, staging) == [
        {"status": "no_dataset_configured"}
    ]
    report = artifacts.upload(config, root, staging)
    assert [(r["status"], r["path"]) for r in report] == [
        ("uploaded", "sessions/s1"),
        ("uploaded", "sessions/s2"),
    ]
    assert uploads[0] == (
        "sessions/s1",
        ["frames/f00d.jpg", "routes/2026-09-16--10-02-00--3/fcamera.hevc"],
    )
    manifest = json.loads((root / "sessions/s1.json").read_text())
    assert manifest["artifacts"]["path"] == "sessions/s1"
    assert (
        next(f for f in manifest["artifacts"]["files"] if f["path"].endswith("f00d.jpg"))["sha256"]
        == hashlib.sha256(b"frame").hexdigest()
    )
    assert artifacts.upload(config, root, staging) == []  # nothing pending twice
    # fetch follows the manifest's path.
    fetched = {}
    monkeypatch.setattr("huggingface_hub.snapshot_download", lambda **kw: fetched.update(kw))
    result = artifacts.fetch(config, "s1", root, tmp_path / "fetched")
    assert fetched["allow_patterns"] == ["sessions/s1/*"] and result["path"].endswith("sessions/s1")


def test_sync_composes_every_step(tmp_path, monkeypatch):
    root, mirror = tmp_path / "traces", tmp_path / "runs/recordings"
    write_segment(mirror, "2026-09-16-abc123", ["segment_start", "tool", "segment_end"])
    session_json(mirror, ended_at="2026-09-16T10:05:00+00:00", outcome="completed")
    monkeypatch.setattr(cli, "mirror_recordings", lambda config, path: mirror)
    monkeypatch.setattr(traces, "TRANSCRIPT_ROOTS", {"codex": str(tmp_path / "nowhere/*.jsonl")})
    report = cli.sync(Config(dataset=None), root, mirror)
    assert report["segments"]["published"] == ["2026-09-16-abc123"]
    assert report["sessions"]["published"] == ["2026-09-16-gpt-5-codex-1a2b3c"]
    assert report["chats"] == {"2026-09-16-abc123": []}
    assert report["staged"] == []  # no dataset configured: nothing to stage
    assert (root / "sessions/2026-09-16-gpt-5-codex-1a2b3c.json").is_file()


def test_find_chats_is_adversarial_about_escaping_boilerplate_and_stale_files(tmp_path):
    root = tmp_path / "traces"
    folder = write_segment(root, "2026-09-16-abc123", ["segment_start", "segment_end"])
    curly = "The aisle\u2019s clear \u2014 \u201cgo\u201d toward the trailer on the right."
    boilerplate = "Continue straight toward the gap."
    observed = "2026-09-16T10:00:03.123456+00:00"
    with (folder / "events.jsonl").open("a") as stream:
        for reason in (curly, boilerplate):
            stream.write(event("tool", tool="set_motion", arguments={"reason": reason}) + "\n")
        stream.write(event("tool", tool="observe", outcome={"timestamp": observed}) + "\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    # 1. The driving chat stores arguments JSON-escaped (\u2019, \u201c ...): still matched.
    escaped = logs / "escaped.jsonl"
    escaped.write_text(
        json.dumps({"arguments": {"reason": curly}}) + "\n" + json.dumps({"r": boilerplate}) + "\n"
    )
    assert "\\u2019" in escaped.read_text()
    # 2. A different chat that only used the same boilerplate reason: one hit is not enough.
    other = logs / "other.jsonl"
    other.write_text(json.dumps({"arguments": {"reason": boilerplate}}) + "\n")
    # 3. A chat that observed once, with the exact producer timestamp: one timestamp suffices.
    observer = logs / "observer.jsonl"
    observer.write_text(json.dumps({"text": json.dumps({"timestamp": observed})}) + "\n")
    # 4. A transcript last modified before the segment began is never read.
    stale = logs / "stale.jsonl"
    stale.write_text(json.dumps({"arguments": {"reason": curly}}) + json.dumps({"r": boilerplate}))
    import os

    os.utime(stale, (0, 0))
    roots = {"codex": str(logs / "*.jsonl")}
    found = traces.find_chats(folder, roots)
    assert [(p.name, m) for _, p, m in found] == [
        ("escaped.jsonl", {"timestamps": 0, "reasons": 2}),
        ("observer.jsonl", {"timestamps": 1, "reasons": 0}),
    ]


def test_transcript_roots_include_codex_home_and_configured_extras(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert set(traces.transcript_roots()) == {"codex", "claude", "cursor"}
    monkeypatch.setenv("CODEX_HOME", "/tmp/second")
    roots = traces.transcript_roots(Config(extra_transcripts={"codex-laptop2": "/x/*.jsonl"}))
    assert roots["codex-home"] == "/tmp/second/sessions/*/*/*/rollout-*.jsonl"
    assert (
        roots["codex-laptop2"] == "/x/*.jsonl"
        and roots["codex"] == traces.TRANSCRIPT_ROOTS["codex"]
    )


def test_default_roots_match_each_clients_real_layout(tmp_path, monkeypatch):
    """Cursor nests one directory per chat; the glob must reach the file, or chats are never found."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    layouts = {
        "codex": ".codex/sessions/2026/09/16/rollout-2026-09-16T18-25-00-abc.jsonl",
        "claude": ".claude/projects/-Users-me-repo/0f3e.jsonl",
        "cursor": ".cursor/projects/empty-window/agent-transcripts/d0a9/d0a9.jsonl",
    }
    for relative in layouts.values():
        (tmp_path / relative).parent.mkdir(parents=True)
        (tmp_path / relative).write_text("{}\n")
    import glob
    import os

    found = {
        client: glob.glob(os.path.expanduser(pattern))
        for client, pattern in traces.transcript_roots().items()
    }
    assert {client: len(paths) for client, paths in found.items()} == {
        "codex": 1,
        "claude": 1,
        "cursor": 1,
    }

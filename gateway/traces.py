"""Publish the comma's recordings into the repository's traces/ folder.

Mirror layout (device recordings, rsynced into the ignored runs/recordings):
    segments/<segment>/events.jsonl, thumbs/    sessions/<session>.json    images/<id>.jpg

Published layout (committed):
    traces/segments/<segment>/{events.jsonl, thumbs/, chat/}
    traces/sessions/<session>.json      fields, timing, outcome, segments, artifacts

Nothing here rewrites or deletes what was published before.
"""

import base64
import glob
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

SEGMENT_ID = re.compile(r"\d{4}-\d{2}-\d{2}-[0-9a-f]{6}")
TRANSCRIPT_ROOTS = {
    "codex": "~/.codex/sessions/*/*/*/rollout-*.jsonl",
    "claude": "~/.claude/projects/*/*.jsonl",
    "cursor": "~/.cursor/projects/*/agent-transcripts/*/*.jsonl",
}


def transcript_roots(config=None) -> dict[str, str]:
    """Default client locations, plus $CODEX_HOME and any extra roots from the laptop config."""
    roots = dict(TRANSCRIPT_ROOTS)
    if home := os.environ.get("CODEX_HOME"):
        roots["codex-home"] = f"{home}/sessions/*/*/*/rollout-*.jsonl"
    roots.update(getattr(config, "extra_transcripts", None) or {})
    return roots


def read_events(path: Path) -> list[dict]:
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass  # A partial final line means the device is still writing.
    return events


def publish_segments(mirror: Path, traces: Path) -> dict:
    """Copy each finished segment (has segment_end); report open ones and conflicts."""
    published, running, conflicts = [], [], []
    for folder in sorted((mirror / "segments").glob("*")) if (mirror / "segments").is_dir() else []:
        events = folder / "events.jsonl"
        if not SEGMENT_ID.fullmatch(folder.name) or not events.is_file():
            continue
        if not any(event.get("kind") == "segment_end" for event in read_events(events)):
            running.append(folder.name)
            continue
        destination = traces / "segments" / folder.name
        target = destination / "events.jsonl"
        if target.exists() and target.read_bytes() != events.read_bytes():
            conflicts.append(folder.name)
            continue
        destination.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(events.read_bytes())
        for thumb in sorted((folder / "thumbs").glob("*.jpg")):
            (destination / "thumbs").mkdir(exist_ok=True)
            if not (destination / "thumbs" / thumb.name).exists():
                (destination / "thumbs" / thumb.name).write_bytes(thumb.read_bytes())
        published.append(folder.name)
    return {"published": published, "running": running, "conflicts": conflicts}


def publish_sessions(mirror: Path, traces: Path) -> dict:
    """Copy ended labeled sessions; the comma's fields win, published artifacts are kept."""
    published, running = [], []
    for path in (
        sorted((mirror / "sessions").glob("*.json")) if (mirror / "sessions").is_dir() else []
    ):
        session = json.loads(path.read_text())
        if not session.get("ended_at"):
            running.append(session["id"])
            continue
        target = traces / "sessions" / f"{session['id']}.json"
        existing = json.loads(target.read_text()) if target.exists() else {}
        merged = {**session, "artifacts": existing.get("artifacts")}
        if existing != merged:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(merged, indent=2) + "\n")
        published.append(session["id"])
    return {"published": published, "running": running}


def label_session(traces: Path, segments: list[str], model, harness, notes, outcome, note=""):
    """Post-hoc labeling from already published segments, for the forgotten-to-start case."""
    from drivingbench.device.recording import slug
    from drivingbench.shared.contracts import SessionEnd, SessionLabel

    label, end = (
        SessionLabel(model=model, harness=harness, notes=notes),
        SessionEnd(outcome=outcome, note=note),
    )
    times = []
    for segment in segments:
        events = traces / "segments" / segment / "events.jsonl"
        if not events.is_file():
            raise ValueError(f"{segment} is not a published segment")
        times += [event["timestamp"] for event in read_events(events)]
    started, ended = min(times), max(times)
    session = {
        "id": f"{started[:10]}-{slug(label.model)}-{slug(label.harness)}-{hashlib.sha256(''.join(segments).encode()).hexdigest()[:6]}",
        **label.model_dump(),
        "started_at": started,
        "ended_at": ended,
        **end.model_dump(),
        "segments": list(segments),
        "labeled_after": True,
        "artifacts": None,
    }
    target = traces / "sessions" / f"{session['id']}.json"
    if target.exists():
        raise ValueError(f"{target} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(session, indent=2) + "\n")
    return session


def chat_keys(segment_folder: Path) -> dict[str, set[str]]:
    """Strings a driving transcript must quote: observe response timestamps (producer-made,
    microsecond, ASCII, unique) and the reasons the model sent (optional, may repeat)."""
    keys = {"timestamps": set(), "reasons": set()}
    for event in read_events(segment_folder / "events.jsonl"):
        if event.get("kind") != "tool":
            continue
        if event.get("tool") == "observe" and (event.get("outcome") or {}).get("timestamp"):
            keys["timestamps"].add(event["outcome"]["timestamp"])
        reason = (event.get("arguments") or {}).get("reason", "")
        if len(reason) >= 12:
            keys["reasons"].add(reason)
    return keys


def quoted(text: str, key: str) -> bool:
    """Transcripts store tool arguments as JSON strings, so match the escaped form too."""
    return key in text or json.dumps(key)[1:-1] in text


def find_chats(segment_folder: Path, roots: dict[str, str] | None = None) -> list:
    """Transcripts that drove this segment, with the evidence: (client, path, matches).

    A file counts with one observe-timestamp hit or two reason hits, so a reused
    boilerplate reason alone cannot attach the wrong chat. Only transcripts modified
    after the segment began are read.
    """
    keys = chat_keys(segment_folder)
    events = read_events(segment_folder / "events.jsonl")
    started = datetime.fromisoformat(events[0]["timestamp"]).timestamp() if events else 0
    found = []
    for client, pattern in (roots or transcript_roots()).items() if any(keys.values()) else []:
        for name in sorted(glob.glob(os.path.expanduser(pattern))):
            path = Path(name)
            try:
                if path.stat().st_mtime < started:
                    continue
                text = path.read_text(errors="replace")
            except OSError:
                continue
            matches = {
                kind: sum(quoted(text, key) for key in values) for kind, values in keys.items()
            }
            if matches["timestamps"] >= 1 or matches["reasons"] >= 2:
                found.append((client, path, matches))
    return found


BASE64_IMAGE = re.compile(r"(?:data:image/[a-z]+;base64,)?([A-Za-z0-9+/]{800,}={0,2})")


def strip_images(text: str, known: dict[str, str]) -> str:
    """Replace inline base64 images with references to recorded frames (by content hash)."""

    def replace(match):
        try:
            digest = hashlib.sha256(base64.b64decode(match.group(1), validate=True)).hexdigest()
        except ValueError:
            return match.group(0)
        return f"[image {known.get(digest, 'unrecorded-' + digest[:12])}]"

    return BASE64_IMAGE.sub(replace, text)


def recorded_image_hashes(mirror: Path) -> dict[str, str]:
    images = mirror / "images"
    return (
        {hashlib.sha256(p.read_bytes()).hexdigest(): p.name for p in images.glob("*.jpg")}
        if images.is_dir()
        else {}
    )


def attach_chat(traces: Path, segment: str, transcript: Path, client: str, known=None) -> dict:
    """File the driving chat's transcript, images stripped to references; never overwrite."""
    folder = traces / "segments" / segment
    if not (folder / "events.jsonl").is_file():
        raise ValueError(f"{segment} is not a published segment under {traces}")
    transcript = transcript.expanduser().resolve()
    content = strip_images(transcript.read_text(errors="replace"), known or {}).encode()
    destination = folder / "chat" / f"{client}-{transcript.name}"
    if destination.exists():
        if destination.read_bytes() == content:
            return {"status": "unchanged", "chat": str(destination)}
        raise ValueError(f"{destination} exists with different content; choose another name")
    destination.parent.mkdir(exist_ok=True)
    destination.write_bytes(content)
    return {"status": "attached", "chat": str(destination)}


def segments_without_chat(traces: Path) -> list[str]:
    root = traces / "segments"
    return [
        folder.name
        for folder in (sorted(root.iterdir()) if root.is_dir() else [])
        if (folder / "events.jsonl").is_file() and not any((folder / "chat").glob("*"))
    ]

"""Best-effort background evidence. Disk I/O never gates native motion.

Two groupings, both stamped on every event:

- segment: automatic, one per native engagement -> disengagement.
- session: optional, operator-labeled (model, harness, notes), spanning segments.

Layout under the recordings directory:

    segments/<segment>/events.jsonl   every event while that segment was open
    segments/<segment>/thumbs/<id>.jpg small copies of observed images (committable)
    sessions/<session>.json           labeled session: fields, timing, outcome, segments
    idle/...                          events and thumbs outside any segment
    images/<id>.jpg                   full-resolution observed images (device only)
    current-segment, current-session  markers while open
"""

import io
import json
import queue
import re
import secrets
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

SEGMENT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{6}$")
THUMB_WIDTH = 320


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def slug(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40] or "unnamed"


def thumbnail(jpeg):
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as image:
        image = image.convert("RGB")
        if image.width > THUMB_WIDTH:
            image = image.resize(
                (THUMB_WIDTH, max(1, round(image.height * THUMB_WIDTH / image.width)))
            )
        out = io.BytesIO()
        image.save(out, "JPEG", quality=60, optimize=True)
        return out.getvalue()


class Recorder:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.calls = deque(maxlen=200)
        self.lock = threading.Lock()
        self.queue = queue.Queue(maxsize=2048)
        self.error = None
        self.segment = None
        self.session = None  # dict while a labeled session is open
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        try:
            marker = self.directory / "current-segment"
            if marker.is_file():
                # The previous producer died with a segment open; close it explicitly.
                self.segment = marker.read_text().strip() or None
                self.end_segment("producer_restart")
            marker = self.directory / "current-session"
            if marker.is_file():  # Labeled sessions belong to the operator; keep it open.
                path = self.directory / "sessions" / f"{marker.read_text().strip()}.json"
                self.session = json.loads(path.read_text()) if path.is_file() else None
        except (OSError, ValueError) as error:
            self.error = str(error)
        self.thread = threading.Thread(target=self._run, name="evidence", daemon=True)
        self.thread.start()

    # Segments -------------------------------------------------------------------------

    def begin_segment(self, **data):
        if self.segment is not None:
            return self.segment
        self.segment = f"{datetime.now(timezone.utc):%Y-%m-%d}-{secrets.token_hex(3)}"
        self._enqueue(("marker", ("current-segment", self.segment)))
        if self.session is not None:
            self.session["segments"].append(self.segment)
            self._save_session()
        self.record("segment_start", **data)
        return self.segment

    def end_segment(self, reason, **data):
        if self.segment is None:
            return
        self.record("segment_end", reason=reason, **data)
        self.segment = None
        self._enqueue(("marker", ("current-segment", None)))

    # Labeled sessions -----------------------------------------------------------------

    def begin_session(self, model, harness, notes=""):
        if self.session is not None:
            self.end_session("superseded")
        started = datetime.now(timezone.utc)
        self.session = {
            "id": f"{started:%Y-%m-%d}-{slug(model)}-{slug(harness)}-{secrets.token_hex(3)}",
            "model": model,
            "harness": harness,
            "notes": notes,
            "started_at": started.isoformat(),
            "ended_at": None,
            "outcome": None,
            "note": "",
            "segments": [self.segment] if self.segment else [],
        }
        self._save_session()
        self._enqueue(("marker", ("current-session", self.session["id"])))
        self.record("session_start", **{k: v for k, v in self.session.items() if k != "segments"})
        return dict(self.session)

    def end_session(self, outcome, note=""):
        if self.session is None:
            return None
        self.session.update(ended_at=now_iso(), outcome=outcome, note=note)
        self.record("session_end", outcome=outcome, note=note)
        self._save_session()
        self._enqueue(("marker", ("current-session", None)))
        ended, self.session = self.session, None
        return ended

    def _save_session(self):
        self._enqueue(("session", dict(self.session)))

    # Events and images ----------------------------------------------------------------

    def record(self, kind, **data):
        event = {
            "id": uuid4().hex,
            "timestamp": now_iso(),
            "kind": kind,
            "segment": self.segment,
            "session": self.session["id"] if self.session else None,
            **data,
        }
        if kind == "tool":
            with self.lock:
                self.calls.append(event)
        self._enqueue(("event", event))
        return event

    def history(self):
        with self.lock:
            return list(self.calls)

    def image(self, jpeg):
        name = uuid4().hex + ".jpg"
        self._enqueue(("image", (self.segment, name, jpeg)))
        return name

    def _enqueue(self, item):
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self.error = "recording_backlog"

    def _folder(self, segment):
        return self.directory / ("segments/" + segment if segment else "idle")

    def _run(self):
        while not self.stop.is_set() or not self.queue.empty():
            try:
                kind, payload = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                if kind == "event":
                    folder = self._folder(payload["segment"])
                    folder.mkdir(parents=True, exist_ok=True)
                    with (folder / "events.jsonl").open("a") as stream:
                        stream.write(json.dumps(payload, allow_nan=False) + "\n")
                elif kind == "image":
                    segment, name, jpeg = payload
                    (self.directory / "images").mkdir(exist_ok=True)
                    (self.directory / "images" / name).write_bytes(jpeg)
                    thumbs = self._folder(segment) / "thumbs"
                    thumbs.mkdir(parents=True, exist_ok=True)
                    (thumbs / name).write_bytes(thumbnail(jpeg))
                elif kind == "session":
                    (self.directory / "sessions").mkdir(exist_ok=True)
                    path = self.directory / "sessions" / f"{payload['id']}.json"
                    path.write_text(json.dumps(payload, indent=2) + "\n")
                else:
                    name, value = payload
                    if value is None:
                        (self.directory / name).unlink(missing_ok=True)
                    else:
                        (self.directory / name).write_text(value)
            except (OSError, ValueError) as error:
                self.error = str(error)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)

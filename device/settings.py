"""One atomically saved device configuration, patched by field rather than whole forms."""

import json
import os
import tempfile
import threading
from pathlib import Path

from drivingbench.shared.contracts import Settings


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SettingsStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.value = Settings()
        self.revision = 0
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            self.value = Settings.model_validate(saved["settings"])
            self.revision = saved["revision"]

    def snapshot(self):
        with self.lock:
            return self.value.model_copy(deep=True), self.revision

    def patch(self, patch):
        with self.lock:
            value = self.value.model_dump()
            for key, item in patch.items():
                if key in {"image_adjustments", "speed_control"} and isinstance(item, dict):
                    value[key] = {**value[key], **item}
                else:
                    value[key] = item
            checked = Settings.model_validate(value)
            revision = self.revision + 1
            atomic_json(self.path, {"settings": checked.model_dump(), "revision": revision})
            self.value, self.revision = checked, revision
            return checked.model_copy(deep=True), revision

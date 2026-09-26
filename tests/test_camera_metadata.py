from collections import OrderedDict
from types import SimpleNamespace

import pytest
from drivingbench.device import cameras


def event(frame=7, *, valid=True, timestamps=(100, 110)):
    return SimpleNamespace(
        valid=valid,
        narrowRoadCameraState=SimpleNamespace(
            frameId=frame, timestampSof=timestamps[0], timestampEof=timestamps[1]
        ),
    )


def match(monkeypatch, events, cached=None, cancelled=False):
    now = [0.0]
    monkeypatch.setattr(cameras.time, "monotonic", lambda: now[0])

    class Stop:
        def is_set(self):
            return cancelled

        def wait(self, duration):
            now[0] += duration

    def receive():
        now[0] += 0.0001
        return events(now[0])

    valid = OrderedDict(cached or {})
    matched = cameras._match_metadata(
        receive, "narrowRoadCameraState", valid, 7, (100, 110), Stop()
    )
    return matched, now[0], valid


def test_pixels_before_metadata_wait_for_exact_frame(monkeypatch):
    matched, elapsed, _ = match(monkeypatch, lambda t: event() if t >= 0.003 else None)
    assert matched and 0.003 <= elapsed < 0.01


def test_metadata_before_pixels_uses_cached_match(monkeypatch):
    matched, elapsed, _ = match(monkeypatch, lambda t: None, {7: (100, 110)})
    assert matched and elapsed < 0.001


@pytest.mark.parametrize("packet", [event(valid=False), event(timestamps=(100, 111))])
def test_invalid_or_wrong_timestamps_reject(monkeypatch, packet):
    assert not match(monkeypatch, lambda t: packet)[0]


def test_missing_metadata_is_bounded_and_cache_is_bounded(monkeypatch):
    matched, elapsed, valid = match(monkeypatch, lambda t: event(frame=100 + int(t * 100000)))
    assert not matched and 0.05 <= elapsed < 0.051
    assert len(valid) == 64
    matched, elapsed, _ = match(monkeypatch, lambda t: None)
    assert not matched and 0.05 <= elapsed < 0.052


def test_shutdown_does_not_wait_for_metadata(monkeypatch):
    matched, elapsed, _ = match(monkeypatch, lambda t: None, cancelled=True)
    assert not matched and elapsed == 0

# Traces

Two groupings, both recorded on the comma and stamped on every event:

- **Segment**: automatic, one per openpilot engagement → disengagement. Every drive
  produces segments; nothing needs labeling.
- **Session**: optional, operator-labeled (model, harness, notes) and ended with an
  outcome. Groups the segments of one benchmark attempt. Started from Setup →
  "Labeled session" or `drivingbench session start`; ended there with
  `completed | collision | aborted`; starting another session ends the current one
  as `superseded`. Forgot to start one? `drivingbench session label <segment>… --model
  … --harness … --outcome …` labels published segments after the fact.

## Layout

```text
traces/segments/2026-09-16-3f9a1c/
  events.jsonl        segment_start, tool calls (with the model's reason), settings
                      revisions, ~10 Hz native telemetry while engaged, segment_end
  thumbs/<id>.jpg     small copies of the images observe returned
  chat/<client>-<f>   the chat transcript(s) that drove it, inline images replaced by
                      [image <id>.jpg] references to the recorded frames
traces/sessions/2026-09-16-gpt-5-codex-3f9a1c.json
  model, harness, notes, started_at, ended_at, outcome, note, segments[],
  artifacts: {repo, path, files[{path, bytes, sha256}]}   (after upload)
```

Bulk artifacts (full-resolution observed frames and openpilot's route minutes:
`fcamera.hevc` road video, `ecamera.hevc` wide video, `rlog.zst` full log, `qcamera.ts`
preview) are staged for labeled sessions under the ignored `runs/artifacts/<session>/`
and, if a Hugging Face dataset is configured (`drivingbench install --dataset
<user>/<repo>`), uploaded to `sessions/<session>/` there. Unlabeled segments keep
their full-size frames on the comma and in the ignored local mirror.

## After a drive, on the laptop that drove

```sh
uv run drivingbench sync        # comma needed: mirror → runs/recordings (ignored),
                                # publish finished segments and ended sessions here,
                                # attach the transcripts that drove them,
                                # stage labeled sessions' frames and route minutes
git add traces && git commit -m "Trace: <what was driven>"
uv run drivingbench upload      # comma not needed: upload staged sessions, record hashes
git add traces && git commit -m "Trace: artifact hashes"
uv run drivingbench fetch <session>   # elsewhere: pull a session's artifacts
```

The gateway's trace viewer (<http://127.0.0.1:8766/traces>) lists these segments and
sessions and scrubs through a segment's frames, tool calls, and telemetry like a video.
It serves the committed thumbnails, or a session's full-size frames once `fetch` has put
them under `runs/artifacts/`. It reads the checkout `install` was run from.

Transcripts are found by content: the microsecond `observe` timestamps the producer
returned (one hit suffices) and the `reason` strings the model sent (two hits), matched
in raw and JSON-escaped form among files modified after the segment began. Where each
client keeps them:

| Client | Transcript |
| --- | --- |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl` |
| Claude Code | `~/.claude/projects/<project-slug>/<session>.jsonl` |
| Cursor | `~/.cursor/projects/<project-slug>/agent-transcripts/<uuid>/<uuid>.jsonl` |

`$CODEX_HOME/sessions` is searched too, and any `extra_transcripts` globs in the
laptop config (`~/.config/drivingbench-v01/config.json`) for second homes.

Rules: never delete, rewrite, or hand-edit a published segment or session; never
commit a segment without `segment_end` or a session without `ended_at` (`sync`
skips them and reports `running`); a published `events.jsonl` that differs from the
comma's copy is reported under `conflicts`, never overwritten. No Git LFS, no hooks.

from __future__ import annotations

import json

from exhibition_archive import progress


def test_spirula_progress_parser_recognizes_real_extraction_and_mapping_lines():
    parser = progress.SpirulaProgressParser("sfm", {"target_images": 77})
    extraction = parser.parse("[extract] 4/77 IMG_1297.jpg Features: 8192")
    matching = parser.parse("[run] Matching: global pairs Pairs kept: 1588/2926 Inliers: 505847/747637")
    mapping = parser.parse("[map] Registered image 4, images in the model: 32")

    assert (extraction["current"], extraction["total"], extraction["image"]) == (4, 77, "IMG_1297.jpg")
    assert extraction["eta_seconds"] is None
    assert matching["pairs_kept"] == 1588
    assert matching["inliers"] == 505847
    assert (mapping["current"], mapping["total"]) == (32, 77)
    assert "ETA: calculating" in progress.format_event(extraction)


def test_training_eta_uses_observed_rate_and_unknown_eta_is_explicit(monkeypatch):
    ticks = iter([0.0, 10.0, 10.0, 12.0, 12.0])
    monkeypatch.setattr(progress.time, "monotonic", lambda: next(ticks))
    parser = progress.SpirulaProgressParser("training")

    first = parser.parse("iteration 100/1000 loss=0.025")
    second = parser.parse("iteration 200/1000 loss=0.020")

    assert first["eta_seconds"] is None
    assert second["iterations_per_second"] == 50.0
    assert second["eta_seconds"] == 16.0
    assert second["metric"] == {"name": "loss", "value": 0.02}
    assert "ETA 16s" in progress.format_event(second)


def test_non_finite_training_metric_is_ignored_and_malformed_lines_are_safe():
    parser = progress.SpirulaProgressParser("training", {"target_iterations": 100})
    event = parser.parse("iteration 4 loss=nan")

    assert event["current"] == 4
    assert "metric" not in event
    assert parser.parse("[extract] not-a-counter") is None
    assert parser.parse("garbled backend line \udcff") is None


def test_incremental_log_reader_handles_partial_lines_and_bounds_reads(tmp_path, monkeypatch):
    path = tmp_path / "backend.log"
    path.write_bytes(b"[extract] 1/2 first")
    reader = progress.IncrementalLogReader()
    assert reader.read(path) == []
    with path.open("ab") as stream:
        stream.write(b".jpg\nsecond\xff\n")

    lines = reader.read(path)

    assert lines == ["[extract] 1/2 first.jpg", "second\ufffd"]
    assert list(reader.recent_lines) == lines

    bounded_path = tmp_path / "bounded.log"
    bounded_path.write_bytes(b"a" * (progress.MAX_READ_BYTES_PER_POLL + 100))
    bounded = progress.IncrementalLogReader()
    first_chunk = bounded.read(bounded_path)
    assert first_chunk == []
    assert bounded.offset == progress.MAX_READ_BYTES_PER_POLL


def test_jsonl_progress_events_survive_resume_and_text_modes_are_concise(tmp_path):
    path = tmp_path / "progress.jsonl"
    event = progress.make_event("sfm", "pipeline_stage_cached", stage_number=6,
                                stage_total=11, status="SKIPPED_VALID_CACHE")
    progress.append_event(path, event)
    resumed = progress.make_event("training", "training_iteration", current=10, total=100,
                                  elapsed_seconds=1.0, iterations_per_second=None, eta_seconds=None)
    progress.append_event(path, resumed)

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert "reused validated cache" in progress.format_event(rows[0])
    assert "ETA: calculating" in progress.format_event(rows[1])
    assert "command" not in progress.format_event(progress.make_event(
        "sfm", "stage_process_started", command=["spirula", "sfm"], output_log="run.log"))


def test_heartbeat_and_checkpoint_formatting_reports_elapsed_without_fake_percentages():
    heartbeat = progress.make_event("training", "stage_process_heartbeat", elapsed_seconds=3670,
                                    progress=None, checkpoint_count=1)
    checkpoint = progress.make_event("training", "checkpoint_state", candidates=[{"path": "latest.pth"}])

    assert "elapsed 1:01:10" in progress.format_event(heartbeat)
    assert "backend progress: unavailable" in progress.format_event(heartbeat)
    assert "latest.pth" in progress.format_event(checkpoint)
    assert "%" not in progress.format_event(heartbeat)

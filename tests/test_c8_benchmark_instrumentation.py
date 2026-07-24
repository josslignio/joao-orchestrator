"""C8-B: Product Superiority & Efficiency instrumentation
(`JOAO_C8_GATES_SPEC.md` §24, `JOAO_C8_GATES_ROADMAP.md` LOT C8-B).
"""
from __future__ import annotations

import json

import pytest

from src.joao_orchestrator.bubble import benchmark_events as be

IDENTITY = dict(benchmark_id="b1", run_id="r1", mission_id="m1", workflow_mode="joao",
                base_sha="a" * 40, spec_sha="b" * 40, roadmap_sha="c" * 40)


def _log(path):
    return be.EventLog(path, **IDENTITY)


def test_manifest_written_with_required_identity_fields(tmp_path):
    manifest = be.write_run_manifest(tmp_path, risk_tier="normal", **IDENTITY)
    on_disk = json.loads((tmp_path / "benchmark-run-manifest.json").read_text())
    assert on_disk == manifest
    for field in ("benchmark_id", "run_id", "mission_id", "workflow_mode", "base_sha", "spec_sha", "roadmap_sha"):
        assert on_disk[field]
    assert on_disk["evidence_completeness"] == 100
    assert on_disk["secrets_logged"] == 0


def test_invalid_workflow_mode_rejected(tmp_path):
    with pytest.raises(be.BenchmarkInstrumentationError):
        be.write_run_manifest(tmp_path, risk_tier="normal", **{**IDENTITY, "workflow_mode": "bogus"})
    with pytest.raises(be.BenchmarkInstrumentationError):
        be.EventLog(tmp_path / "events.jsonl", **{**IDENTITY, "workflow_mode": "bogus"})


def test_candidate_tree_null_before_bound_mandatory_after(tmp_path):
    log = _log(tmp_path / "events.jsonl")
    log.append("GO", timestamp="t0")
    with pytest.raises(be.BenchmarkInstrumentationError):
        log.append("SNEAKY", candidate_tree="x" * 40, timestamp="t1")
    log.append("CANDIDATE_BOUND", candidate_tree="t" * 40, candidate_commit="c" * 40, timestamp="t2")
    with pytest.raises(be.BenchmarkInstrumentationError):
        log.append("REVIEW_DONE", timestamp="t3")  # missing candidate_tree post-bind
    event = log.append("REVIEW_DONE", candidate_tree="t" * 40, timestamp="t3")
    assert event["candidate_tree"] == "t" * 40


def test_candidate_bound_requires_real_tree_and_commit(tmp_path):
    log = _log(tmp_path / "events.jsonl")
    with pytest.raises(be.BenchmarkInstrumentationError):
        log.append("CANDIDATE_BOUND", timestamp="t0")


def test_sequence_numbers_strictly_increase_never_reused(tmp_path):
    log = _log(tmp_path / "events.jsonl")
    e1 = log.append("A", timestamp="t0")
    e2 = log.append("B", timestamp="t1")
    e3 = log.append("C", timestamp="t2")
    assert [e1["sequence_number"], e2["sequence_number"], e3["sequence_number"]] == [1, 2, 3]


def test_reopening_an_existing_log_resumes_sequence_and_chain(tmp_path):
    path = tmp_path / "events.jsonl"
    log1 = _log(path)
    log1.append("A", timestamp="t0")
    log1.append("B", timestamp="t1")
    log2 = _log(path)  # simulates a fresh process resuming the same run
    e3 = log2.append("C", timestamp="t2")
    assert e3["sequence_number"] == 3
    result = be.verify_chain(path)
    assert result["ok"] is True and result["event_count"] == 3


def test_hash_chain_tamper_is_detected(tmp_path):
    path = tmp_path / "events.jsonl"
    log = _log(path)
    log.append("A", timestamp="t0")
    log.append("B", timestamp="t1")
    log.append("C", timestamp="t2")
    lines = path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["event_type"] = "TAMPERED"
    lines[1] = json.dumps(tampered, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    result = be.verify_chain(path)
    assert result["ok"] is False
    assert "modified" in result["reason"]


def test_deleted_event_breaks_the_chain_detectably(tmp_path):
    path = tmp_path / "events.jsonl"
    log = _log(path)
    log.append("A", timestamp="t0")
    log.append("B", timestamp="t1")
    log.append("C", timestamp="t2")
    lines = path.read_text().splitlines()
    del lines[1]  # remove the middle event, as a replay/deletion attack would
    path.write_text("\n".join(lines) + "\n")
    result = be.verify_chain(path)
    assert result["ok"] is False


def test_usage_missing_is_unknown_never_zero():
    fields = be.usage_fields(None, role="builder")
    assert fields["builder_input_tokens"] == "unknown"
    assert fields["builder_output_tokens"] == "unknown"
    assert 0 not in fields.values()


def test_usage_present_is_captured_verbatim():
    fields = be.usage_fields({"input": 120, "output": 45, "cache_read": 10, "cache_write": 0}, role="reviewer")
    assert fields["reviewer_input_tokens"] == 120
    assert fields["reviewer_cache_write_tokens"] == 0  # a REAL reported 0 is fine — only MISSING becomes "unknown"


def test_instrumentation_failure_marks_incomplete_never_raises_into_caller(tmp_path):
    be.write_run_manifest(tmp_path, risk_tier="critical", **IDENTITY)
    be.mark_evidence_incomplete(tmp_path, "provider exposed no usage telemetry for this dispatch")
    manifest = json.loads((tmp_path / "benchmark-run-manifest.json").read_text())
    assert manifest["evidence_completeness"] < 100
    assert manifest["incompleteness_reasons"]


def test_determinism_same_inputs_same_serialization(tmp_path):
    log_a = be.EventLog(tmp_path / "a.jsonl", **IDENTITY)
    log_b = be.EventLog(tmp_path / "b.jsonl", **IDENTITY)
    event_a = log_a.append("GO", timestamp="fixed-t")
    event_b = log_b.append("GO", timestamp="fixed-t")
    assert event_a == event_b  # identical inputs -> byte-identical event, including its hash


def test_determinism_same_event_log_same_derived_metrics(tmp_path):
    log = _log(tmp_path / "events.jsonl")
    log.append("GO", timestamp="t0")
    log.append("BUILD_STARTED", timestamp="t1")
    log.append("CANDIDATE_BOUND", candidate_tree="t" * 40, candidate_commit="c" * 40, timestamp="t2")
    log.append("REVIEW_DONE", candidate_tree="t" * 40, timestamp="t3")
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines() if line.strip()]
    metrics_1 = be.derive_metrics(events)
    metrics_2 = be.derive_metrics(events)
    assert metrics_1 == metrics_2
    assert metrics_1["total_events"] == 4
    assert metrics_1["event_type_counts"]["GO"] == 1


def test_manual_and_joao_workflow_modes_use_identical_schema(tmp_path):
    manual = be.EventLog(tmp_path / "manual.jsonl", **{**IDENTITY, "workflow_mode": "manual_same_stack"})
    joao = be.EventLog(tmp_path / "joao.jsonl", **{**IDENTITY, "workflow_mode": "joao"})
    e_manual = manual.append("GO", timestamp="t0")
    e_joao = joao.append("GO", timestamp="t0")
    assert set(e_manual.keys()) == set(e_joao.keys())  # same event schema on both arms

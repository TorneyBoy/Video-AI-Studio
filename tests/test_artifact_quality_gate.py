"""Approval prerequisites using labelled metadata fixtures, never real media."""
from copy import deepcopy

import pytest

from app import models
from app.services.scenario_profiles import get_scenario_profile, scenario_binding
from test_artifact_reviews import access, mutate, report, review, review_case as _review_case
from test_artifact_reviews import edit_case as _edit_case
from timeline_engine import Timeline, create_manifest


review_case = _review_case
edit_case = _edit_case


def finding(severity="error"):
    return {"code": "known_boundary_failure", "severity": severity, "dimension": "technical",
        "message": "Fixture-only measured failure", "evidence": {"source": "acceptance_record", "reference": "fixture"}}


def set_findings(case, values):
    def change(db):
        row = db.get(models.Artifact, case["artifact"])
        row.metadata_json = {**row.metadata_json, "metrics": {"renderer_quality_findings": values}}
    mutate(case, change)


def bind_scene(case):
    binding = scenario_binding(get_scenario_profile("product_showcase"))
    def change(db):
        row = db.get(models.Artifact, case["artifact"])
        job = db.get(models.Job, row.job_id)
        canonical = deepcopy(job.payload["manifest"]["source"]["timeline"])
        canonical["metadata"] = {**canonical.get("metadata", {}), "scenario": binding}
        manifest = create_manifest(Timeline.model_validate(canonical))
        # Marked synthetic legacy manifest isolates the quality gate from the
        # separate persisted-editing contracts already covered elsewhere.
        job.payload = {"manifest": manifest.model_dump(mode="json")}
        row.metadata_json = {**row.metadata_json, "content_hash": manifest.content_hash}
    mutate(case, change)
    return binding


def quality_review(case, *, key="quality-pass", status="passed", scenario=None, silent=False):
    body = {"artifact_id": case["artifact"], "output_sha256": "b" * 64,
        "scenario": scenario or scenario_binding(get_scenario_profile("product_showcase")),
        "rubric_version": "fixture-1", "dimensions": [
            {"dimension": dimension, "status": status, "evidence_ids": [case["artifact"]]}
            for dimension in ("facts", "technical", "narrative", "visual", "audio")]}
    if silent:
        body["dimensions"][-1].update(status="not_applicable", note="Fixture measured no audio stream")
    response = case["client"].post(f"/api/v1/projects/{case['project']}/quality/reviews",
        headers={**case["headers"], "Idempotency-Key": key}, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def work(case):
    response = case["client"].get(f"/api/v1/projects/{case['project']}/works", headers=case["headers"])
    assert response.status_code == 200, response.text
    return next(row for row in response.json()["items"] if row["artifact_id"] == case["artifact"])


def test_legacy_no_scene_or_findings_keeps_original_approval_binding(review_case):
    case = review_case
    initial = report(case)
    assert "quality_evidence" not in initial["binding"]
    assert review(case).status_code == 201
    assert work(case)["state"] == "approved"
    assert access(case).status_code == 200


@pytest.mark.parametrize("severity", ["error", "warning"])
def test_new_quality_finding_invalidates_approval_but_preserves_review_preview(review_case, severity):
    case = review_case
    before = report(case)
    approved = review(case, before).json()
    set_findings(case, [finding(severity)])
    state = report(case)
    assert state["status"] == "stale" and not state["quality_ready"] and state["can_preview"]
    assert state["binding_hash"] != approved["review"]["binding_hash"]
    assert review(case, state, key="approve-known-failure").status_code == 409
    assert access(case).status_code == 409 and case["gateway"].calls == 0
    assert access(case, "preview").status_code == 200
    assert work(case)["state"] == "unavailable"
    # Replaying the old receipt cannot restore eligibility.
    assert review(case, before).json()["review"]["id"] == approved["review"]["id"]
    assert not report(case)["can_download"]


def test_cleared_findings_need_fresh_owner_approval_against_new_report(review_case):
    case = review_case
    assert review(case).status_code == 201
    set_findings(case, [finding()])
    assert not report(case)["quality_ready"]
    set_findings(case, [])
    state = report(case)
    assert state["quality_ready"] and state["status"] == "stale"
    assert access(case).status_code == 409
    assert review(case, state, key="approve-new-clean-report").status_code == 201
    assert access(case).status_code == 200 and work(case)["state"] == "approved"


@pytest.mark.parametrize("values", [{"unexpected": "not-a-list"}, [{"severity": "error"}]])
def test_malformed_quality_report_never_falls_back_to_legacy_approval(review_case, values):
    case = review_case
    set_findings(case, values)
    state = report(case)
    assert not state["quality_ready"] and state["can_preview"]
    denied = review(case, state)
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "artifact_quality_evidence_invalid"
    assert work(case)["state"] == "unavailable"


def test_bound_scene_requires_complete_current_review_then_separate_owner_approval(review_case):
    case = review_case
    binding = bind_scene(case)
    state = report(case)
    assert state["can_preview"] and not state["quality_ready"]
    assert review(case, state).status_code == 409
    assessed = quality_review(case, scenario=binding)
    assert assessed["assessment"]["verdict"] == "passed"
    state = report(case)
    assert state["quality_ready"] and not state["can_download"]
    assert review(case, state, key="approve-ready-scene").status_code == 201
    assert work(case)["state"] == "approved"
    assert access(case).status_code == 200
    quality_review(case, key="found-new-failure", status="failed", scenario=binding)
    changed = report(case)
    assert changed["status"] == "stale" and not changed["can_download"]
    assert access(case).status_code == 409 and work(case)["state"] == "unavailable"
    quality_review(case, key="corrected-human-assessment", scenario=binding)
    refreshed = report(case)
    assert refreshed["quality_ready"] and refreshed["status"] == "stale"
    assert access(case).status_code == 409
    assert review(case, refreshed, key="approve-new-evidence").status_code == 201
    assert access(case).status_code == 200 and work(case)["state"] == "approved"


def test_quality_review_is_not_trusted_from_its_cached_positive_verdict(review_case):
    case = review_case
    binding = bind_scene(case)
    result = quality_review(case, status="failed", scenario=binding)
    def change(db):
        row = db.get(models.Artifact, result["id"])
        row.metadata_json = {**row.metadata_json, "assessment": {"verdict": "passed"}}
    mutate(case, change)
    assert not report(case)["quality_ready"]
    assert review(case).status_code == 409


def test_current_output_sha_and_scene_must_match_review(review_case):
    case = review_case
    binding = bind_scene(case)
    quality_review(case, scenario=binding)
    assert review(case).status_code == 201
    def changed_output(db):
        row = db.get(models.Artifact, case["artifact"])
        row.metadata_json = {**row.metadata_json, "output_sha256": "c" * 64,
            "quality_control": {**row.metadata_json["quality_control"], "output_sha256": "c" * 64}}
        db.get(models.ArtifactStorage, row.id).sha256 = "c" * 64
    mutate(case, changed_output)
    state = report(case)
    assert not state["quality_ready"] and state["status"] == "stale"
    assert access(case).status_code == 409 and work(case)["state"] == "unavailable"


def test_quality_change_during_signing_returns_no_download_url(review_case):
    case = review_case
    assert review(case).status_code == 201
    def hook():
        case["gateway"].hook = None
        set_findings(case, [finding()])
    case["gateway"].hook = hook
    response = access(case)
    assert response.status_code == 409 and "short-lived-media" not in response.text


def test_conflicting_server_output_hashes_cannot_keep_an_old_approval(review_case):
    case = review_case
    assert review(case).status_code == 201
    def change(db):
        row = db.get(models.Artifact, case["artifact"])
        row.metadata_json = {**row.metadata_json, "output_sha256": "c" * 64}
    mutate(case, change)
    assert not report(case)["quality_ready"]
    assert access(case).status_code == 409 and work(case)["state"] == "unavailable"


@pytest.mark.parametrize("change", ["qc_true", "media_true", "media_null", "media_string"])
def test_audio_not_applicable_is_rechecked_against_current_media_evidence(review_case, change):
    case = review_case
    quality_review(case, silent=True)
    assert review(case).status_code == 201
    def change_evidence(db):
        row = db.get(models.Artifact, case["artifact"])
        if change == "qc_true":
            row.metadata_json = {**row.metadata_json, "quality_control": {**row.metadata_json["quality_control"], "has_audio": True}}
        else:
            value = {"media_true": True, "media_null": None, "media_string": "false"}[change]
            row.metadata_json = {**row.metadata_json, "metrics": {"media": {"has_audio": value}}}
    mutate(case, change_evidence)
    state = report(case)
    assert not state["quality_ready"] and state["can_preview"]
    assert review(case, state, key="approve-changed-audio").status_code == 409
    assert access(case).status_code == 409 and work(case)["state"] == "unavailable"


@pytest.mark.parametrize("status", ["failed", "unreviewed"])
def test_unbound_output_failed_or_incomplete_scene_review_also_blocks_existing_approval(review_case, status):
    case = review_case
    assert review(case).status_code == 201
    quality_review(case, status=status)
    state = report(case)
    assert not state["quality_ready"] and state["status"] == "stale"
    assert access(case).status_code == 409 and work(case)["state"] == "unavailable"


def test_foreign_project_review_is_not_used_as_quality_evidence(review_case):
    case = review_case
    own = quality_review(case)
    def foreign(db):
        project = models.Project(tenant_id=case["tenant"], name="Other fixture")
        db.add(project); db.flush()
        db.add(models.Artifact(tenant_id=case["tenant"], project_id=project.id, kind="scenario.quality_review",
            uri="quality-review://fixture-only", metadata_json={"artifact_id": case["artifact"], "review": {"bad": "fixture"}}))
    mutate(case, foreign)
    state = report(case)
    assert state["quality_ready"] and state["binding"]["quality_evidence"]["review_id"] == own["id"]
    assert review(case).status_code == 201 and work(case)["state"] == "approved"

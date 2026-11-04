"""Real API/worker/editor contracts with declared synthetic ASR evidence.

No recognition, media decoding or cloud request is performed by these tests.
"""
import pytest

from app.api import routers
from app.database import get_db
from app.main import app
from app.services.scenario_profiles import get_scenario_profile, scenario_binding
from app.services.worker import process_queued_auto_edit_jobs
from test_edit_revisions import save, timeline


def speech_metadata(uri, *, length=1.0, quality=.8, semantic=.8):
    return {"duration": length, "analysis": {
        "schema_version": "media-analysis/2", "provider": "fixture-inspector", "source": {"uri": uri},
        "media": {"status": "ok", "duration_seconds": length, "size_bytes": 100, "format": "mp4"},
        "video": {"width": 480, "height": 832}, "audio": {"sample_rate": 48000, "channels": 1},
        "shots": [{"start": 0.0, "end": length, "duration": length, "quality": quality, "semantic_match": semantic}],
        "shot_analysis": {"status": "ok", "provider": "fixture-cuts", "version": "1"},
        "transcription": {"status": "ok", "provider": "fixture-asr", "version": "1",
            "word_alignment": {"status": "ok", "provider": "fixture-aligner", "version": "1"},
            "segments": [{"id": "complete-sentence", "start": 0.0, "end": length,
                          "text": "Keep complete sentence.", "speaker_id": "narrator", "boundary_safe": True}],
            "words": [{"id": "first", "segment_id": "complete-sentence", "start": .1, "end": .3, "text": "Keep"},
                      {"id": "last", "segment_id": "complete-sentence", "start": .5, "end": length - .1,
                       "text": "complete sentence."}]}}}


@pytest.fixture
def scene_case(client, monkeypatch):
    monkeypatch.setattr(routers.settings, "asset_storage_mode", "local")
    tenant = client.post("/api/v1/tenants", json={"slug": "scene-editor", "name": "Scene editor"}).json()["id"]
    headers = {"X-Tenant-ID": tenant}
    project = client.post("/api/v1/projects", headers=headers, json={"name": "Spoken evidence fixture"}).json()["id"]
    content = {"schema_version": "1.0", "id": "spoken", "version": "1", "fps": 30, "duration": 2,
               "metadata": {"scenario": {"id": "spoken_explainer", "version": "1"}},
               "slots": [{"name": "first", "type": "video"}, {"name": "second", "type": "video"}]}
    response = client.post("/api/v1/templates", headers=headers,
                           json={"name": "Spoken fixture", "project_id": project, "version": "1", "content": content})
    assert response.status_code == 201, response.text
    template_id = response.json()["id"]
    assets = []
    for index, (length, quality) in enumerate(((1.0, .95), (1.0, .9), (2.0, .7), (1.0, .6))):
        uri = f"oss://fixture-source/spoken-{index}.mp4"
        metadata = speech_metadata(uri, length=length, quality=quality)
        response = client.post("/api/v1/assets", headers=headers, json={"name": f"speech-{index}",
            "project_id": project, "kind": "video/mp4", "uri": uri, "metadata": metadata})
        assert response.status_code == 201, response.text
        assets.append(response.json())
    response = client.post("/api/v1/jobs/auto-edit", headers={**headers, "Idempotency-Key": "spoken-plan"},
                           json={"project_id": project, "template_id": template_id, "asset_ids": [a["id"] for a in assets]})
    assert response.status_code == 201, response.text
    job = response.json()
    iterator = app.dependency_overrides[get_db](); db = next(iterator)
    try:
        outcomes = process_queued_auto_edit_jobs(db)
        assert outcomes[0]["status"] == "succeeded", outcomes
    finally:
        iterator.close()
    response = client.post(f"/api/v1/jobs/{job['id']}/editing", headers=headers)
    assert response.status_code == 200, response.text
    return {"client": client, "headers": headers, "project": project, "template": template_id,
            "job": job, "assets": assets, "revision": response.json(), "tenant": tenant}


def assert_unchanged(case, before):
    current = case["client"].get(f"/api/v1/editing/{before['document_id']}", headers=case["headers"])
    assert current.status_code == 200, current.text
    assert current.json()["revision_id"] == before["revision_id"]
    assert current.json()["timeline_hash"] == before["timeline_hash"]


def test_scene_api_worker_and_snapshot_preserve_frozen_rules_and_words(scene_case):
    case = scene_case; first = case["revision"]
    expected = scenario_binding(get_scenario_profile("spoken_explainer"))
    assert case["job"]["payload"]["template_snapshot"]["metadata"]["scenario"] == expected
    assert first["template"]["metadata"]["scenario"] == expected
    assert first["plan"]["scenario"] == expected
    value = timeline(case, first)
    assert value["metadata"]["scenario"] == expected
    assert all("#speech-" in row["asset_id"] for row in first["plan"]["selected"])
    for clip in value["tracks"][0]["clips"]:
        assert clip["metadata"]["speech_unit"]["word_ids"] == ["first", "last"]
        assert clip["source_end"] - clip["source_start"] == 1


@pytest.mark.parametrize("kind", ["trim", "split", "resize_after_precise", "replace_long"])
def test_manual_edits_cannot_bypass_complete_spoken_units(scene_case, kind):
    case = scene_case; first = case["revision"]; row = first["plan"]["selected"][0]
    if kind == "trim":
        operations = [{"op": "trim_clip", "clip_id": row["clip_id"], "source_start": 0, "source_end": .6}]
    elif kind == "split":
        operations = [{"op": "split_clip", "clip_id": row["clip_id"], "offset_seconds": .5}]
    elif kind == "resize_after_precise":
        changed = save(case, first, "audio-only", [{"op": "set_clip_audio", "clip_id": row["clip_id"], "volume": .5}])
        assert changed.status_code == 201, changed.text
        first = changed.json()
        operations = [{"op": "configure", "target_duration": 1, "aspect_ratio": "9:16"}]
    else:
        long_id = case["assets"][2]["id"] + "#speech-0"
        operations = [{"op": "replace", "selection_id": row["selection_id"], "candidate_id": long_id}]
    response = save(case, first, "unsafe-" + kind, operations)
    assert response.status_code in {409, 422}, response.text
    assert_unchanged(case, first)


def test_partial_replan_cannot_trim_the_better_but_longer_spoken_candidate(scene_case):
    case = scene_case; first = case["revision"]; row = first["plan"]["selected"][0]
    response = save(case, first, "replan-safe", [{"op": "replan", "selection_ids": [row["selection_id"]]}])
    if response.status_code in {409, 422}:
        assert_unchanged(case, first)
        return
    assert response.status_code == 201, response.text
    value = timeline(case, response.json())
    clip = next(c for c in value["tracks"][0]["clips"] if c["id"] == row["clip_id"])
    assert clip["asset_id"] != case["assets"][2]["id"]  # Two-second unit must not be cut down to one.
    assert clip["source_end"] - clip["source_start"] == 1
    assert clip["metadata"]["speech_unit"]["evidence_ref"].startswith("transcript:" + clip["asset_id"] + ":")


def test_equal_length_replacement_refreshes_speech_evidence_and_supports_undo(scene_case):
    case = scene_case; first = case["revision"]; row = first["plan"]["selected"][0]
    unused = case["assets"][3]["id"] + "#speech-0"
    response = save(case, first, "replace-safe", [{"op": "replace", "selection_id": row["selection_id"], "candidate_id": unused}])
    assert response.status_code == 201, response.text
    second = response.json(); value = timeline(case, second)
    clip = next(c for c in value["tracks"][0]["clips"] if c["id"] == row["clip_id"])
    assert clip["metadata"]["source_shot_id"] == unused
    assert clip["metadata"]["speech_unit"]["evidence_ref"].startswith("transcript:" + case["assets"][3]["id"] + ":")
    undone = save(case, second, "undo-safe-replace", [{"op": "undo"}])
    assert undone.status_code == 201, undone.text
    restored = timeline(case, undone.json()); original = timeline(case, first)
    restored.pop("id"); original.pop("id")
    assert restored == original


def test_complete_spoken_source_without_measured_scene_features_is_not_a_manual_candidate(scene_case):
    case = scene_case; first = case["revision"]
    uri = "oss://fixture-source/unscored.mp4"
    metadata = speech_metadata(uri)
    metadata["analysis"]["shots"][0].pop("quality")
    metadata["analysis"]["shots"][0].pop("semantic_match")
    response = case["client"].post("/api/v1/assets", headers=case["headers"], json={"name": "No measured scores",
        "project_id": case["project"], "kind": "video/mp4", "uri": uri, "metadata": metadata})
    assert response.status_code == 201, response.text
    id = response.json()["id"]
    added = save(case, first, "add-unscored", [{"op": "refresh_candidates"}], [id])
    assert added.status_code == 201, added.text
    second = added.json()
    result = case["client"].get(f"/api/v1/editing/{second['document_id']}/revisions/{second['revision_id']}/candidates", headers=case["headers"])
    assert result.status_code == 200, result.text
    assert id + "#speech-0" not in {item["candidate_id"] for item in result.json()["items"]}


def test_invalid_scene_hash_is_refused_before_plan_job_exists(client, monkeypatch):
    monkeypatch.setattr(routers.settings, "asset_storage_mode", "local")
    tenant = client.post("/api/v1/tenants", json={"slug": "bad-scene", "name": "Bad scene"}).json()["id"]
    headers = {"X-Tenant-ID": tenant}
    project = client.post("/api/v1/projects", headers=headers, json={"name": "Bad scene"}).json()["id"]
    value = {"id": "bad-scene", "version": "1", "slots": [{"name": "main", "type": "video"}],
             "metadata": {"scenario": {"id": "spoken_explainer", "version": "1", "content_hash": "0" * 64}}}
    response = client.post("/api/v1/templates", headers=headers,
        json={"name": "Invalid scene", "project_id": project, "version": "1", "content": value})
    assert response.status_code == 201, response.text
    result = client.post("/api/v1/jobs/auto-edit", headers={**headers, "Idempotency-Key": "invalid-profile"},
        json={"project_id": project, "template_id": response.json()["id"]})
    assert result.status_code == 422 and result.json()["detail"]["code"] == "invalid_template"

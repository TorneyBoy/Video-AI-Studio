"""Scenario review aggregation and benchmark split isolation.

These functions do not inspect media or turn a metadata declaration into a
trusted measurement. Evidence ownership/source verification belongs to the
calling project-scoped API or inspector.
"""
from __future__ import annotations

from ..scenario_schemas import ScenarioBenchmark, ScenarioQualityReview
from .scenario_profiles import resolve_scenario_binding, scenario_binding


def assess_scenario_quality(review: ScenarioQualityReview | dict, *, technical_findings=()) -> dict:
    review = review if isinstance(review, ScenarioQualityReview) else ScenarioQualityReview.model_validate(review)
    profile = resolve_scenario_binding(review.scenario)
    findings = []
    if technical_findings:
        from .media_quality import QualityFinding
        findings = [QualityFinding.model_validate(value).model_dump(mode="json") for value in technical_findings]
    rows = {row.dimension: row for row in review.dimensions}
    failed = [key for key, row in rows.items() if row.status == "failed"]
    missing = [key for key in profile.required_quality_dimensions if key not in rows or rows[key].status == "unreviewed"]
    invalid_na = [key for key, row in rows.items() if row.status == "not_applicable" and
                  (key != "audio" or profile.source_policy == "speech_first")]
    errors = [row for row in findings if row["severity"] == "error"]
    warnings = [row for row in findings if row["severity"] == "warning"]
    if failed or errors or invalid_na:
        verdict = "failed"
    elif missing or warnings:
        verdict = "needs_review"
    else:
        verdict = "passed"
    return {"schema_version": "scenario-quality-assessment/1", "scope": "scenario_review",
            "verdict": verdict, "scenario": scenario_binding(profile), "output_sha256": review.output_sha256,
            "reviewer_id": review.reviewer_id, "rubric_version": review.rubric_version,
            "failed_dimensions": failed, "missing_dimensions": missing, "invalid_not_applicable": invalid_na,
            "dimensions": [row.model_dump(mode="json") for row in review.dimensions], "findings": findings,
            "profile_validation_status": profile.validation_status,
            "commercial_or_release_admission": False}


def tuning_cases(benchmark: ScenarioBenchmark | dict, case_ids: list[str]) -> list[dict]:
    benchmark = benchmark if isinstance(benchmark, ScenarioBenchmark) else ScenarioBenchmark.model_validate(benchmark)
    if not case_ids or len(case_ids) != len(set(case_ids)):
        raise ValueError("select distinct benchmark tuning cases")
    by_id = {row.id: row for row in benchmark.cases}
    selected = []
    for id in case_ids:
        case = by_id.get(id)
        if case is None or case.split != "tuning" or case.status != "ready":
            raise ValueError("only ready tuning cases may enter prompt optimization")
        resolve_scenario_binding(case.scenario)
        selected.append(case.model_dump(mode="json"))
    return selected


def benchmark_summary(benchmark: ScenarioBenchmark | dict) -> dict:
    benchmark = benchmark if isinstance(benchmark, ScenarioBenchmark) else ScenarioBenchmark.model_validate(benchmark)
    for case in benchmark.cases:
        resolve_scenario_binding(case.scenario)
    return {"schema_version": benchmark.schema_version, "id": benchmark.id, "version": benchmark.version,
            "case_count": len(benchmark.cases),
            "ready_cases": sum(case.status == "ready" for case in benchmark.cases),
            "planned_cases": sum(case.status == "planned" for case in benchmark.cases),
            "splits": {split: sum(case.split == split for case in benchmark.cases)
                       for split in ("tuning", "validation", "holdout")},
            "real_media_acceptance": False}

"""Compare a real analysis receipt with an annotated input, without cloud calls."""

import argparse
import hashlib
import json
from pathlib import Path
import re

from app.services.analysis_contract import validate_hosted_analysis


def distance(reference: str, measured: str) -> int:
    previous = list(range(len(measured) + 1))
    for i, left in enumerate(reference, 1):
        current = [i]
        for j, right in enumerate(measured, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    if report["status"] != "succeeded":
        parser.error("The real operation has not succeeded")
    analysis = validate_hosted_analysis(report["analysis"], source_uri=report["analysis"]["source"]["uri"])
    measured = "".join(row["text"] for row in analysis["transcription"]["segments"])
    reference = fixture["expected_transcript"]
    normalized_reference = re.sub(r"[^\w]", "", reference, flags=re.UNICODE)
    normalized_measured = re.sub(r"[^\w]", "", measured, flags=re.UNICODE)
    if not normalized_reference:
        parser.error("An annotated nonempty transcript is required")
    edits = distance(normalized_reference, normalized_measured)
    cuts = [row["start"] for row in analysis["shots"][1:]]
    expected_cuts = fixture["expected_cut_seconds"]
    cut_error = (max((abs(x - y) for x, y in zip(cuts, expected_cuts)), default=0)
                 if len(cuts) == len(expected_cuts) else None)
    source_matches = report["metrics"]["source_evidence"]["sha256"] == fixture["source_sha256"]
    passed = (source_matches and cut_error is not None and cut_error <= 1 / 30 and edits == 0
              and analysis["shot_analysis"]["status"] == "ok" and analysis["transcription"]["status"] == "ok")
    result = {"status": "passed" if passed else "failed", "scope": "one fixed synthetic sample; not generalized ASR accuracy",
        "request_id": report["request_id"], "provider_job_id": report["provider_job_id"],
        "source_sha256": report["metrics"]["source_evidence"]["sha256"], "source_matches": source_matches,
        "expected_cut_seconds": expected_cuts, "measured_cut_seconds": cuts, "max_cut_error_seconds": cut_error,
        "transcript_expected": reference, "transcript_measured": measured,
        "normalization": "punctuation and whitespace removed; characters retained",
        "character_edits": edits, "reference_characters": len(normalized_reference),
        "character_error_rate": edits / len(normalized_reference), "transcript_segments": analysis["transcription"]["segments"],
        "cloud_jobs": report["metrics"]["cloud_jobs"],
        "analysis_object_sha256": report["metrics"]["analysis_evidence"]["sha256"],
        "raw_receipt_sha256": hashlib.sha256(args.report.read_bytes()).hexdigest(), "cost": report["cost"]}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

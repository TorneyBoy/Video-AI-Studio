from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .manifest import create_manifest
from .schema import TemplateBinding, TemplateDefinition, Timeline
from .validation import validate_all


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="timeline-engine", description="Validate timelines and produce render manifests")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="validate timeline and optional template binding")
    v.add_argument("timeline")
    v.add_argument("--template")
    v.add_argument("--binding")
    m = sub.add_parser("manifest", help="create deterministic render manifest")
    m.add_argument("timeline")
    m.add_argument("--template")
    m.add_argument("--binding")
    m.add_argument("--output", help="output JSON path; defaults to stdout")
    args = parser.parse_args(argv)
    try:
        timeline = Timeline.model_validate(_load(args.timeline))
        template = TemplateDefinition.model_validate(_load(args.template)) if getattr(args, "template", None) else None
        binding = TemplateBinding.model_validate(_load(args.binding)) if getattr(args, "binding", None) else None
        if args.command == "validate":
            payload = validate_all(timeline, template, binding).as_dict()
        else:
            payload = create_manifest(timeline, template=template, binding=binding).model_dump(mode="json")
        text = json.dumps(payload, indent=2, sort_keys=True)
        if getattr(args, "output", None):
            Path(args.output).write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        if args.command == "validate" and not payload["valid"]:
            return 2
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


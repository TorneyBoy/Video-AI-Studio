"""Canonical timeline validation and render hand-off primitives."""

from .job import JobEvent, JobState, RenderJob
from .manifest import RenderManifest, create_manifest
from .schema import (
    AssetRef, AssetType, Clip, Placement, Resolution, SlotConstraints, SlotType, SlotValue,
    TemplateBinding, TemplateDefinition, TemplateSlot, TextStyle, TimeRange, Timeline, Track,
    TrackType, Transition, TransitionType,
)
from .validation import Severity, ValidationIssue, ValidationReport, validate_all, validate_template_binding, validate_timeline

__all__ = [
    "AssetRef", "AssetType", "Clip", "Placement", "Resolution", "SlotConstraints", "SlotType", "SlotValue",
    "TemplateBinding", "TemplateDefinition", "TemplateSlot", "TextStyle", "TimeRange", "Timeline", "Track",
    "TrackType", "Transition", "TransitionType",
    "RenderManifest", "create_manifest", "JobEvent", "JobState", "RenderJob", "Severity", "ValidationIssue",
    "ValidationReport", "validate_all", "validate_template_binding", "validate_timeline",
]


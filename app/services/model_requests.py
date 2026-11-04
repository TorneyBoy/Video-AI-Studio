"""Versioned AI request contracts and evidence-based selection.

This module maps the platform's normalized gateway request, not a vendor's
native network API. Contract/transport tests do not confer media acceptance.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from .editing_errors import EditingError


Scalar = StrictStr | StrictInt | StrictFloat | StrictBool
IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
PARAMETER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
OPERATIONS = Literal["video_generation", "face_replace", "person_replace"]
RESERVED_PARAMETERS = {"provider", "model_id", "model_version", "provider_job_id", "output_dir", "output_path",
                       "region", "tenant_id", "request_id", "commercial_use", "consent_ids", "duration"}


def safe_parameter(name: str, *, duration: bool = False) -> bool:
    normalized = name.lower()
    return bool(PARAMETER.fullmatch(name) and (duration and name == "duration" or name not in RESERVED_PARAMETERS)
                and not any(part in normalized for part in ("secret", "password", "authorization", "api_key", "apikey", "token", "endpoint", "headers")))


class ParameterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    type: Literal["string", "integer", "number", "boolean"]
    enum: list[Scalar] | None = Field(default=None, min_length=1, max_length=64)
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = Field(default=10000, ge=1, le=20000)
    required: bool = False
    default: Scalar | None = None

    def validate_value(self, value: Any):
        valid = {"string": isinstance(value, str), "integer": type(value) is int,
                 "number": type(value) in {int, float}, "boolean": type(value) is bool}[self.type]
        if not valid:
            raise ValueError("parameter type differs from model contract")
        if type(value) in {int, float}:
            if not math.isfinite(value) or self.minimum is not None and value < self.minimum or self.maximum is not None and value > self.maximum:
                raise ValueError("parameter is outside model bounds")
        if isinstance(value, str) and (len(value) > self.max_length or any(ord(c) < 32 and c not in "\n\t" for c in value)):
            raise ValueError("parameter string is outside model bounds")
        if self.enum is not None and not any(item == value and (type(item) is type(value)
                or self.type == "number" and type(item) in {int, float} and type(value) in {int, float}) for item in self.enum):
            raise ValueError("parameter is not in model choices")
        return value

    @model_validator(mode="after")
    def consistent_spec(self):
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("parameter minimum exceeds maximum")
        for value in self.enum or []:
            self.validate_value(value)
        if self.default is not None:
            self.validate_value(self.default)
        return self


class ModelRequestContract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    schema_version: Literal["ai-request-contract/1"] = "ai-request-contract/1"
    id: str = Field(pattern=IDENTIFIER)
    version: str = Field(pattern=IDENTIFIER)
    operations: list[OPERATIONS] = Field(min_length=1, max_length=3)
    input_mode: Literal["text", "image_reference", "identity_video"]
    prompt_version: str = Field(pattern=IDENTIFIER)
    prompt_template: str = Field(default="{prompt}", min_length=1, max_length=4000)
    parameters: dict[str, ParameterSpec] = Field(max_length=16)
    parameter_mapping: dict[str, str] = Field(default_factory=dict, max_length=16)
    output_short_edges: dict[str, int] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def consistent_contract(self):
        if any(not safe_parameter(name, duration=True) for name in self.parameters):
            raise ValueError("unsafe model parameter")
        if "duration" not in self.parameters or self.parameters["duration"].type not in {"number", "integer"}:
            raise ValueError("model contract requires an explicit duration parameter")
        if any(name not in self.parameters or not safe_parameter(target, duration=True)
               for name, target in self.parameter_mapping.items()):
            raise ValueError("unsafe model parameter mapping")
        targets = [self.parameter_mapping.get(name, name) for name in self.parameters]
        if len(targets) != len(set(targets)):
            raise ValueError("model parameter mapping has collisions")
        tokens = re.findall(r"\{[^{}]*\}", self.prompt_template)
        if self.prompt_template.count("{prompt}") != 1 or any(token not in {"{prompt}", "{scenario}"} for token in tokens):
            raise ValueError("prompt template must contain one prompt and only approved placeholders")
        if any(not isinstance(label, str) or not 0 < len(label) <= 100 or type(edge) is not int or not 16 <= edge <= 8192
               for label, edge in self.output_short_edges.items()):
            raise ValueError("invalid model output specification")
        return self


class ScenarioEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    scenario_id: str = Field(pattern=IDENTIFIER)
    scenario_version: str = Field(pattern=IDENTIFIER)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    benchmark_version: str = Field(pattern=IDENTIFIER)
    report_id: str = Field(pattern=IDENTIFIER)
    status: Literal["testing", "verified", "suspended"]
    quality_score: float = Field(ge=0, le=1)


class ModelRouting(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    version: str = Field(pattern=IDENTIFIER)
    default: bool = False
    scenarios: list[ScenarioEvidence] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def unique_evidence(self):
        identities = [(row.scenario_id, row.scenario_version, row.content_hash) for row in self.scenarios]
        if len(identities) != len(set(identities)):
            raise ValueError("ambiguous duplicate scenario evidence")
        return self


def public_contract(value: Any) -> dict | None:
    try:
        return ModelRequestContract.model_validate(value).model_dump(mode="json", exclude_none=True)
    except (ValueError, TypeError):
        return None


def public_routing(value: Any) -> dict | None:
    try:
        return ModelRouting.model_validate(value).model_dump(mode="json")
    except (ValueError, TypeError):
        return None


def prepare_model_request(entry: dict, body, duration: float, scenario: dict | None = None) -> dict:
    """Validate and freeze both standard options and mapped gateway parameters."""
    value = (entry.get("limits") or {}).get("request_contract")
    if value is None:
        # Historical Wan-only inputs remain deliberately bounded. A different
        # adapter must declare its schema before receiving arbitrary options.
        options = dict(body.options)
        if body.operation == "person_replace" and set(options) - {"mode"}:
            raise EditingError("ai_parameter_invalid", "当前人物替换模型仅接受标准/专业模式", 409)
        if set(options) - {"seed", "resolution", "mode", "negative_prompt"}:
            raise EditingError("ai_parameter_contract_required", "此模型尚未声明这些参数，请选择已配置的模型", 409)
        if options.get("mode", "wan-std") not in {"wan-std", "wan-pro"} or options.get("resolution", "720P") not in {"480P", "720P", "1080P"}:
            raise EditingError("ai_parameter_invalid", "生成参数不在模型支持范围", 409)
        if "seed" in options and (type(options["seed"]) is not int or not 0 <= options["seed"] <= 2147483647):
            raise EditingError("ai_parameter_invalid", "seed超出当前模型范围", 409)
        options["duration"] = duration
        if body.operation == "video_generation":
            options.setdefault("resolution", ((entry.get("limits") or {}).get("generation_resolutions") or ["1080P"])[0])
        return {"execution_options": options, "execution_prompt": body.prompt,
                "prompt_version": "legacy-user-prompt/1"}
    try:
        contract = ModelRequestContract.model_validate(value)
        if body.operation not in contract.operations:
            raise ValueError("operation differs")
        actual_mode = "identity_video" if body.operation != "video_generation" else "image_reference" if body.reference_asset_ids else "text"
        if contract.input_mode != actual_mode:
            raise ValueError("input mode differs")
        if set(body.options) - set(contract.parameters) or "duration" in body.options:
            raise ValueError("unsupported parameter")
        standard = {}
        for name, definition in contract.parameters.items():
            value = duration if name == "duration" else body.options.get(name, definition.default)
            if value is None:
                if definition.required:
                    raise ValueError("required parameter missing")
                continue
            # JSON numeric durations use float in the public input; normalize
            # an exact whole second only for a declared integer contract.
            if name == "duration" and definition.type == "integer" and float(value).is_integer():
                value = int(value)
            standard[name] = definition.validate_value(value)
        prompt = re.sub(r"\{(?:prompt|scenario)\}",
                        lambda match: body.prompt if match.group() == "{prompt}" else (scenario or {}).get("prompt_context", ""),
                        contract.prompt_template)
        if len(prompt) > (entry.get("limits") or {}).get("max_prompt_chars", 20000):
            raise ValueError("expanded prompt too long")
        mapped = {contract.parameter_mapping.get(name, name): value for name, value in standard.items()}
        return {"execution_options": standard, "provider_parameters": mapped, "execution_prompt": prompt,
                "request_contract": contract.model_dump(mode="json", exclude_none=True),
                "prompt_version": contract.prompt_version}
    except (ValueError, TypeError) as exc:
        raise EditingError("ai_model_input_unsupported", "此模型不支持所选输入或参数，请核对模型选项", 409) from exc


def choose_model(candidates: list[tuple[dict, dict]], body, scenario: dict | None) -> tuple[dict, dict, dict]:
    """Choose only eligible input contracts, never treat catalogue order as quality."""
    if not candidates:
        raise EditingError("ai_capability_unavailable", "没有覆盖所选操作和参数的已验收托管服务", 409)
    explicitly_selected = bool(body.provider or body.model_id or body.model_version)
    if explicitly_selected:
        if len(candidates) != 1:
            raise EditingError("ai_model_ambiguous", "请选择明确的供应商、模型及版本", 409)
        selected, execution = candidates[0]
        return selected, execution, {"method": "user_selected"}
    if scenario and scenario.get("validation_status") == "verified" and scenario.get("acceptance_report"):
        scored = []
        for entry, execution in candidates:
            routing = public_routing((entry.get("limits") or {}).get("model_routing"))
            if routing:
                evidence = [row for row in routing["scenarios"] if row["status"] == "verified"
                    and row["scenario_id"] == scenario["id"] and row["scenario_version"] == scenario["version"]
                    and row["content_hash"] == scenario["content_hash"]]
                if len(evidence) == 1:
                    scored.append((evidence[0]["quality_score"], entry, execution, routing, evidence[0]))
        if scored:
            best = max(row[0] for row in scored)
            winners = [row for row in scored if row[0] == best]
            if len(winners) != 1:
                raise EditingError("ai_model_selection_required", "场景评测结果并列，请明确选择模型", 409)
            _, entry, execution, routing, evidence = winners[0]
            return entry, execution, {"method": "verified_scenario", "routing_version": routing["version"],
                                      "evidence": deepcopy(evidence)}
    if len(candidates) == 1:
        entry, execution = candidates[0]
        return entry, execution, {"method": "single_eligible", "quality_ranked": False}
    defaults = []
    for entry, execution in candidates:
        routing = public_routing((entry.get("limits") or {}).get("model_routing"))
        if routing and routing["default"]:
            defaults.append((entry, execution, routing))
    if len(defaults) == 1 and not scenario:
        entry, execution, routing = defaults[0]
        return entry, execution, {"method": "operator_default", "routing_version": routing["version"], "quality_ranked": False}
    raise EditingError("ai_model_selection_required", "多个模型可用但尚无适用场景评测，请明确选择模型", 409)

"""Project the initial gateway provider aliases without rewriting receipts."""


def registered_result(result: dict, *, provider: str, legacy_provider: str, model_id: str, version: str) -> dict:
    model = result.get("model") or {}
    if (model.get("provider"), model.get("model_id"), model.get("version")) != (legacy_provider, model_id, version):
        return result
    return {**result, "model": {**model, "provider": provider},
        "metrics": {**(result.get("metrics") or {}), "gateway_identity_correction": {
            "legacy_provider": legacy_provider, "registered_provider": provider,
            "basis": "initial_gateway_alias_correction", "stored_receipt_unchanged": True}}}

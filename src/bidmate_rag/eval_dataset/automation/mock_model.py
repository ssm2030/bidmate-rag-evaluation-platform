def response(stage: str, payload: dict) -> dict:
    return {"stage": stage, "provider": "mock", "payload_hash": str(len(str(payload)))}

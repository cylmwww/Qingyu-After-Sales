from app.core.constants import PII_MASK
from app.core.security import redact_mapping


def test_redact_mapping_masks_secrets_and_nested_strings() -> None:
    payload = {
        "api_key": "sk-live-secret",
        "note": "电话 13900001111",
        "nested": {"password": "hunter2", "order": "ORD1234567"},
        "count": 3,
    }
    redacted = redact_mapping(payload)
    assert redacted["api_key"] == PII_MASK
    assert "13900001111" not in str(redacted["note"])
    nested = redacted["nested"]
    assert isinstance(nested, dict)
    assert nested["password"] == PII_MASK
    assert "ORD1234567" not in str(nested["order"])
    assert redacted["count"] == 3

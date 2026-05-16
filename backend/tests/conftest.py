import pytest

from app.services.security_service import (
    cosine_similarity,
    detect_pii,
    injection_score,
    mask_pii,
)


@pytest.fixture
def sample_text():
    return "Contact me at jane.doe@example.com or 555-123-4567"

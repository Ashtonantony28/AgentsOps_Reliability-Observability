from app.services.auth_service import hash_password, verify_password


def test_password_roundtrip():
    h = hash_password("S3cret!!")
    assert verify_password("S3cret!!", h)
    assert not verify_password("wrong", h)

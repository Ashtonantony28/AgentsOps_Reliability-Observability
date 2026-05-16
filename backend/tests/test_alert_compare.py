from app.services.alert_service import compare


def test_compare():
    assert compare(5.0, ">", 3.0) is True
    assert compare(3.0, ">=", 3.0) is True
    assert compare(3.0, "<", 5.0) is True
    assert compare(3.0, "<=", 3.0) is True
    assert compare(3.0, "==", 3.0) is True
    assert compare(3.0, "wat", 3.0) is False

from mail import mask_identifier


def test_mask_identifier_plain_login():
    assert mask_identifier("admin") == "ad***"


def test_mask_identifier_with_at_sign():
    m = mask_identifier("ab@example.com")
    assert "@" in m
    assert "example.com" in m


def test_mask_identifier_empty():
    assert mask_identifier("") == "***"
    assert mask_identifier("   ") == "***"

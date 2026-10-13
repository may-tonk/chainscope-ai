from chainscope.config import Settings


def test_password_url_encoding_and_hidden_repr():
    settings = Settings(
        "https://example.invalid/secret-key",
        "localhost",
        3306,
        "user",
        "p@ss:/word",
        "chainscope",
    )
    url = settings.database_url()
    assert url.password == "p@ss:/word"
    assert "secret-key" not in repr(settings)
    assert "p@ss:/word" not in repr(settings)

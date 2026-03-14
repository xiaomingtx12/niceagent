from internal.core.mcp.security import MCPSecretCipher


def test_mcp_secret_cipher_encrypt_and_decrypt(monkeypatch):
    monkeypatch.setenv("MCP_SECRET_KEY", "test-secret-key")

    encrypted_env = MCPSecretCipher.encrypt_env({"API_KEY": "abc123"})
    encrypted_headers = MCPSecretCipher.encrypt_headers(
        [{"key": "Authorization", "value": "Bearer token"}]
    )

    assert encrypted_env["API_KEY"].startswith("enc_v1:")
    assert encrypted_headers[0]["value"].startswith("enc_v1:")
    assert MCPSecretCipher.decrypt_env(encrypted_env) == {"API_KEY": "abc123"}
    assert MCPSecretCipher.decrypt_headers(encrypted_headers) == [
        {"key": "Authorization", "value": "Bearer token"}
    ]


def test_mcp_secret_cipher_keeps_plaintext_compatible():
    plain_env = {"BASE_URL": "http://localhost:8000"}
    plain_headers = [{"key": "Accept", "value": "application/json"}]

    assert MCPSecretCipher.decrypt_env(plain_env) == plain_env
    assert MCPSecretCipher.decrypt_headers(plain_headers) == plain_headers

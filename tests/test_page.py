from fastapi.testclient import TestClient

from app.main import create_app


def test_demo_page_assets_and_no_secret_exposure(client, settings):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert 'name="viewport"' in response.text
    assert 'role="status"' in response.text
    contents = response.text
    for path, content_type in (("/assets/demo.js", "javascript"), ("/assets/demo.css", "text/css")):
        asset = client.get(path)
        assert asset.status_code == 200
        assert content_type in asset.headers["content-type"]
        contents += asset.text
    assert settings.operator_api_key.get_secret_value() not in contents
    assert settings.signing_secret.get_secret_value() not in contents
    assert "localStorage" not in contents and "sessionStorage" not in contents
    assert client.get("/assets/.env").status_code == 404
    assert client.get("/assets/config.py").status_code == 404


def test_source_preview_is_allowlisted(client):
    assert client.get("/source").status_code == 200
    assert "text/plain" in client.get("/source/worker").headers["content-type"]
    assert "def claim(" in client.get("/source/worker").text
    for name in (".env", "config", "main", "database_url"):
        assert client.get(f"/source/{name}").status_code == 404


def test_page_available_when_demo_disabled(settings):
    with TestClient(create_app(settings.model_copy(update={"demo_mode": False}))) as client:
        assert client.get("/").status_code == 200
        assert client.post("/demo/runs", json={"scenario": "success"}).status_code == 404

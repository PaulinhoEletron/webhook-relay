"""Verify local admission limits and rejection of caller-controlled demo fields."""

import httpx

with httpx.Client(base_url="http://localhost:8000", timeout=10) as client:
    for field in ("url", "headers", "payload"):
        response = client.post("/demo/runs", json={"scenario": "success", field: {}})
        assert response.status_code == 422
    assert client.post("/demo/runs", content=b"x" * 16385).status_code == 413
    codes = []
    for number in range(6):
        response = client.post(
            "/demo/runs",
            json={"scenario": "success"},
            headers={"X-Forwarded-For": f"192.0.2.{number + 1}"},
        )
        codes.append(response.status_code)
        if response.status_code == 429:
            assert 1 <= int(response.headers["Retry-After"]) <= 60
            assert response.json()["error"]["code"] == "quota_exceeded"
    assert 429 in codes and all(code in (202, 429) for code in codes)
    print(
        "PASS: strict inputs, body limit, creation quota and untrusted forwarding; statuses:", codes
    )

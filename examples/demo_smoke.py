"""Verify independent demos through the running API and real HTTP worker/receiver."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

STATE = Path("/tmp/relay-demo-smoke.json")
SCENARIOS = ["success", "temporary_failure", "permanent_failure", "temporary_failure"]
EXPECTED = {"success": [200], "temporary_failure": [503, 503, 200], "permanent_failure": [400]}


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recheck", action="store_true")
    args = parser.parse_args()
    with httpx.Client(base_url="http://localhost:8000", timeout=5) as client:
        for _ in range(30):
            try:
                if client.get("/health/ready").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        assert client.get("/health/worker").json()["status"] == "online"
        if args.recheck:
            runs = json.loads(STATE.read_text())
        else:

            def create(scenario):
                response = client.post("/demo/runs", json={"scenario": scenario})
                assert response.status_code == 202
                return response.json()

            with ThreadPoolExecutor(max_workers=4) as pool:
                runs = list(pool.map(create, SCENARIOS))
            # Tokens stay in a private container-local file, never output or URLs.
            STATE.touch(mode=0o600, exist_ok=True)
            STATE.write_text(json.dumps(runs))
        for run, scenario in zip(runs, SCENARIOS, strict=True):
            other_token = runs[(runs.index(run) + 1) % len(runs)]["read_token"]
            assert client.get(run["status_url"]).status_code == 401
            assert (
                client.get(
                    run["status_url"],
                    headers={"Authorization": f"Bearer {other_token}"},
                ).status_code
                == 401
            )
            for _ in range(40):
                response = client.get(
                    run["status_url"], headers={"Authorization": f"Bearer {run['read_token']}"}
                )
                assert response.status_code == 200
                result = response.json()
                if result["status"] in ("delivered", "failed"):
                    break
                time.sleep(1)
            assert [attempt["http_status"] for attempt in result["attempts"]] == EXPECTED[scenario]
            assert result["status"] == (
                "failed" if scenario == "permanent_failure" else "delivered"
            )
            print(
                json.dumps(
                    {
                        "event_id": run["event_id"],
                        "scenario": scenario,
                        "status": result["status"],
                        "http_history": EXPECTED[scenario],
                    }
                )
            )


if __name__ == "__main__":
    main()

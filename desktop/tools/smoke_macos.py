"""Boot the release launcher twice against disposable, credential-free local state."""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


def main() -> None:
    binary = Path(sys.argv[1]).resolve()
    version = os.environ["ALPHA_HARNESS_VERSION"]
    wheel_dir = Path(os.environ["SMOKE_WHEEL_DIR"])
    opener = build_opener(ProxyHandler({}))

    def request(path: str, body: dict | None = None, method: str | None = None) -> bytes:
        data = json.dumps(body).encode() if body is not None else None
        req = Request(
            "http://127.0.0.1:8000" + path,
            data=data,
            method=method,
            headers={"X-Harness-Client": "1", "Content-Type": "application/json"},
        )
        with opener.open(req, timeout=2) as response:
            return response.read()

    with tempfile.TemporaryDirectory(prefix="alpha-launcher-smoke-") as temporary:
        root = Path(temporary)
        home = root / "launcher"
        home.mkdir()
        subprocess.run(["uv", "venv", "--python", "3.14", str(home / "venv-a")], check=True)
        python = home / "venv-a/bin/python"
        wheel = next(wheel_dir.glob("*.whl"))
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        assert digest == os.environ["ALPHA_HARNESS_WHEEL_SHA256"]
        requirements = root / "smoke-requirements.txt"
        requirements.write_text(wheel.as_uri() + " --hash=sha256:" + digest + "\n")
        subprocess.run(
            ["uv", "pip", "install", "--require-hashes", "--python", str(python), "-c",
             str(wheel_dir / "constraints.txt"), "-r", str(requirements)],
            check=True,
        )
        (home / "slot-a.json").write_text(json.dumps({"version": version, "served": True}))
        (home / "active-slot.txt").write_text("a")
        environment = os.environ | {
            "ALPHA_HARNESS_HOME": str(home),
            "ALPHA_HARNESS_DATA_DIR": str(root / "data"),
        }
        # Test the installed wheel and real SDK on Apple Silicon. MockTransport
        # intercepts every request; no provider or BRAIN access occurs.
        test_environment = environment.copy()
        test_environment.pop("PYTHONPATH", None)
        subprocess.run(
            [str(python), "-m", "unittest", "discover", "-s",
             str(Path(__file__).resolve().parents[2] / "backend/tests"),
             "-p", "test_generation_failures.py"],
            env=test_environment, check=True,
        )
        print("Native installed-wheel LLM fault injection and conservative accounting passed.")
        expected = None
        research_id = None
        for attempt in range(2):
            with (root / f"launcher-{attempt}.log").open("w+") as log:
                process = subprocess.Popen([str(binary)], env=environment, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 90
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError(f"Launcher exited before readiness: {process.returncode}")
                        try:
                            health = json.loads(request("/api/health"))
                            break
                        except URLError, TimeoutError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Native app did not serve within 90 seconds") from None
                            time.sleep(0.25)
                    assert health["ok"] and health["version"] == version
                    assert not health["session"]["authenticated"]
                    assert health["activeSimulations"] == 0
                    html = request("/").decode()
                    assets = re.findall(r'(?:src|href)="([^" ]+\.(?:js|css))"', html)
                    assert assets, "Wheel must contain its built frontend"
                    assert request(assets[0]), "Built frontend assets must be served"
                    history = json.loads(request("/api/campaign/experiments"))
                    preferences = json.loads(request("/api/preferences"))
                    if attempt == 0:
                        assert history == []
                        policy = json.loads(request("/api/research/options"))
                        assert policy["globalBudgets"]["live"]["quick"] == 0
                        created = json.loads(request("/api/research", {
                            "name": "Synthetic campaign restart smoke",
                            "mode": "offline", "hypothesis": "Synthetic engineering smoke only.",
                            "validationStart": "2025-01-01", "validationEnd": "2025-12-31",
                            "baselineIds": ["synthetic-baseline"],
                        }))
                        research_id = created["id"]
                        request(f"/api/research/{research_id}/control", {"action": "start"})
                        request(f"/api/research/{research_id}/control", {"action": "pause"})
                        preferences.update(defaultCores=2, updateCheckHours=0, autoUpdate=False)
                        request("/api/preferences", preferences, "PUT")
                        plan = {
                            "hypothesis": "Synthetic restart smoke test only.",
                            "candidateIds": [], "validationStart": "2025-01-01",
                            "validationEnd": "2025-12-31",
                        }
                        # Empty reports are valid task-review history. The manual freeze
                        # endpoint correctly requires at least one Vault candidate.
                        with sqlite3.connect(root / "data/harness.db") as connection:
                            saved = connection.execute(
                                "INSERT INTO campaign_experiment (created_at, plan) "
                                "VALUES (CURRENT_TIMESTAMP, ?)", (json.dumps(plan),),
                            )
                            expected = saved.lastrowid
                    else:
                        assert preferences["defaultCores"] == 2
                        assert preferences["updateCheckHours"] == 0
                        assert preferences["autoUpdate"] is False
                        assert len(history) == 1 and history[0]["id"] == expected
                        campaign = json.loads(request(f"/api/research/{research_id}"))
                        assert campaign["status"] == "PAUSED" and campaign["mode"] == "offline"
                        request(f"/api/research/{research_id}/control", {"action": "resume"})
                        deadline = time.monotonic() + 80
                        while True:
                            campaign = json.loads(request(f"/api/research/{research_id}"))
                            if campaign["status"] == "COMPLETE":
                                break
                            assert campaign["status"] == "RUNNING", campaign.get("message")
                            assert time.monotonic() < deadline, "Offline native campaign timed out"
                            time.sleep(0.25)
                        assert campaign["budgets"]["quick"]["reserved"] == 10
                        assert campaign["budgets"]["full"]["reserved"] == 2
                        assert campaign["budgets"]["requests"]["reserved"] == 2
                        assert json.loads(request(f"/api/research/{research_id}/download"))["id"] == research_id
                        assert json.loads(request("/api/health"))["activeSimulations"] == 0
                        print("Native offline campaign: 10 Quick, 2 Full, 2 synthetic requests; pause/restart/resume and export passed.")
                    request("/api/quit", {}, "POST")
                    assert process.wait(timeout=25) == 0
                except BaseException:
                    log.seek(0)
                    print(log.read()[-12000:])
                    launcher_log = home / "launcher.log"
                    if launcher_log.exists():
                        print(launcher_log.read_text()[-12000:])
                    raise
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=30)
        print("Native launcher, frontend, API, clean shutdown and restart persistence passed.")


if __name__ == "__main__":
    main()

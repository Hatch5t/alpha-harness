"""Boot the release launcher twice against disposable, credential-free local state."""

import json
import os
import re
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
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "-c",
             str(wheel_dir / "constraints.txt"), str(next(wheel_dir.glob("*.whl")))],
            check=True,
        )
        (home / "slot-a.json").write_text(json.dumps({"version": version, "served": True}))
        (home / "active-slot.txt").write_text("a")
        environment = os.environ | {
            "ALPHA_HARNESS_HOME": str(home),
            "ALPHA_HARNESS_DATA_DIR": str(root / "data"),
        }
        expected = None
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
                        preferences.update(defaultCores=2, updateCheckHours=0, autoUpdate=False)
                        request("/api/preferences", preferences, "PUT")
                        saved = json.loads(request("/api/campaign/experiments", {
                            "hypothesis": "Synthetic restart smoke test only.",
                            "candidateIds": [], "validationStart": "2025-01-01",
                            "validationEnd": "2025-12-31",
                        }))
                        expected = saved["id"]
                    else:
                        assert preferences["defaultCores"] == 2
                        assert preferences["updateCheckHours"] == 0
                        assert preferences["autoUpdate"] is False
                        assert len(history) == 1 and history[0]["id"] == expected
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

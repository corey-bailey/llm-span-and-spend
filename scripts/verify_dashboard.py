"""Run every query in the generated dashboard against the live stack. Fails on any query
error; reports which panels have data. Run: uv run python scripts/verify_dashboard.py"""
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.build_dashboard import build  # noqa: E402

GRAFANA = os.environ.get("GRAFANA_URL", "http://localhost:3001")
AUTH = ("admin", "admin")


def run(target: dict) -> tuple[bool, int, str]:
    uid, expr = target["datasource"]["uid"], target["expr"]
    now = int(time.time())
    if uid == "prometheus":
        path, params = "/api/v1/query", {"query": expr}
    else:
        path, params = "/loki/api/v1/query_range", {
            "query": expr, "start": f"{now - 86400}000000000", "end": f"{now}000000000", "limit": 5}
    r = httpx.get(f"{GRAFANA}/api/datasources/proxy/uid/{uid}{path}", params=params, auth=AUTH, timeout=30)
    body = r.json()
    if r.status_code != 200 or body.get("status") != "success":
        return False, 0, body.get("error", r.text[:200])
    return True, len(body["data"]["result"]), ""


def main() -> int:
    failures = 0
    for p in build()["panels"]:
        for t in p.get("targets", []):
            ok, n, err = run(t)
            failures += not ok
            state = "ERROR " + err if not ok else (f"{n} series" if n else "no data yet")
            print(f"[{'ok' if ok else 'FAIL'}] {p['title'][:44]:44} {t['refId']}  {state}")
    print(f"{failures} failing queries")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

"""`catalystedge verify-deployment`: checks a running CatalystEdge from the outside, over HTTPS.

Run it on the server (the password is read from APP_PASSWORD, never typed or printed):
    docker compose -f docker-compose.cloud.yml exec api python -m catalystedge verify-deployment \
        --url https://YOUR-NAME.duckdns.org --web https://YOUR-APP.vercel.app --email

Checks, in order: health + database, scheduler heartbeat, login, a full refresh, the web app's
API address, and (with --email) one real email sent by the worker.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

import httpx

OK, BAD, WAIT = "PASS", "FAIL", "WAIT"


class Checker:
    def __init__(self, url: str, password: str | None, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, out: Callable[[str], None] = print):
        self.url = url.rstrip("/")
        self.password = password
        self.http = client or httpx.Client(timeout=30.0, follow_redirects=True)
        self.sleep, self.out = sleep, out
        self.headers: dict[str, str] = {}
        self.failed: list[str] = []

    def line(self, status: str, name: str, detail: str = "") -> None:
        if status == BAD:
            self.failed.append(name)
        self.out(f"[{status}] {name}" + (f": {detail}" if detail else ""))

    def get(self, path: str) -> httpx.Response:
        return self.http.get(self.url + path, headers=self.headers)

    def health(self) -> dict | None:
        try:
            r = self.http.get(self.url + "/health")
            body = r.json()
        except Exception as e:
            self.line(BAD, "API reachable over HTTPS", f"{type(e).__name__} (is the server up and the address right?)")
            return None
        self.line(OK if r.status_code == 200 else BAD, "API reachable over HTTPS", f"HTTP {r.status_code}")
        self.line(OK if body.get("database") == "ok" else BAD, "Database (Neon)", str(body.get("database")))
        prof = body.get("profile")
        self.line(OK if prof == "cloud" else WAIT, "Cloud profile", f"profile={prof}"
                  + ("" if prof == "cloud" else " (fine for a local test; the cloud needs CATALYSTEDGE_PROFILE=cloud)"))
        return body

    def scheduler(self, body: dict) -> None:
        sch = body.get("scheduler") or {}
        st = sch.get("status")
        if st == "ok":
            self.line(OK, "Scheduler running", f"last task '{sch.get('last_task')}' {sch.get('minutes_ago')} min ago")
        elif st == "no heartbeat yet":
            self.line(WAIT, "Scheduler running", "no scheduled task has finished yet; news runs at least hourly, "
                                                  "so check again within the hour")
        else:
            self.line(BAD, "Scheduler running",
                      f"{st}; see `docker compose -f docker-compose.cloud.yml logs beat worker`")

    def login(self) -> bool:
        if not self.password:
            self.line(BAD, "Login", "APP_PASSWORD is not set here")
            return False
        r = self.http.post(self.url + "/api/login", json={"password": self.password})
        token = r.json().get("token") if r.status_code == 200 else None
        if not token:
            self.line(BAD, "Login", f"HTTP {r.status_code}: the password on this machine does not match the server")
            return False
        self.headers = {"Authorization": f"Bearer {token}"}
        unauth = self.http.get(self.url + "/api/signals").status_code
        self.line(OK, "Login", "password accepted")
        self.line(OK if unauth == 401 else BAD, "Password required", "pages refuse visitors without the password"
                  if unauth == 401 else f"HTTP {unauth} without a password: set APP_PASSWORD on the server")
        return True

    def refresh(self, timeout_s: int = 900) -> None:
        r = self.http.post(self.url + "/api/refresh?trigger=manual", headers=self.headers).json()
        rid = r.get("refresh_id")
        if not rid:
            self.line(BAD, "Refresh", "did not start")
            return
        waited = 0
        while waited <= timeout_s:
            run = (self.get("/api/refresh/latest").json() or {}).get("refresh") or {}
            if run.get("id") == rid and run.get("status") != "running":
                break
            self.sleep(10)
            waited += 10
        else:
            self.line(BAD, "Refresh", f"still running after {timeout_s // 60} min; see the worker log")
            return
        latest = self.get("/api/refresh/latest").json()
        status = latest["refresh"]["status"]
        errs = [f"{x['key']}: {x['error']}" for x in latest.get("sources", []) if x.get("status") not in ("ok", None)]
        note = "started a fresh run" if r.get("started") else "joined the refresh already running or just finished"
        self.line(OK if status == "done" else BAD, "Refresh (news, events, prices, signals, portfolio)",
                  f"{status}; {note}" + (f"; source problems: {'; '.join(errs)[:400]}" if errs else ""))

    def web(self, web_url: str) -> None:
        try:
            cfg = self.http.get(web_url.rstrip("/") + "/runtime-config").json()
        except Exception as e:
            self.line(BAD, "Web app (Vercel)", f"{type(e).__name__}: is the Vercel address right?")
            return
        api = (cfg.get("apiUrl") or "").rstrip("/")
        self.line(OK if api == self.url else BAD, "Web app points at this API",
                  api if api == self.url else f"it points at {api or 'nothing'}; set API_PUBLIC_URL={self.url} in "
                                              "Vercel and redeploy")

    def email(self, timeout_s: int = 300) -> None:
        r = self.http.post(self.url + "/api/notifications/test", headers=self.headers)
        if r.status_code != 200:
            self.line(BAD, "Test email", r.json().get("detail", f"HTTP {r.status_code}"))
            return
        nid, waited = r.json()["notification_id"], 0
        while waited <= timeout_s:
            row = next((n for n in self.get("/api/notifications").json()["log"] if n["id"] == nid), None)
            if row and row["status"] == "sent":
                self.line(OK, "Test email sent by the worker", f"via {row['provider']}; check your inbox (and spam)")
                return
            if row and row["status"] == "failed" and row["attempts"] >= 1:
                self.line(BAD, "Test email", f"provider error: {row['last_error']}")
                return
            self.sleep(10)
            waited += 10
        self.line(BAD, "Test email", "not sent within 5 min: is the worker running?")


def run(url: str, web: str | None = None, email: bool = False, password: str | None = None, **kw) -> int:
    c = Checker(url, password if password is not None else os.environ.get("APP_PASSWORD"), **kw)
    c.out(f"Checking {c.url}")
    body = c.health()
    if body is not None:
        c.scheduler(body)
        if c.login():
            c.refresh()
            if email:
                c.email()
    if web:
        c.web(web)
    c.out("All checks passed." if not c.failed else f"{len(c.failed)} check(s) failed: {', '.join(c.failed)}")
    return 0 if not c.failed else 1

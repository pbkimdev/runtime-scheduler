"""A minimal GitHub REST client on urllib. No runtime dependencies.

Every call's outcome is recorded on the client so facts.py can count read
failures per provider without threading a result object through the call
sites.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

API_VERSION = "2022-11-28"
DEFAULT_API_URL = "https://api.github.com"
USER_AGENT = "pbkimdev-runtime-scheduler/0.1"


class GitHubError(Exception):
    def __init__(self, status: int | None, url: str, body: str) -> None:
        super().__init__(f"{status} {url}: {body[:400]}")
        self.status = status
        self.url = url
        self.body = body


@dataclass
class Call:
    method: str
    path: str
    status: int | None
    ok: bool
    error: str = ""


@dataclass
class GitHubClient:
    token: str
    api_url: str = DEFAULT_API_URL
    retries: int = 3
    backoff_s: float = 1.0
    sleep: Callable[[float], None] = time.sleep
    calls: list[Call] = field(default_factory=list)

    # ---- transport -----------------------------------------------------

    def _open(
        self, request: urllib.request.Request
    ) -> tuple[int, dict[str, str], bytes]:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, dict(response.headers), response.read()

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        record: bool = True,
    ) -> tuple[int, dict[str, str], Any]:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"

        last: Exception | None = None
        for attempt in range(self.retries):
            request = urllib.request.Request(
                url, data=payload, headers=headers, method=method
            )
            try:
                status, response_headers, raw = self._open(request)
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if exc.code >= 500 and attempt < self.retries - 1:
                    last = exc
                    self.sleep(self.backoff_s * (2**attempt))
                    continue
                if record:
                    self.calls.append(
                        Call(
                            method,
                            path,
                            exc.code,
                            False,
                            raw.decode("utf-8", "replace"),
                        )
                    )
                raise GitHubError(
                    exc.code, url, raw.decode("utf-8", "replace")
                ) from exc
            except urllib.error.URLError as exc:
                last = exc
                if attempt < self.retries - 1:
                    self.sleep(self.backoff_s * (2**attempt))
                    continue
                if record:
                    self.calls.append(Call(method, path, None, False, str(exc)))
                raise GitHubError(None, url, str(exc)) from exc

            if record:
                self.calls.append(Call(method, path, status, True))
            parsed = json.loads(raw) if raw else None
            return status, response_headers, parsed

        raise GitHubError(None, url, str(last))

    def _paginate(self, path: str, key: str | None = None) -> list[Any]:
        """Follow the Link header's rel="next" until it is gone."""
        items: list[Any] = []
        next_path: str | None = path
        while next_path:
            _, headers, parsed = self._request("GET", next_path)
            if parsed is None:
                break
            page = parsed if key is None else parsed.get(key, [])
            items.extend(page)
            next_path = _next_link(headers.get("Link", ""))
        return items

    # ---- reads ---------------------------------------------------------

    def list_org_repos(self, org: str) -> list[dict[str, Any]]:
        return self._paginate(f"/orgs/{org}/repos?type=all&per_page=100")

    def list_runs(
        self, owner: str, repo: str, created_since: str
    ) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {"created": f">={created_since}", "per_page": "100"}
        )
        return self._paginate(
            f"/repos/{owner}/{repo}/actions/runs?{query}", key="workflow_runs"
        )

    def list_jobs(self, owner: str, repo: str, run_id: int) -> list[dict[str, Any]]:
        # filter=all so every re-run attempt is visible; a re-run attempt is a
        # new allocation (E15).
        return self._paginate(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100",
            key="jobs",
        )

    def list_org_runners(self, org: str) -> list[dict[str, Any]]:
        return self._paginate(
            f"/orgs/{org}/actions/runners?per_page=100", key="runners"
        )

    def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        _, _, parsed = self._request("GET", f"/repos/{owner}/{repo}")
        return parsed or {}

    def list_org_variables(self, org: str) -> dict[str, str]:
        variables = self._paginate(
            f"/orgs/{org}/actions/variables?per_page=100", key="variables"
        )
        return {entry["name"]: entry.get("value", "") for entry in variables}

    def get_org_variable(self, org: str, name: str) -> str | None:
        try:
            _, _, parsed = self._request("GET", f"/orgs/{org}/actions/variables/{name}")
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return parsed.get("value") if parsed else None

    def patch_org_variable(self, org: str, name: str, value: str) -> None:
        self._request(
            "PATCH", f"/orgs/{org}/actions/variables/{name}", {"value": value}
        )

    def org_billing_usage(self, org: str, year: int, month: int) -> dict[str, Any]:
        query = urllib.parse.urlencode({"year": year, "month": month})
        _, _, parsed = self._request(
            "GET", f"/organizations/{org}/settings/billing/usage?{query}"
        )
        return parsed or {}

    def user_billing_usage(self, user: str, year: int, month: int) -> dict[str, Any]:
        query = urllib.parse.urlencode({"year": year, "month": month})
        _, _, parsed = self._request(
            "GET", f"/users/{user}/settings/billing/usage?{query}"
        )
        return parsed or {}

    # ---- workflow dispatch, for the contract check ----------------------

    def dispatch_workflow(self, owner: str, repo: str, workflow: str, ref: str) -> None:
        self._request(
            "POST",
            f"/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches",
            {"ref": ref},
        )

    def list_workflow_runs(
        self, owner: str, repo: str, workflow: str, per_page: int = 20
    ) -> list[dict[str, Any]]:
        _, _, parsed = self._request(
            "GET",
            f"/repos/{owner}/{repo}/actions/workflows/{workflow}/runs"
            f"?per_page={per_page}",
        )
        return (parsed or {}).get("workflow_runs", [])

    def get_run(self, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        _, _, parsed = self._request(
            "GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}"
        )
        return parsed or {}

    # ---- outcome accounting --------------------------------------------

    def read_ok(self) -> bool:
        """True when no recorded read failed. Two consecutive failing ticks
        trip a provider's circuit (section 8)."""
        return all(call.ok for call in self.calls if call.method == "GET")

    def failed_calls(self) -> list[Call]:
        return [call for call in self.calls if not call.ok]

    def reset_calls(self) -> None:
        self.calls.clear()


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip().strip("<>")
        for segment in segments[1:]:
            if segment.strip() in ('rel="next"', "rel=next"):
                return url
    return None

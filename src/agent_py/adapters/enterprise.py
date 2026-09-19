"""Bounded, credential-isolated readers for the selected enterprise systems.

Writes are deliberately exposed only by the certified execution boundary, not these readers.
"""

import json
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin, urlsplit

import httpx

from agent_py.domain import DomainError
from agent_py.resilience import TransientReadError


def reject_constant(value):
    raise ValueError("Nonfinite JSON number")


class EnterpriseClient:
    def __init__(self, base_url: str, token: str, *, transport=None, username: str | None = None):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("An explicitly configured HTTPS service origin is required")
        self.base = base_url.rstrip("/") + "/"
        self.origin = (parsed.scheme, parsed.netloc)
        self.client = httpx.Client(
            timeout=httpx.Timeout(10, connect=5),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            headers={"Authorization": f"Bearer {token}"} if username is None else {},
            auth=(username, token) if username else None,
        )

    def close(self):
        self.client.close()

    def get(self, path: str, params: dict | None = None):
        url = urljoin(self.base, path)
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc) != self.origin or not parsed.path.startswith(
            urlsplit(self.base).path
        ):
            raise DomainError("EGRESS_DENIED", "Cross-origin or out-of-prefix request", 403)
        started = time.monotonic()
        try:
            with self.client.stream("GET", url, params=params) as response:
                if response.status_code == 429:
                    raise TransientReadError(
                        "UPSTREAM_RATE_LIMIT", retry_delay(response.headers.get("Retry-After"))
                    )
                if response.status_code in {500, 502, 503, 504}:
                    header = response.headers.get("Retry-After")
                    raise TransientReadError(retry_after=retry_delay(header) if header else None)
                if 300 <= response.status_code < 400:
                    raise DomainError("UPSTREAM_REDIRECT", "Redirect requires explicit policy", 502)
                if response.status_code >= 400:
                    raise DomainError(
                        "UPSTREAM_HTTP",
                        f"Enterprise reader returned HTTP {response.status_code}",
                        502,
                    )
                data = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > 20:
                        raise TransientReadError("UPSTREAM_READ_DEADLINE")
                    data.extend(chunk)
                    if len(data) > 2_000_000:
                        raise DomainError("TOOL_OUTPUT_LIMIT", "Provider output exceeds 2 MB", 502)
                try:
                    value = json.loads(data, parse_constant=reject_constant)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise DomainError(
                        "UPSTREAM_JSON", "Enterprise reader returned invalid JSON", 502
                    ) from exc
                if not isinstance(value, dict):
                    raise DomainError(
                        "UPSTREAM_JSON", "Enterprise reader requires an object response", 502
                    )
                return value
        except httpx.TransportError as exc:
            raise TransientReadError() from exc

    def bitbucket_pr(self, workspace: str, repo: str, pr: int):
        if pr < 1:
            raise ValueError("Invalid PR")
        return self.get(
            f"repositories/{quote(workspace, safe='')}/{quote(repo, safe='')}/pullrequests/{pr}"
        )

    def bitbucket_commit(self, workspace: str, repo: str, sha: str):
        return self.get(
            f"repositories/{quote(workspace, safe='')}/{quote(repo, safe='')}/commit/{quote(sha, safe='')}"
        )

    def jira_issue(self, key: str):
        return self.get(
            f"issue/{quote(key, safe='')}", {"fields": "summary,description,status,updated"}
        )

    def jenkins_build(self, job: str, build: int):
        if build < 1 or any(part in {"", ".", ".."} for part in job.split("/")):
            raise ValueError("Invalid Jenkins job or build")
        path = "/".join("job/" + quote(part, safe="") for part in job.split("/"))
        return self.get(
            f"{path}/{build}/api/json",
            {"tree": "number,result,building"},
        )

    def kubernetes_deployment(self, namespace: str, name: str):
        return self.get(
            f"apis/apps/v1/namespaces/{quote(namespace, safe='')}/deployments/{quote(name, safe='')}"
        )

    def prometheus_range(self, query: str, start: int, end: int, step: int = 30):
        if not 0 < end - start <= 3600 or step < 15 or len(query) > 2000:
            raise DomainError("QUERY_LIMIT", "Metric query outside configured limits", 422)
        return self.get(
            "api/v1/query_range", {"query": query, "start": start, "end": end, "step": step}
        )

    def loki_range(self, query: str, start: int, end: int):
        if not 0 < end - start <= 3600 or len(query) > 2000:
            raise DomainError("QUERY_LIMIT", "Log query outside configured limits", 422)
        return self.get(
            "loki/api/v1/query_range", {"query": query, "start": start, "end": end, "limit": 500}
        )


def retry_delay(header: str | None) -> float:
    """Honor seconds or HTTP-date; malformed feedback uses a conservative day cooldown."""
    if header is None:
        return 30
    try:
        if header.isascii() and header.isdigit():
            seconds = float(header)
        else:
            date = parsedate_to_datetime(header)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            seconds = max(0, (date - datetime.now(UTC)).total_seconds())
        return seconds
    except (ValueError, TypeError, OverflowError):
        return 86400

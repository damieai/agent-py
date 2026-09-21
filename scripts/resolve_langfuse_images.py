"""Explicit network command: resolve current lock tags into a review-only candidate lock."""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
from langfuse_stack import STACK

ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)
REGISTRIES = {
    "docker.io": ("registry-1.docker.io", "https://auth.docker.io/token"),
    "cgr.dev": ("cgr.dev", "https://cgr.dev/token"),
}


def resolve(client: httpx.Client, reference: str) -> dict:
    tag_reference = reference.split("@", 1)[0]
    registry, repository = tag_reference.split("/", 1)
    repository, tag = repository.rsplit(":", 1)
    host, token_url = REGISTRIES[registry]
    url = f"https://{host}/v2/{repository}/manifests/{tag}"
    headers = {"Accept": ACCEPT}
    response = client.get(url, headers=headers)
    if response.status_code == 401:
        params = dict(re.findall(r'(\w+)="([^"]+)"', response.headers["www-authenticate"]))
        if params.pop("realm") != token_url:
            raise ValueError("unexpected authentication endpoint")
        token_response = client.get(token_url, params=params)
        token_response.raise_for_status()
        payload = token_response.json()
        headers["Authorization"] = "Bearer " + (payload.get("token") or payload["access_token"])
        response = client.get(url, headers=headers)
    response.raise_for_status()
    digest = "sha256:" + hashlib.sha256(response.content).hexdigest()
    if digest != response.headers["docker-content-digest"]:
        raise ValueError("manifest digest mismatch")
    platforms = [item["platform"] for item in response.json()["manifests"]]
    if not {("linux", "amd64"), ("linux", "arm64")} <= {
        (p["os"], p["architecture"]) for p in platforms
    }:
        raise ValueError("required platforms missing")
    return {"image": tag_reference + "@" + digest, "platforms": platforms}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="new candidate file (no overwrite)"
    )
    args = parser.parse_args()
    try:
        lock = json.loads((STACK / "images.lock.json").read_text())
        with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
            images = {name: resolve(client, item["image"]) for name, item in lock["images"].items()}
        lock.update(images=images, resolved_at=datetime.now(UTC).date().isoformat())
        with args.output.open("x") as stream:
            stream.write(json.dumps(lock, indent=2) + "\n")
        print("Candidate image lock written; existing deployment unchanged")
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        parser.exit(1, "Image resolution failed; no deployment files changed.\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Check or explicitly apply this repository's public GitHub discovery metadata."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def gh(*args: str, payload=None):
    result = subprocess.run(
        ["gh", *args], cwd=ROOT, check=True, text=True, capture_output=True,
        input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Update public GitHub metadata; default is read-only")
    args = parser.parse_args()
    spec = json.loads((ROOT / ".github/discovery.json").read_text(encoding="utf-8"))
    repo, topics = spec["repository"], spec["topics"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        parser.error("Invalid repository identity")
    if (not isinstance(topics, list) or not 1 <= len(topics) <= 20 or len(set(topics)) != len(topics)
            or any(not isinstance(topic, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", topic) for topic in topics)):
        parser.error("GitHub topics must be 1–20 unique lowercase topic names")
    if not isinstance(spec["description"], str) or not 1 <= len(spec["description"]) <= 350:
        parser.error("Description must contain 1–350 characters")
    homepage = urlsplit(spec["homepage"])
    if homepage.scheme != "https" or not homepage.hostname or homepage.username or homepage.password:
        parser.error("Homepage must be a public HTTPS URL without credentials")
    # A fork must update discovery.json before applying its own public metadata.
    checkout = gh("repo", "view", "--json", "nameWithOwner")
    if checkout["nameWithOwner"].casefold() != repo.casefold():
        parser.error("This checkout and discovery.json refer to different repositories")
    current = gh("api", f"repos/{repo}")
    current_topics = gh("api", f"repos/{repo}/topics").get("names", [])
    changed = (current.get("description") != spec["description"]
               or (current.get("homepage") or "") != spec["homepage"]
               or set(current_topics) != set(topics))
    if args.apply and changed:
        gh("api", "--method", "PATCH", f"repos/{repo}", "--input", "-",
           payload={"description": spec["description"], "homepage": spec["homepage"]})
        gh("api", "--method", "PUT", f"repos/{repo}/topics", "--input", "-", payload={"names": topics})
        verified = gh("api", f"repos/{repo}")
        verified_topics = gh("api", f"repos/{repo}/topics").get("names", [])
        if (verified.get("description") != spec["description"] or verified.get("homepage") != spec["homepage"]
                or set(verified_topics) != set(topics)):
            raise SystemExit("GitHub metadata verification failed; rerun to reconcile")
    print(json.dumps({"repository": repo, "changed": changed, "applied": bool(args.apply and changed),
                      "description": spec["description"], "homepage": spec["homepage"], "topics": topics},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

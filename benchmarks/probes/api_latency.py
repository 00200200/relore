"""How long the deployed API takes, verb by verb -- and what the web UI's own buttons cost.

A person reported relore as "slow in production" and pointed at the web UI's query samples.
They were right, and no measurement this project had would have found it: section 10 scores
retrieval, `fieldrun` scores a flow, and neither times a single call against the deployment
people actually click. This does.

**The sample strip is a latency surface.** `relore/api/ui.py` ships a row of buttons that
run a query for you, which is the fastest way to learn what the tool answers well and the
first thing anyone touches. Two of its three `why` samples pointed at line 1 of a source
file, and `why`'s origin pass pickaxes words out of the line *plus the comment block above
it* -- so on line 1 that is the licence header: five `git log -S` passes on `Copyright`,
`HuggingFace`, `rights`, `reserved` and `team`, each a full walk of that file's history, for
an answer that was empty. Measured here at **3.98s and 6.35s**, against 1.77s for a sample
whose first candidate was accepted (relore#71).

So the samples are not retyped into this script: they are **read out of `ui.py`**, and a
run that cannot find them says so instead of quietly timing a stale list. A probe that
drifts from the page it is about would have missed exactly the bug it was written for.

    python benchmarks/probes/api_latency.py --base https://relore.example.org \\
        --repo huggingface/transformers

Every row is the best of N, because the question is what the call costs and not what the
network did once. The connection baseline is measured and printed separately: against a
VPN-internal deployment, DNS + TCP + TLS is ~0.3s of every cold call and belongs to the
deployment rather than to the verb.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "relore" / "api" / "ui.py"

#: `{q: "...", kind: "..."}` and `{at: "path:line", repo: "..."}` out of the two sample
#: arrays in `ui.py`. Deliberately narrow: it reads the fields a request needs and ignores
#: the labels, so a wording change to a button does not break the probe and a change to
#: what the button *asks* does show up here.
_SAMPLES = re.compile(r"const SAMPLES = \[(.*?)\n\];", re.S)
_WHY_SAMPLES = re.compile(r"const WHY_SAMPLES = \[(.*?)\n\];", re.S)
_FIELD = re.compile(r'(\w+):\s*"([^"]*)"')


def ui_samples() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """The UI's own two sample lists, as dictionaries. Raises rather than returning empty."""
    source = UI.read_text()
    out = []
    for name, pattern in (("SAMPLES", _SAMPLES), ("WHY_SAMPLES", _WHY_SAMPLES)):
        block = pattern.search(source)
        if not block:
            raise SystemExit(
                f"could not find `const {name}` in {UI}. The samples are read from the UI on "
                "purpose -- a probe that drifts from the page it is about is worse than none "
                "-- so fix this regex rather than hard-coding a list here."
            )
        rows = [dict(_FIELD.findall(entry)) for entry in block.group(1).split("},")]
        out.append([row for row in rows if row])
    return out[0], out[1]


def best(call, n: int) -> tuple[float, int, int]:
    """Best of ``n``: the question is what the call costs, not what the network did once."""
    fastest, size, status = None, 0, 0
    for _ in range(n):
        started = time.perf_counter()
        response = call()
        elapsed = time.perf_counter() - started
        fastest = elapsed if fastest is None else min(fastest, elapsed)
        size, status = len(response.content), response.status_code
    return fastest or 0.0, size, status


def connection_baseline(client: httpx.Client, base: str, headers: dict[str, str]) -> float:
    """What a cold call pays before any verb runs. Against a VPN-internal deployment this is
    most of a fast call, and attributing it to the verb makes every verb look slow."""
    times = []
    for _ in range(3):
        started = time.perf_counter()
        client.get(f"{base}/healthz", headers=headers, timeout=60)
        times.append(time.perf_counter() - started)
    return min(times)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="the deployment's base URL")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--client-version", default=None, help="override the wire version sent")
    parser.add_argument("--token", default=None)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--thread", type=int, default=None, help="a thread number to time")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT))
    from relore import __version__

    headers = {"x-relore-client": args.client_version or __version__}
    if args.token:
        headers["authorization"] = f"Bearer {args.token}"

    search_samples, why_samples = ui_samples()
    rows: list[dict[str, Any]] = []

    with httpx.Client() as client:
        baseline = connection_baseline(client, args.base, headers)

        for sample in search_samples:
            body: dict[str, Any] = {"query": sample.get("q", ""), "repos": [args.repo]}
            for key in ("kind", "trust"):
                if sample.get(key):
                    body[key] = sample[key]
            if sample.get("file"):
                body["files"] = [sample["file"]]
            seconds, size, status = best(
                lambda b=body: client.post(
                    f"{args.base}/api/v1/search", json=b, headers=headers, timeout=180
                ),
                args.repeat,
            )
            rows.append(
                {
                    "surface": "ui sample",
                    "verb": "search",
                    "what": sample.get("q", "")[:44],
                    "seconds": round(seconds, 2),
                    "bytes": size,
                    "status": status,
                }
            )

        for sample in why_samples:
            at = sample.get("at", "")
            path, _, line = at.rpartition(":")
            params = {"repo": sample.get("repo") or args.repo, "path": path, "line": line}
            seconds, size, status = best(
                lambda p=params: client.get(
                    f"{args.base}/api/v1/why", params=p, headers=headers, timeout=180
                ),
                args.repeat,
            )
            rows.append(
                {
                    "surface": "ui sample",
                    "verb": "why",
                    "what": at[-44:],
                    "seconds": round(seconds, 2),
                    "bytes": size,
                    "status": status,
                }
            )

        if args.thread:
            for label, params in (
                ("thread", {}),
                ("thread --outline", {"outline": "true"}),
                ("thread --full", {"full": "true"}),
            ):
                query = {"repo": args.repo, **params}
                seconds, size, status = best(
                    lambda q=query: client.get(
                        f"{args.base}/api/v1/thread/{args.thread}",
                        params=q,
                        headers=headers,
                        timeout=180,
                    ),
                    args.repeat,
                )
                rows.append(
                    {
                        "surface": "verb",
                        "verb": label,
                        "what": f"#{args.thread}",
                        "seconds": round(seconds, 2),
                        "bytes": size,
                        "status": status,
                    }
                )

    if args.json:
        print(json.dumps({"baseline_seconds": round(baseline, 3), "rows": rows}, indent=2))
        return 0

    print(f"connection baseline (GET /healthz, best of 3): {baseline:.2f}s")
    print("every row below includes it; what the verb itself costs is the difference\n")
    print(f"{'surface':10} {'verb':18} {'what':46} {'sec':>6} {'bytes':>7} {'st':>4}")
    print("-" * 96)
    for row in rows:
        print(
            f"{row['surface']:10} {row['verb']:18} {row['what']:46} "
            f"{row['seconds']:>6} {row['bytes']:>7} {row['status']:>4}"
        )
    slow = [r for r in rows if r["seconds"] > baseline * 3]
    if slow:
        print(
            f"\n{len(slow)} call(s) cost more than three times the connection baseline. "
            "On a page of clickable samples that is the first thing a person feels, and it "
            "is where relore#71 was reported from."
        )
        median = statistics.median(r["seconds"] for r in rows)
        for row in slow:
            print(f"  {row['verb']} {row['what']}  {row['seconds']}s  (median row {median:.2f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

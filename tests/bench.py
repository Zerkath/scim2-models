from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pyperf

from scim2_models import Context
from scim2_models import EnterpriseUser
from scim2_models import Group
from scim2_models import ListResponse
from scim2_models import User
from scim2_models.attributes import is_complex_attribute
from scim2_models.base import BaseModel as ScimBaseModel
from scim2_models.base import _exact_attr_match
from scim2_models.base import _is_attribute_requested

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = REPO_ROOT / "samples"
BUCKET_ROOT = Path("/tmp/scim2")
RESOURCE_COUNTS = [50, 100, 250, 500, 2500]


TRACKED_CACHES: list[tuple[str, Callable]] = [
    ("BaseModel.get_field_root_type", ScimBaseModel.get_field_root_type),
    ("BaseModel.get_field_multiplicity", ScimBaseModel.get_field_multiplicity),
    ("attributes.is_complex_attribute", is_complex_attribute),
    ("base._is_attribute_requested", _is_attribute_requested),
    ("base._exact_attr_match", _exact_attr_match),
]


def _cache_info_dump() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for label, fn in TRACKED_CACHES:
        info_fn = getattr(fn, "cache_info", None)
        if info_fn is None:
            out[label] = {"cached": False}
            continue
        info = info_fn()
        out[label] = {
            "cached": True,
            "hits": info.hits,
            "misses": info.misses,
            "currsize": info.currsize,
            "maxsize": info.maxsize,
            "hit_rate": info.hits / (info.hits + info.misses) if (info.hits + info.misses) else 0.0,
        }
    return out


def _print_cache_info() -> None:
    info = _cache_info_dump()
    print("cache_info:")
    for label, data in info.items():
        if not data.get("cached"):
            print(f"  {label}: <no cache_info>")
            continue
        print(
            f"  {label}: hits={data['hits']} misses={data['misses']} "
            f"hit_rate={data['hit_rate'] * 100:.1f}% "
            f"currsize={data['currsize']} maxsize={data['maxsize']}"
        )


def build_response(n_resources: int) -> ListResponse:
    user = json.loads((SAMPLES / "rfc7643-8.3-enterprise_user.json").read_text())
    group = json.loads((SAMPLES / "rfc7643-8.4-group.json").read_text())
    resources = []
    for i in range(n_resources):
        u = json.loads(json.dumps(user))
        u["id"] = f"{u['id']}-{i}"
        u["userName"] = f"user{i}@example.com"
        resources.append(u)
    resources.append(group)
    payload = {
        "totalResults": len(resources),
        "itemsPerPage": len(resources),
        "startIndex": 1,
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "Resources": resources,
    }
    return ListResponse[User[EnterpriseUser] | Group].model_validate(payload)


SCENARIOS: dict[str, dict] = {
    "no_ctx": dict(scim_ctx=None),
    "query_response": dict(scim_ctx=Context.RESOURCE_QUERY_RESPONSE),
    "query_attrs": dict(
        scim_ctx=Context.RESOURCE_QUERY_RESPONSE,
        attributes=[
            "userName",
            "emails",
            "name.familyName",
            "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User:employeeNumber",
            "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User:manager.displayName",
        ],
    ),
    "query_excl": dict(
        scim_ctx=Context.RESOURCE_QUERY_RESPONSE,
        excluded_attributes=[
            "addresses",
            "groups",
            "x509Certificates",
            "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User:manager",
        ],
    ),
    "creation_request": dict(scim_ctx=Context.RESOURCE_CREATION_REQUEST),
    "search_request": dict(scim_ctx=Context.SEARCH_REQUEST),
}


def _make_dump_fn(response: ListResponse, kwargs: dict) -> Callable[[], None]:
    def _dump() -> None:
        response.model_dump(**kwargs)

    return _dump


def _git_branch() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip() or "detached"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _git_commit() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _slug(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", branch).strip("_") or "unknown"


def bucket_dir() -> Path:
    BUCKET_ROOT.mkdir(parents=True, exist_ok=True)
    return BUCKET_ROOT


def result_path(branch: str | None = None) -> Path:
    return bucket_dir() / f"{_slug(branch or _git_branch())}.json"


def _register_worker_cache_log(branch: str, started: str) -> None:
    import atexit
    import os

    log_path = bucket_dir() / "worker_cache_log.jsonl"
    worker_id = os.getpid()

    def _dump() -> None:
        entry = {
            "branch": branch,
            "run_started": started,
            "pid": worker_id,
            "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
            "cache_info": _cache_info_dump(),
        }
        with log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    atexit.register(_dump)


def main(argv: list[str] | None = None) -> int:
    runner = pyperf.Runner()

    args = runner.parse_args(argv)

    responses = {n: build_response(n) for n in RESOURCE_COUNTS}

    benchmarks: list[pyperf.Benchmark] = []
    for n in RESOURCE_COUNTS:
        for scenario in SCENARIOS:
            bench_name = f"{scenario}_{n}"
            bench = runner.bench_func(
                bench_name, _make_dump_fn(responses[n], SCENARIOS[scenario])
            )
            if bench is not None:
                benchmarks.append(bench)

    if getattr(args, "worker", False):
        _register_worker_cache_log(_git_branch(), _dt.datetime.now().isoformat(timespec="seconds"))
        return 0

    cache_info = _cache_info_dump()
    _print_cache_info()

    branch = _git_branch()
    results = {
        b.get_name(): {"mean_ms": b.mean() * 1000, "stdev_ms": b.stdev() * 1000}
        for b in benchmarks
    }
    output = {
        "branch": branch,
        "commit": _git_commit(),
        "started": _dt.datetime.now().isoformat(timespec="seconds"),
        "resource_counts": RESOURCE_COUNTS,
        "scenarios": list(SCENARIOS),
        "cache_info": cache_info,
        "results": results,
    }
    save_target = result_path(branch)
    save_target.write_text(json.dumps(output, indent=2, sort_keys=True))
    print(f"\nresults: {save_target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

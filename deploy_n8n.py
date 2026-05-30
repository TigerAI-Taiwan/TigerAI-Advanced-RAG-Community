#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deploy_n8n.py — Auto-deploy TigerAI-RAG Community n8n workflows.

USAGE
-----
    export N8N_URL=http://localhost:5678
    export N8N_API_KEY=<your-n8n-api-key>
    python3 deploy_n8n.py
    python3 deploy_n8n.py --force          # delete + re-import existing
    python3 deploy_n8n.py --dir ./n8n/V3-WebApp-v1.0

Generate the API key in n8n: Settings -> n8n API -> Create an API key.

RATIONALE
---------
n8n assigns a fresh internal id to each workflow on import. "Execute Workflow"
nodes (n8n-nodes-base.executeWorkflow) hard-reference subflows by that id, so
on a clean install every cross-workflow reference is dangling. n8n keeps the
human-readable name in `parameters.workflowId.cachedResultName`, which we use
to rebuild the link after import:

    name (cachedResultName) --> new id  ==>  parameters.workflowId.value

Community templates additionally use the literal sentinel string
"__REWIRE_BY_NAME__" as `parameters.workflowId.value` (notably for
Sub-Chat-Cloud, which is referenced from #05-NonStream-Cloud and
#05b-Cloud-Streaming-Generic). The rewire pass resolves both shapes.

EXIT CODES
----------
    0  all green
    1  misconfiguration (missing env / bad args / no workflow dir)
    2  one or more imports failed
    3  one or more activations failed (imports were ok)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REWIRE_SENTINEL = "__REWIRE_BY_NAME__"
DEFAULT_WORKFLOW_DIR = "./n8n/V3-WebApp-v1.0"


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, body: str, url: str, method: str):
        super().__init__(f"{method} {url} -> HTTP {status}\n{body}")
        self.status = status
        self.body = body
        self.url = url
        self.method = method


def _request(
    method: str,
    base_url: str,
    path: str,
    api_key: str,
    payload: Any | None = None,
) -> Any:
    url = base_url.rstrip("/") + path
    data: bytes | None = None
    headers = {
        "X-N8N-API-KEY": api_key,
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    req = urllib.request.Request(url=url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            if not raw:
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise ApiError(e.code, body, url, method) from None
    except urllib.error.URLError as e:
        raise ApiError(0, f"URLError: {e.reason}", url, method) from None


def api_get(base_url: str, path: str, api_key: str) -> Any:
    return _request("GET", base_url, path, api_key)


def api_post(base_url: str, path: str, api_key: str, payload: Any | None = None) -> Any:
    return _request("POST", base_url, path, api_key, payload)


def api_put(base_url: str, path: str, api_key: str, payload: Any) -> Any:
    return _request("PUT", base_url, path, api_key, payload)


def api_delete(base_url: str, path: str, api_key: str) -> Any:
    return _request("DELETE", base_url, path, api_key)


# ---------------------------------------------------------------------------
# n8n operations
# ---------------------------------------------------------------------------

def list_workflows(base_url: str, api_key: str) -> list[dict]:
    """List all workflows, following nextCursor pagination if present."""
    out: list[dict] = []
    cursor: str | None = None
    while True:
        path = "/api/v1/workflows?limit=250"
        if cursor:
            path += f"&cursor={urllib.parse.quote(cursor)}"
        resp = api_get(base_url, path, api_key)
        if isinstance(resp, dict):
            out.extend(resp.get("data") or [])
            cursor = resp.get("nextCursor")
            if not cursor:
                break
        elif isinstance(resp, list):
            out.extend(resp)
            break
        else:
            break
    return out


def get_workflow(base_url: str, api_key: str, wf_id: str) -> dict:
    return api_get(base_url, f"/api/v1/workflows/{wf_id}", api_key)


def delete_workflow(base_url: str, api_key: str, wf_id: str) -> None:
    api_delete(base_url, f"/api/v1/workflows/{wf_id}", api_key)


def create_workflow(base_url: str, api_key: str, body: dict) -> dict:
    # n8n POST /workflows rejects unknown / read-only fields (id, active,
    # tags, versionId, createdAt, updatedAt, etc.). Strip to the allowed set.
    allowed = {"name", "nodes", "connections", "settings", "staticData"}
    payload = {k: v for k, v in body.items() if k in allowed}
    if "settings" not in payload:
        payload["settings"] = {}
    return api_post(base_url, "/api/v1/workflows", api_key, payload)


def update_workflow(base_url: str, api_key: str, wf_id: str, body: dict) -> dict:
    # PUT requires the same restricted shape as POST.
    allowed = {"name", "nodes", "connections", "settings", "staticData"}
    payload = {k: v for k, v in body.items() if k in allowed}
    if "settings" not in payload:
        payload["settings"] = {}
    return api_put(base_url, f"/api/v1/workflows/{wf_id}", api_key, payload)


def activate_workflow(base_url: str, api_key: str, wf_id: str) -> dict:
    # Current n8n public API: POST /api/v1/workflows/{id}/activate
    # (see https://docs.n8n.io/api/api-reference/ — workflows/activate).
    return api_post(base_url, f"/api/v1/workflows/{wf_id}/activate", api_key)


# ---------------------------------------------------------------------------
# Rewire logic
# ---------------------------------------------------------------------------

def rewire_workflow_nodes(workflow: dict, name_to_id: dict[str, str]) -> tuple[dict, int]:
    """Walk nodes; fix Execute Workflow references. Returns (workflow, fix_count)."""
    fixes = 0
    nodes = workflow.get("nodes") or []
    for node in nodes:
        if node.get("type") != "n8n-nodes-base.executeWorkflow":
            continue
        params = node.get("parameters") or {}
        wf_ref = params.get("workflowId")
        if not isinstance(wf_ref, dict):
            continue
        cached_name = wf_ref.get("cachedResultName")
        current_value = wf_ref.get("value")

        target_id: str | None = None
        # Case A: explicit sentinel — resolve by cachedResultName.
        if current_value == REWIRE_SENTINEL and cached_name in name_to_id:
            target_id = name_to_id[cached_name]
        # Case B: stale id — if the cachedResultName matches one of our
        # newly-mapped workflows and the value differs, rewire.
        elif cached_name and cached_name in name_to_id:
            new_id = name_to_id[cached_name]
            if current_value != new_id:
                target_id = new_id

        if target_id is not None:
            wf_ref["value"] = target_id
            # mode "list" is what n8n uses for resourceLocator selections.
            wf_ref.setdefault("mode", "list")
            fixes += 1
    return workflow, fixes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_workflow_files(workflow_dir: Path) -> list[Path]:
    if not workflow_dir.is_dir():
        return []
    return sorted(p for p in workflow_dir.iterdir() if p.suffix.lower() == ".json")


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def fmt_row(name: str, wf_id: str, status: str, width_name: int) -> str:
    return f"  {name.ljust(width_name)}  {wf_id:<24}  {status}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy TigerAI-RAG Community n8n workflows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dir", default=DEFAULT_WORKFLOW_DIR,
        help=f"Directory of *.json workflows (default: {DEFAULT_WORKFLOW_DIR})",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="If workflow with same name exists, delete then re-import.",
    )
    parser.add_argument(
        "--no-activate", action="store_true",
        help="Skip the activation step.",
    )
    args = parser.parse_args()

    # 1. Config
    base_url = os.environ.get("N8N_URL", "").strip()
    api_key = os.environ.get("N8N_API_KEY", "").strip()
    missing = [k for k, v in (("N8N_URL", base_url), ("N8N_API_KEY", api_key)) if not v]
    if missing:
        sys.stderr.write(
            "ERROR: missing required environment variable(s): "
            + ", ".join(missing) + "\n\n"
            "Set them and retry:\n"
            "  export N8N_URL=http://localhost:5678\n"
            "  export N8N_API_KEY=<api key from n8n Settings -> n8n API>\n"
        )
        return 1

    workflow_dir = Path(args.dir).resolve()
    files = load_workflow_files(workflow_dir)
    if not files:
        sys.stderr.write(f"ERROR: no *.json workflows found in {workflow_dir}\n")
        return 1

    print(f"n8n endpoint : {base_url}")
    print(f"workflow dir : {workflow_dir}")
    print(f"workflows    : {len(files)}")
    print(f"force        : {args.force}")
    print()

    # 2. Existing
    try:
        existing = list_workflows(base_url, api_key)
    except ApiError as e:
        sys.stderr.write(f"ERROR: failed to list existing workflows.\n{e}\n")
        return 1
    name_to_existing: dict[str, dict] = {}
    for wf in existing:
        n = wf.get("name")
        if n:
            name_to_existing[n] = wf
    print(f"Existing workflows on server: {len(name_to_existing)}")

    # 3. Per-file action
    # results: name -> {id, status, error}
    results: dict[str, dict[str, Any]] = {}
    name_to_id: dict[str, str] = {n: wf["id"] for n, wf in name_to_existing.items()}
    imported_ids: list[str] = []  # ids we just imported (candidates for rewire+activate)
    import_failures = 0

    for path in files:
        try:
            body = read_json(path)
        except Exception as e:
            print(f"  [READ-FAIL] {path.name}: {e}")
            results[path.name] = {"id": "-", "status": "failed", "error": f"read: {e}"}
            import_failures += 1
            continue

        wf_name = body.get("name") or path.stem
        body["name"] = wf_name

        existing_wf = name_to_existing.get(wf_name)
        if existing_wf and not args.force:
            wf_id = existing_wf["id"]
            name_to_id[wf_name] = wf_id
            results[wf_name] = {"id": wf_id, "status": "skipped", "error": ""}
            imported_ids.append(wf_id)  # still rewire it
            print(f"  [SKIP]   {wf_name}  (exists id={wf_id})")
            continue

        if existing_wf and args.force:
            try:
                # must deactivate before delete? n8n typically allows delete
                # regardless; if your version refuses, try deactivate first.
                delete_workflow(base_url, api_key, existing_wf["id"])
                print(f"  [DELETE] {wf_name}  (was id={existing_wf['id']})")
            except ApiError as e:
                print(f"  [DELETE-FAIL] {wf_name}: HTTP {e.status} {e.body[:200]}")
                results[wf_name] = {"id": "-", "status": "failed", "error": f"delete: {e.status}"}
                import_failures += 1
                continue

        try:
            created = create_workflow(base_url, api_key, body)
            wf_id = created.get("id") if isinstance(created, dict) else None
            if not wf_id:
                raise ApiError(0, f"create returned no id: {created!r}",
                               base_url + "/api/v1/workflows", "POST")
            name_to_id[wf_name] = wf_id
            imported_ids.append(wf_id)
            results[wf_name] = {"id": wf_id, "status": "created", "error": ""}
            print(f"  [CREATE] {wf_name}  -> id={wf_id}")
        except ApiError as e:
            print(f"  [CREATE-FAIL] {wf_name}: HTTP {e.status}\n{e.body[:500]}")
            results[wf_name] = {"id": "-", "status": "failed", "error": f"create: {e.status}"}
            import_failures += 1

    print()
    print(f"Final name->id map has {len(name_to_id)} entries.")
    print()

    # 5. Rewire pass
    print("Rewire pass (Execute Workflow references) ...")
    rewire_failures = 0
    for wf_id in imported_ids:
        try:
            current = get_workflow(base_url, api_key, wf_id)
        except ApiError as e:
            print(f"  [REWIRE-FETCH-FAIL] id={wf_id}: HTTP {e.status}")
            rewire_failures += 1
            continue
        wf_name = current.get("name", "?")
        updated, fixes = rewire_workflow_nodes(current, name_to_id)
        if fixes == 0:
            continue
        try:
            update_workflow(base_url, api_key, wf_id, updated)
            print(f"  [REWIRE] {wf_name}  ({fixes} ref(s) fixed)")
        except ApiError as e:
            print(f"  [REWIRE-FAIL] {wf_name}: HTTP {e.status}\n{e.body[:300]}")
            rewire_failures += 1
    print()

    # 6. Activation pass
    activation_failures = 0
    if args.no_activate:
        print("Activation skipped (--no-activate).")
    else:
        print("Activation pass ...")
        for wf_name, info in results.items():
            if info["status"] not in ("created", "skipped"):
                continue
            wf_id = info["id"]
            try:
                activate_workflow(base_url, api_key, wf_id)
                # don't overwrite 'created' label; append activation flag
                info["status"] = (
                    "created+activated" if info["status"] == "created"
                    else "skipped+activated"
                )
                print(f"  [ACTIVE] {wf_name}  (id={wf_id})")
            except ApiError as e:
                # Already-active workflows return 400 on some n8n versions;
                # treat the explicit "already active" wording as success.
                if "already active" in (e.body or "").lower():
                    info["status"] = (
                        "created+activated" if info["status"] == "created"
                        else "skipped+activated"
                    )
                    print(f"  [ACTIVE] {wf_name}  (already active)")
                    continue
                print(f"  [ACTIVE-FAIL] {wf_name}: HTTP {e.status}\n{e.body[:300]}")
                info["status"] = info["status"] + "+activate-failed"
                info["error"] = f"activate: {e.status}"
                activation_failures += 1
    print()

    # 7. Summary
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    if results:
        width = max(len(n) for n in results)
    else:
        width = 20
    print(fmt_row("name", "id", "status", width))
    print(fmt_row("-" * 4, "-" * 2, "-" * 6, width))
    for name in sorted(results):
        info = results[name]
        print(fmt_row(name, str(info["id"]), info["status"], width))
    print()
    print(
        f"created={sum(1 for v in results.values() if v['status'].startswith('created'))}  "
        f"skipped={sum(1 for v in results.values() if v['status'].startswith('skipped'))}  "
        f"failed={import_failures}  "
        f"rewire-failed={rewire_failures}  "
        f"activation-failed={activation_failures}"
    )

    if import_failures > 0:
        return 2
    if activation_failures > 0 or rewire_failures > 0:
        return 3
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        raise SystemExit(130)

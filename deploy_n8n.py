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


def ensure_n8n_rag_dir(verbose: bool = True) -> bool:
    """
    Ensure RAG/ directory exists at n8n container's mount path.

    The bundled workflows read `/home/node/.n8n-files/RAG/<project_id>/...`.
    If n8n's mount doesn't have a `RAG/` entry, every training silently
    "succeeds with 0 files". This function:
      1. Auto-detects the running n8n container (tries common names)
      2. docker-execs `ls` to check if RAG/ exists in its mount
      3. If missing, creates `ln -s . /home/node/.n8n-files/RAG`
      4. Verifies + reports cross-check with our splitter's /srv view

    Returns True if RAG/ is usable; False if any step failed.
    Soft-fails: prints clear remedy on failure but doesn't abort the deploy.
    """
    import subprocess

    def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)

    # 0. docker CLI available?
    r = run(["docker", "version", "--format", "{{.Server.Version}}"])
    if r.returncode != 0:
        if verbose:
            print("[rag-check] docker CLI not available; skipping n8n RAG/ check.")
            print("           If n8n's mount doesn't have RAG/ subdir, manually run:")
            print("             docker exec <n8n> sh -c 'ln -s . /home/node/.n8n-files/RAG'")
        return False

    # 1. Find running n8n container
    candidates = [
        "n8n", "n8n-main", "n8n-worker",
        "opengenie-n8n", "ai-customer-service-n8n",
    ]
    n8n_container = None
    for name in candidates:
        r = run(["docker", "inspect", "-f", "{{.State.Running}}", name])
        if r.returncode == 0 and r.stdout.strip() == "true":
            n8n_container = name
            break
    if not n8n_container:
        # Fall back: scan all running containers for /home/node/.n8n-files mount
        r = run(["docker", "ps", "--format", "{{.Names}}"])
        for nm in (r.stdout or "").splitlines():
            nm = nm.strip()
            if not nm: continue
            r2 = run(["docker", "inspect", "-f",
                      "{{range .Mounts}}{{.Destination}} {{end}}", nm])
            if "/home/node/.n8n-files" in (r2.stdout or ""):
                n8n_container = nm
                break

    if not n8n_container:
        if verbose:
            print("[rag-check] Could not find n8n container.")
            print("           Tried common names + scanned all running containers for /home/node/.n8n-files mount.")
            print("           If you know your n8n container name, run:")
            print("             docker exec <YOUR_N8N> sh -c '[ ! -e /home/node/.n8n-files/RAG ] && ln -s . /home/node/.n8n-files/RAG'")
        return False
    if verbose:
        print(f"[rag-check] Found n8n container: {n8n_container}")

    # 2. Check if RAG/ exists at n8n's mount
    r = run(["docker", "exec", n8n_container, "test", "-e", "/home/node/.n8n-files/RAG"])
    if r.returncode == 0:
        if verbose:
            print(f"[rag-check] OK: /home/node/.n8n-files/RAG already exists in {n8n_container}")
    else:
        if verbose:
            print(f"[rag-check] RAG/ missing in {n8n_container}; creating real directory (preferred) ...")
        # v1.0.9: 優先建真目錄(乾淨、無 symlink 教育成本)
        r = run(["docker", "exec", n8n_container,
                 "mkdir", "-p", "/home/node/.n8n-files/RAG"])
        if r.returncode == 0:
            if verbose:
                print(f"[rag-check] OK: created /home/node/.n8n-files/RAG/ (real directory)")
        else:
            # mkdir 失敗(e.g. read-only mount)→ fallback symlink
            if verbose:
                print(f"[rag-check] mkdir failed ({r.stderr.strip()}); trying symlink fallback ...")
            r2 = run(["docker", "exec", n8n_container, "sh", "-c",
                      "[ ! -e /home/node/.n8n-files/RAG ] && ln -s . /home/node/.n8n-files/RAG && echo CREATED"])
            if r2.returncode != 0 or "CREATED" not in (r2.stdout or ""):
                if verbose:
                    print(f"[rag-check] FAIL: could not create RAG/ in {n8n_container}")
                    print(f"           stderr (mkdir):  {r.stderr.strip()}")
                    print(f"           stderr (symlink): {r2.stderr.strip()}")
                    print(f"           Likely cause: n8n mount is read-only, or different user/permissions.")
                    print(f"           Try as root: docker exec -u root {n8n_container} mkdir -p /home/node/.n8n-files/RAG")
                return False
            if verbose:
                print(f"[rag-check] OK: created /home/node/.n8n-files/RAG -> . symlink (fallback)")

    # 4. Cross-check: n8n's view of RAG/ vs our splitter's view of /srv/
    #    If they show different content, n8n and splitter mount DIFFERENT host paths,
    #    which symlink alone won't fix — print loud warning.
    r_n8n = run(["docker", "exec", n8n_container, "ls", "/home/node/.n8n-files/RAG/"])
    r_split = run(["docker", "exec", "tigerai-splitter", "ls", "/srv/"])
    if r_n8n.returncode == 0 and r_split.returncode == 0:
        n8n_entries = set((r_n8n.stdout or "").split())
        split_entries = set((r_split.stdout or "").split())
        if n8n_entries != split_entries and verbose:
            only_n8n = n8n_entries - split_entries
            only_split = split_entries - n8n_entries
            if only_n8n or only_split:
                print("[rag-check] WARNING: n8n's RAG/ and splitter's /srv/ show DIFFERENT content:")
                if only_n8n: print(f"           only in n8n:      {sorted(only_n8n)[:5]}")
                if only_split: print(f"           only in splitter: {sorted(only_split)[:5]}")
                print("           This means n8n and splitter mount DIFFERENT host volumes.")
                print("           Symlink alone CANNOT fix this. You must align the mounts:")
                print(f"           - n8n container ({n8n_container}) must mount the same host path")
                print(f"             as your tigerai-splitter / FileBrowser (or n8n's parent + RAG/ subdir).")
                print("           Check both compose files for the volume mounts.")
                return False
        elif verbose:
            print(f"[rag-check] OK: n8n + splitter see same content ({len(n8n_entries)} entries)")

    return True


def detect_opengenie_services(backend_url: str, verbose: bool = True) -> dict:
    """
    Auto-detect OpenGenie service containers and write discovered URLs into
    backend settings. Solves the "AI installer names containers differently
    than our defaults → workflows silently call wrong URLs" failure mode.

    Returns dict of detected services: {service_name: {container, url, healthy}}.
    """
    import subprocess
    import socket
    import re

    # service_name → (regex_pattern, default_port, settings_key)
    SVC = [
        ('docling',     r'docling(-intel|-serve|-cpu|-gpu)?',           5001,  'docling_url'),
        ('qdrant',      r'qdrant(?!.*pgadmin)(?!.*-ui)',                6333,  'qdrant_url'),
        ('openwebui',   r'(open[-_]?webui|owui)',                        8080,  'owui_base_url'),
        ('filebrowser', r'filebrowser',                                  80,    None),  # FB_HOST/PORT, not single URL
        ('ollama',      r'^ollama$',                                     11434, 'ollama_url'),
        ('postgres',    r'(^postgres|^pg-)(?!.*admin)',                  5432,  None),  # PG_HOST/PORT, not single URL
    ]

    def run(cmd):
        return subprocess.run(cmd, capture_output=True, text=True)

    if run(['docker', 'version', '--format', '{{.Server.Version}}']).returncode != 0:
        if verbose: print("[svc-detect] docker CLI not available; skipping.")
        return {}

    # Get all running container names
    r = run(['docker', 'ps', '--format', '{{.Names}}'])
    names = [n.strip() for n in (r.stdout or '').split() if n.strip()]

    detected: dict = {}
    for svc, pattern, port, key in SVC:
        matches = [n for n in names if re.search(pattern, n, re.I)]
        if not matches:
            detected[svc] = None
            if verbose: print(f"[svc-detect] {svc:12s} → NOT FOUND (matched 0 of {len(names)} containers against /{pattern}/)")
            continue
        # Prefer 'main' over 'worker' if multiple matches
        best = sorted(matches, key=lambda n: (0 if 'main' in n.lower() else 1, n))[0]
        url = f'http://{best}:{port}'
        # TCP health check (via splitter container which shares network with services)
        # Just verify the container is up (already true since it's in docker ps)
        detected[svc] = {'container': best, 'url': url, 'port': port, 'settings_key': key}
        if verbose: print(f"[svc-detect] {svc:12s} → {url:50s} (container: {best})")

    # Build settings update payload
    settings_update = {}
    for svc, info in detected.items():
        if info is None: continue
        key = info['settings_key']
        if key:
            settings_update[key] = info['url']
        # Special handling for filebrowser (HOST + PORT split)
        if svc == 'filebrowser':
            settings_update['fb_host'] = f"{info['container']}:{info['port']}"
        # Special handling for postgres (HOST + PORT split — backend reads PG_HOST/PG_PORT from env, not settings)
        # Skip — backend's PG config comes from env vars, not /settings API

    if not settings_update:
        if verbose: print("[svc-detect] No service URLs to update.")
        return detected

    # POST to backend /settings
    if verbose:
        print()
        print(f"[svc-detect] Updating backend settings via {backend_url}/settings:")
        for k, v in settings_update.items():
            print(f"             {k:20s} = {v}")

    try:
        # backend POST /settings expects {key: value, ...} (per backend route)
        req = urllib.request.Request(
            f"{backend_url.rstrip('/')}/settings",
            data=json.dumps(settings_update).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8')
            if verbose: print(f"[svc-detect] OK: backend accepted settings update (HTTP {resp.status})")
    except Exception as e:
        if verbose:
            print(f"[svc-detect] WARN: could not POST to backend /settings: {e}")
            print(f"             Manual fix: open Tab 07 System Settings, set URLs to the detected values above.")

    return detected


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
    parser.add_argument(
        "--skip-rag-check", action="store_true",
        help="Skip the n8n RAG/ directory pre-flight check (the auto-create symlink in n8n container).",
    )
    parser.add_argument(
        "--skip-svc-detect", action="store_true",
        help="Skip OpenGenie services auto-detection + backend settings update.",
    )
    parser.add_argument(
        "--skip-cred-rewire", action="store_true",
        help="Skip credential rewire pass (rebinding workflow OpenAI/etc creds to your n8n credentials by name match).",
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="Pre-flight check mode: run all detections (RAG dir, OpenGenie services, n8n credentials, workflow preview), print status, then EXIT WITHOUT IMPORTING anything. Use this FIRST to verify the environment is ready, then re-run without --check-only to actually deploy.",
    )
    parser.add_argument(
        "--backend-url", default=os.environ.get("BACKEND_URL", ""),
        help="Backend URL for settings update (default: auto-probe localhost:8888 then :8088, or set $BACKEND_URL).",
    )
    args = parser.parse_args()

    # v1.0.5: pre-flight — ensure RAG/ exists at n8n's mount (fixes the
    # "PDF→MD silently succeeds 0 files" failure mode for installs where
    # n8n's mount doesn't have a RAG/ subdir).
    if not args.skip_rag_check:
        print("=" * 60)
        print("Pre-flight: n8n RAG/ directory check")
        print("=" * 60)
        ensure_n8n_rag_dir(verbose=True)
        print()

    # v1.0.6: pre-flight — auto-detect OpenGenie services + update backend
    # settings with discovered URLs (fixes the "AI installer names container
    # differently than our defaults → workflows silently call wrong URL")
    if not args.skip_svc_detect:
        print("=" * 60)
        print("Pre-flight: OpenGenie services auto-detection")
        print("=" * 60)
        backend_url = args.backend_url
        if not backend_url:
            # Auto-probe localhost:8888 then :8088 (v1.0.4 default port change)
            for port in (8888, 8088):
                try:
                    with urllib.request.urlopen(f'http://localhost:{port}/backend/health', timeout=2) as resp:
                        if resp.status == 200:
                            backend_url = f'http://localhost:{port}/backend'
                            print(f"[svc-detect] backend auto-detected at localhost:{port}")
                            break
                except Exception:
                    pass
        if not backend_url:
            print("[svc-detect] backend URL not found at localhost:8888 or :8088")
            print("           Set --backend-url=http://<host>:<port>/backend or $BACKEND_URL to update settings")
            print("           Service detection will still run + print, but won't update backend.")
        detect_opengenie_services(backend_url or 'http://localhost:8888/backend', verbose=True)
        print()

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
    if args.check_only:
        print(f"MODE         : CHECK-ONLY (no import/activate)")
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

    # v1.0.8: pre-flight — verify n8n has credentials for workflow types
    # Workflows reference openAiApi (5 workflows). If user hasn't created one
    # in n8n UI yet, --force install will succeed import but cred rewire will
    # FAIL and activation will report missing credentials at runtime.
    print()
    print("=" * 60)
    print("Pre-flight: n8n credentials readiness")
    print("=" * 60)
    cred_check_ok = True
    try:
        cred_list_raw = api_get(base_url, "/credentials", api_key)
        cred_list = cred_list_raw.get("data", cred_list_raw) if isinstance(cred_list_raw, dict) else cred_list_raw
        if not isinstance(cred_list, list): cred_list = []
        types_present = sorted({c.get("type") for c in cred_list if c.get("type")})
        print(f"[cred-check] you have {len(cred_list)} credential(s):")
        for t in types_present:
            count = sum(1 for c in cred_list if c.get("type") == t)
            names = [c.get("name", "?") for c in cred_list if c.get("type") == t]
            print(f"             - {t} x{count}: {names}")
        # Workflows shipped here need at least openAiApi
        REQUIRED_TYPES = {"openAiApi"}
        missing = REQUIRED_TYPES - set(types_present)
        if missing:
            cred_check_ok = False
            print(f"[cred-check] MISSING required credential type(s): {sorted(missing)}")
            print(f"             Workflows referencing these will fail at activation.")
            print(f"             FIX: open n8n UI → Settings → Credentials → New → pick 'OpenAI'")
            print(f"                  paste your API key → Save (any name works)")
            print(f"             Then re-run this script.")
        else:
            print(f"[cred-check] OK: all required credential types present")
    except ApiError as e:
        print(f"[cred-check] WARN: could not list credentials ({e.status}); your n8n API key may lack credentials:read scope.")
        print(f"             Continuing, but credential rewire phase will also be skipped.")
        cred_check_ok = None  # unknown
    print()

    # v1.0.8: if check-only mode, stop here (after all pre-flights)
    if args.check_only:
        print("=" * 60)
        print("CHECK-ONLY SUMMARY")
        print("=" * 60)
        print(f"  workflows that would import: {sum(1 for f in files if read_json(f).get('name') not in name_to_existing)} new, {sum(1 for f in files if read_json(f).get('name') in name_to_existing)} existing")
        print(f"  credentials ready: {'YES' if cred_check_ok else 'NO — fix before --force install' if cred_check_ok is False else 'UNKNOWN'}")
        print(f"  n8n container ensure RAG/: see [rag-check] section above")
        print(f"  OpenGenie services detected: see [svc-detect] section above")
        print()
        print("If everything above shows OK, re-run WITHOUT --check-only to actually deploy:")
        print(f"  python3 {Path(__file__).name} --force")
        return 0 if cred_check_ok is not False else 1

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

    # 5b. Credential rewire pass (v1.0.7)
    #     Workflows ship with hardcoded credential IDs from dev env
    #     (e.g. openAiApi id 'j9NcDQ1CpW9X6v2Z' name 'OpenAiaccount'). Fresh
    #     install's auto-generated cred IDs don't match → workflows reference
    #     non-existent creds → activation fails or runtime errors.
    #     Strategy: GET /api/v1/credentials, group by type, rewire by name match
    #     OR single-cred-of-type. Soft-fail if API doesn't expose cred list.
    if not args.skip_cred_rewire:
        print("Credential rewire pass (match by type + name) ...")
        try:
            cred_list_raw = api_get(base_url, "/credentials", api_key)
            # n8n returns either a list or {data: [...]}
            cred_list = cred_list_raw.get("data", cred_list_raw) if isinstance(cred_list_raw, dict) else cred_list_raw
            if not isinstance(cred_list, list):
                cred_list = []
            creds_by_type: dict[str, list[dict]] = {}
            for c in cred_list:
                creds_by_type.setdefault(c.get("type", "?"), []).append(c)
            print(f"  user has {len(cred_list)} credential(s) across {len(creds_by_type)} type(s)")

            cred_rewires = 0
            cred_warnings = 0
            for wf_id in imported_ids:
                try:
                    wf = get_workflow(base_url, api_key, wf_id)
                except ApiError:
                    continue
                wf_name = wf.get("name", "?")
                changed = False
                for node in wf.get("nodes", []) or []:
                    creds = node.get("credentials") or {}
                    for cred_type, cred_ref in list(creds.items()):
                        if not isinstance(cred_ref, dict):
                            continue
                        old_id = cred_ref.get("id")
                        old_name = cred_ref.get("name", "")
                        user_creds = creds_by_type.get(cred_type, [])
                        user_ids = {c.get("id") for c in user_creds}
                        if old_id in user_ids:
                            continue  # already valid

                        # Pick best target: name match → single cred → first
                        target = None
                        if old_name:
                            for c in user_creds:
                                if c.get("name") == old_name:
                                    target = c
                                    break
                        if target is None and len(user_creds) == 1:
                            target = user_creds[0]
                        if target is None and user_creds:
                            target = user_creds[0]
                            print(f"  [WARN] {wf_name} node '{node.get('name')}' needs '{cred_type}' but multiple creds exist and name '{old_name}' not found; using first '{target.get('name')}'")
                            cred_warnings += 1

                        if target:
                            cred_ref["id"] = target["id"]
                            cred_ref["name"] = target.get("name", cred_ref.get("name", ""))
                            changed = True
                            print(f"  [REWIRE-CRED] {wf_name} node '{node.get('name')}' {cred_type} → '{target.get('name')}' (id {str(target['id'])[:8]}...)")
                            cred_rewires += 1
                        else:
                            print(f"  [FAIL] {wf_name} node '{node.get('name')}' needs '{cred_type}' but you have NONE of that type; create in n8n UI then re-run --force")
                            cred_warnings += 1

                if changed:
                    try:
                        update_workflow(base_url, api_key, wf_id, wf)
                    except ApiError as e:
                        print(f"  [CRED-UPDATE-FAIL] {wf_name}: HTTP {e.status}\n{e.body[:200]}")

            print(f"  cred rewires: {cred_rewires}, warnings: {cred_warnings}")
        except ApiError as e:
            print(f"  [SKIP] could not list credentials ({e.status}): {e.body[:200]}")
            print(f"         your n8n API key may lack credentials:read scope.")
            print(f"         Manual fix: open each affected workflow in n8n UI, re-bind credentials.")
        except Exception as e:
            print(f"  [SKIP] unexpected error: {e}")
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

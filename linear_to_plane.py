#!/usr/bin/env python3
"""Import a Linear CSV export into Plane via the REST API.

Tailored for Linear's CSV columns such as:
ID, Project, Title, Description, Status, Priority, Assignee, Labels,
Started, Completed, Canceled, Parent issue, Related to, Blocked by, UUID.

Safe by default: without --apply it only inspects Plane and prints the plan.

Environment variables:
  PLANE_BASE_URL   e.g. https://plane.example.com
  PLANE_WORKSPACE  workspace slug from the Plane URL
  PLANE_API_KEY    Personal Access Token / API key
  PLANE_VERIFY_SSL optional: false/0/no to disable TLS verification
  PLANE_REQUESTS_PER_MINUTE optional: client-side API throttle (default 50)

Examples:
  python3 linear_to_plane.py linear-export.csv
  python3 linear_to_plane.py linear-export.csv --apply
  python3 linear_to_plane.py linear-export.csv --apply --create-projects
"""

from __future__ import annotations


import argparse
import csv
import html
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from typing import Any

try:
    import requests
    from dotenv import load_dotenv
except ImportError:
    sys.exit("Falta 'requests' y/o 'python-dotenv'. Instálalo con: python3 -m pip install requests python-dotenv")

load_dotenv()

STATE_GROUPS = {
    "backlog": "backlog",
    "todo": "unstarted",
    "in progress": "started",
    "in review": "started",
    "done": "completed",
    "canceled": "cancelled",
    "cancelled": "cancelled",
}

STATE_COLORS = {
    "backlog": "#A3A3A3",
    "unstarted": "#6B7280",
    "started": "#3B82F6",
    "completed": "#22C55E",
    "cancelled": "#EF4444",
    "triage": "#F59E0B",
}

PRIORITIES = {
    "urgent": "urgent",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "no priority": "none",
    "none": "none",
    "": "none",
}

LABEL_COLORS = [
    "#7C3AED", "#2563EB", "#0891B2", "#059669", "#65A30D",
    "#CA8A04", "#EA580C", "#DC2626", "#DB2777", "#9333EA",
]


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", clean(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", value).strip().casefold()


def first_date(value: str) -> str | None:
    value = clean(value)
    if re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return None


def split_csv_list(value: str) -> list[str]:
    return [x.strip() for x in clean(value).split(",") if x.strip()]


def description_html(row: dict[str, str]) -> str:
    raw = clean(row.get("Description"))
    if raw:
        body = "<p>" + html.escape(raw).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>") + "</p>"
    else:
        body = ""

    metadata: list[tuple[str, str]] = []
    for label, key in [
        ("Linear ID", "ID"),
        ("Linear UUID", "UUID"),
        ("Created in Linear", "Created"),
        ("Updated in Linear", "Updated"),
        ("Started in Linear", "Started"),
        ("Completed in Linear", "Completed"),
        ("Canceled in Linear", "Canceled"),
        ("Linear estimate", "Estimate"),
        ("Related to", "Related to"),
        ("Blocked by", "Blocked by"),
        ("Duplicate of", "Duplicate of"),
    ]:
        value = clean(row.get(key))
        if value:
            metadata.append((label, value))

    if metadata:
        lines = "<br>".join(
            f"<strong>{html.escape(k)}:</strong> {html.escape(v)}" for k, v in metadata
        )
        body += f"<hr><p><strong>Imported from Linear</strong><br>{lines}</p>"
    return body


def safe_project_name(name: str) -> str:
    """Return a Plane-safe project name while keeping Unicode letters/numbers.

    Plane Community can reject punctuation such as '-' in project names.
    Replace punctuation/symbols with spaces and collapse whitespace.
    """
    value = unicodedata.normalize("NFKC", clean(name))
    value = "".join(c if (c.isalnum() or c.isspace()) else " " for c in value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "Imported Project"


def find_project_for_linear_name(project_by_name: dict[str, dict[str, Any]], name: str):
    """Match either the original Linear name or its Plane-safe equivalent."""
    return project_by_name.get(norm(name)) or project_by_name.get(norm(safe_project_name(name)))


def make_identifier(name: str, used: set[str]) -> str:
    words = re.findall(r"[A-Za-z0-9]+", unicodedata.normalize("NFKD", name))
    if len(words) >= 2:
        base = "".join(w[0] for w in words[:4]).upper()
    elif words:
        base = re.sub(r"[^A-Za-z0-9]", "", words[0]).upper()[:5]
    else:
        base = "PROJ"
    base = base or "PROJ"
    candidate = base[:5]
    n = 2
    while candidate in used:
        suffix = str(n)
        candidate = (base[: max(1, 5 - len(suffix))] + suffix)[:5]
        n += 1
    used.add(candidate)
    return candidate


class Plane:
    def __init__(self, base_url: str, workspace: str, api_key: str, verify_ssl: bool = True, requests_per_minute: float = 50.0):
        base_url = base_url.rstrip("/")
        if base_url.endswith("/api/v1"):
            self.api = base_url
        else:
            self.api = base_url + "/api/v1"
        self.workspace = workspace
        self.verify_ssl = verify_ssl
        self.requests_per_minute = max(1.0, float(requests_per_minute))
        self._min_request_interval = 60.0 / self.requests_per_minute
        self._last_request_started = 0.0
        self.s = requests.Session()
        self.s.headers.update({"X-API-Key": api_key, "Content-Type": "application/json"})
        # Plane changed the public API resource name from /issues/ to /work-items/.
        # Some self-hosted releases still expose only /issues/, so detect it once.
        self._work_item_resource: str | None = None
        self._external_item_cache: dict[str, dict[str, dict[str, Any]]] = {}

    def _throttle(self) -> None:
        """Space *all* API calls so we stay below Plane's server-side rate limit."""
        now = time.monotonic()
        remaining = self._min_request_interval - (now - self._last_request_started)
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_started = time.monotonic()

    @staticmethod
    def _retry_after_seconds(response) -> float | None:
        value = clean(response.headers.get("Retry-After"))
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    def request(self, method: str, path: str, *, params=None, json=None, ok=(200, 201, 204)):
        url = self.api + path
        last = None
        for attempt in range(8):
            self._throttle()
            r = self.s.request(method, url, params=params, json=json, timeout=45, verify=self.verify_ssl)
            last = r
            if r.status_code in ok:
                if r.status_code == 204 or not r.content:
                    return None
                return r.json()

            if r.status_code == 429:
                # Respect the server when it tells us exactly when to retry.
                # If it does not, use a conservative one-minute window plus a
                # small attempt-dependent cushion instead of the old 1/2/4/8s
                # backoff, which is too short for a 60 requests/minute limit.
                retry_after = self._retry_after_seconds(r)
                delay = retry_after if retry_after is not None else 61.0 + min(attempt * 2.0, 10.0)
                print(f"Rate limit (HTTP 429). Reintentando tras {delay:.0f}s...", file=sys.stderr)
                time.sleep(delay)
                continue

            if 500 <= r.status_code < 600:
                time.sleep(min(2 ** attempt, 8))
                continue

            detail = r.text[:1200]
            raise RuntimeError(f"{method} {url} -> HTTP {r.status_code}: {detail}")
        raise RuntimeError(f"{method} {url} failed after retries: HTTP {last.status_code}: {last.text[:1200]}")

    def get_all(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        out: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        while True:
            data = self.request("GET", path, params=params)
            if isinstance(data, list):
                out.extend(x for x in data if isinstance(x, dict))
                break
            if not isinstance(data, dict):
                break
            results = data.get("results")
            if results is None:
                results = data.get("data")
            if isinstance(results, list):
                out.extend(x for x in results if isinstance(x, dict))
            elif "id" in data:
                out.append(data)
                break
            next_cursor = data.get("next_cursor")
            has_next = data.get("next_page_results")
            if not next_cursor or has_next is False or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            params["cursor"] = next_cursor
        return out

    def projects(self):
        return self.get_all(f"/workspaces/{self.workspace}/projects/")

    def create_project(self, name: str, identifier: str):
        return self.request("POST", f"/workspaces/{self.workspace}/projects/", json={
            "name": name,
            "identifier": identifier,
            "description": "Imported from Linear CSV",
        })

    def members(self):
        data = self.request("GET", f"/workspaces/{self.workspace}/members/")
        return data if isinstance(data, list) else []

    def states(self, project_id: str):
        return self.get_all(f"/workspaces/{self.workspace}/projects/{project_id}/states/")

    def create_state(self, project_id: str, name: str, group: str):
        return self.request("POST", f"/workspaces/{self.workspace}/projects/{project_id}/states/", json={
            "name": name,
            "color": STATE_COLORS.get(group, "#6B7280"),
            "group": group,
            "external_source": "linear",
            "external_id": f"linear-state:{name}",
        })

    def labels(self, project_id: str):
        return self.get_all(f"/workspaces/{self.workspace}/projects/{project_id}/labels/")

    def create_label(self, project_id: str, name: str, color: str):
        return self.request("POST", f"/workspaces/{self.workspace}/projects/{project_id}/labels/", json={
            "name": name,
            "color": color,
            "description": "Imported from Linear",
            "external_source": "linear",
            "external_id": f"linear-label:{name}",
        })

    @staticmethod
    def _is_404(exc: Exception) -> bool:
        return "HTTP 404" in str(exc)

    def work_item_resource(self, project_id: str) -> str:
        """Return 'work-items' on newer Plane, 'issues' on older/self-hosted releases."""
        if self._work_item_resource:
            return self._work_item_resource

        base = f"/workspaces/{self.workspace}/projects/{project_id}"
        try:
            self.get_all(f"{base}/work-items/", params={"per_page": 1})
            self._work_item_resource = "work-items"
        except RuntimeError as exc:
            if not self._is_404(exc):
                raise
            # Several self-hosted releases still expose the legacy public endpoint.
            self.get_all(f"{base}/issues/", params={"per_page": 1})
            self._work_item_resource = "issues"

        print(f"API de work items detectada: /{self._work_item_resource}/")
        return self._work_item_resource

    def work_item_collection_path(self, project_id: str) -> str:
        resource = self.work_item_resource(project_id)
        return f"/workspaces/{self.workspace}/projects/{project_id}/{resource}/"

    def _load_external_item_cache(self, project_id: str) -> dict[str, dict[str, Any]]:
        """Load existing imported items once so re-runs are idempotent.

        We filter locally because older /issues/ endpoints may ignore newer
        external_id/external_source query parameters.
        """
        if project_id in self._external_item_cache:
            return self._external_item_cache[project_id]

        items = self.get_all(self.work_item_collection_path(project_id), params={"per_page": 100})
        index: dict[str, dict[str, Any]] = {}
        for item in items:
            external_id = clean(item.get("external_id"))
            external_source = norm(item.get("external_source"))
            if external_id and external_source == "linear":
                index[external_id] = item
        self._external_item_cache[project_id] = index
        return index

    def find_external_work_item(self, project_id: str, external_id: str):
        return self._load_external_item_cache(project_id).get(external_id)

    def create_work_item(self, project_id: str, payload: dict[str, Any]):
        path = self.work_item_collection_path(project_id)
        created = self.request("POST", path, json=payload)
        external_id = clean(payload.get("external_id"))
        if external_id and norm(payload.get("external_source")) == "linear":
            self._external_item_cache.setdefault(project_id, {})[external_id] = created
        return created

    def update_work_item(self, project_id: str, work_item_id: str, payload: dict[str, Any]):
        resource = self.work_item_resource(project_id)
        return self.request(
            "PATCH",
            f"/workspaces/{self.workspace}/projects/{project_id}/{resource}/{work_item_id}/",
            json=payload,
        )


def read_rows(path: str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    required = {"ID", "Project", "Title", "Status", "Priority", "UUID"}
    missing = required - set(rows[0].keys() if rows else [])
    if missing:
        raise SystemExit(f"El CSV no tiene estas columnas requeridas: {', '.join(sorted(missing))}")
    return rows


def print_csv_summary(rows: list[dict[str, str]]) -> None:
    print(f"CSV: {len(rows)} issues")
    for field in ("Project", "Status", "Priority"):
        counts = Counter(clean(r.get(field)) or "(vacío)" for r in rows)
        print(f"\n{field}:")
        for k, v in counts.most_common():
            print(f"  {k}: {v}")
    labels = Counter()
    for r in rows:
        labels.update(split_csv_list(r.get("Labels", "")))
    print("\nLabels:")
    for k, v in labels.most_common():
        print(f"  {k}: {v}")
    print(f"\nParent relations: {sum(bool(clean(r.get('Parent issue'))) for r in rows)}")
    print(f"Related-to refs:  {sum(bool(clean(r.get('Related to'))) for r in rows)}")
    print(f"Blocked-by refs:  {sum(bool(clean(r.get('Blocked by'))) for r in rows)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Import Linear CSV to Plane")
    ap.add_argument("csv_file")
    ap.add_argument("--apply", action="store_true", help="Actually create/update data. Default is dry-run.")
    ap.add_argument("--create-projects", action="store_true", help="Create Plane projects that do not exist by name.")
    ap.add_argument("--only-project", action="append", default=[], help="Only import this Linear project name. Repeatable.")
    ap.add_argument("--analyze-only", action="store_true", help="Only inspect the CSV; no Plane credentials required.")
    args = ap.parse_args()

    rows = read_rows(args.csv_file)
    if args.only_project:
        wanted = {norm(x) for x in args.only_project}
        rows = [r for r in rows if norm(r.get("Project", "")) in wanted]

    print_csv_summary(rows)
    if args.analyze_only:
        return 0

    base_url = os.environ.get("PLANE_BASE_URL", "").strip()
    workspace = os.environ.get("PLANE_WORKSPACE", "").strip()
    api_key = os.environ.get("PLANE_API_KEY", "").strip()
    if not all((base_url, workspace, api_key)):
        print("\nFaltan credenciales. Define PLANE_BASE_URL, PLANE_WORKSPACE y PLANE_API_KEY.", file=sys.stderr)
        return 2

    verify_ssl = norm(os.environ.get("PLANE_VERIFY_SSL", "true")) not in {"false", "0", "no", "off"}
    try:
        requests_per_minute = float(os.environ.get("PLANE_REQUESTS_PER_MINUTE", "50"))
    except ValueError:
        print("PLANE_REQUESTS_PER_MINUTE debe ser un número.", file=sys.stderr)
        return 2
    if requests_per_minute <= 0:
        print("PLANE_REQUESTS_PER_MINUTE debe ser mayor que 0.", file=sys.stderr)
        return 2
    plane = Plane(base_url, workspace, api_key, verify_ssl=verify_ssl, requests_per_minute=requests_per_minute)

    print(f"\nConectando a Plane... (límite cliente: {requests_per_minute:g} req/min)")
    members = plane.members()
    member_by_email = {norm(m.get("email", "")): m for m in members if m.get("email")}
    print(f"Workspace members visibles por API: {len(members)}")

    projects = plane.projects()
    project_by_name = {norm(p.get("name", "")): p for p in projects}
    used_identifiers = {clean(p.get("identifier")).upper() for p in projects if clean(p.get("identifier"))}

    rows_by_project: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        rows_by_project[clean(r.get("Project"))].append(r)

    missing_projects = [name for name in rows_by_project if not find_project_for_linear_name(project_by_name, name)]
    if missing_projects:
        print("\nProyectos no encontrados en Plane:")
        for name in missing_projects:
            print(f"  - {name}")
        if not args.create_projects:
            print("Usa --create-projects para crearlos automáticamente, o créalos manualmente.")
            return 3
        if not args.apply:
            print("Dry-run: se crearían esos proyectos. Ejecuta con --apply --create-projects para continuar.")
            return 0
        for name in missing_projects:
            identifier = make_identifier(name, used_identifiers)
            plane_name = safe_project_name(name)
            if plane_name != name:
                print(f"Creando proyecto {name!r} como {plane_name!r} ({identifier})...")
            else:
                print(f"Creando proyecto {name!r} ({identifier})...")
            p = plane.create_project(plane_name, identifier)
            # Keep aliases for both the Linear source name and Plane-safe name,
            # so subsequent passes/reruns remain idempotent.
            project_by_name[norm(name)] = p
            project_by_name[norm(plane_name)] = p

    # Prepare states and labels per project.
    prepared: dict[str, dict[str, Any]] = {}
    for project_name, prows in rows_by_project.items():
        project = find_project_for_linear_name(project_by_name, project_name)
        if not project:
            raise RuntimeError(f"No se pudo resolver el proyecto de Plane para {project_name!r}")
        pid = project["id"]
        print(f"\nProyecto: {project_name} ({pid})")

        states = plane.states(pid)
        state_by_name = {norm(s.get("name", "")): s for s in states}
        # Treat canceled/cancelled as aliases.
        if "cancelled" in state_by_name and "canceled" not in state_by_name:
            state_by_name["canceled"] = state_by_name["cancelled"]
        if "canceled" in state_by_name and "cancelled" not in state_by_name:
            state_by_name["cancelled"] = state_by_name["canceled"]

        needed_statuses = sorted({clean(r.get("Status")) for r in prows if clean(r.get("Status"))})
        missing_states = [s for s in needed_statuses if norm(s) not in state_by_name]
        for state_name in missing_states:
            group = STATE_GROUPS.get(norm(state_name), "unstarted")
            if args.apply:
                print(f"  + estado {state_name} [{group}]")
                created = plane.create_state(pid, state_name, group)
                state_by_name[norm(state_name)] = created
            else:
                print(f"  [dry-run] crearía estado {state_name} [{group}]")

        labels = plane.labels(pid)
        label_by_name = {norm(l.get("name", "")): l for l in labels}
        needed_labels = sorted({label for r in prows for label in split_csv_list(r.get("Labels", ""))}, key=str.casefold)
        for i, label_name in enumerate(needed_labels):
            if norm(label_name) not in label_by_name:
                if args.apply:
                    print(f"  + label {label_name}")
                    created = plane.create_label(pid, label_name, LABEL_COLORS[i % len(LABEL_COLORS)])
                    label_by_name[norm(label_name)] = created
                else:
                    print(f"  [dry-run] crearía label {label_name}")

        prepared[project_name] = {
            "project": project,
            "states": state_by_name,
            "labels": label_by_name,
        }

    if not args.apply:
        print("\nDRY-RUN terminado: no se ha modificado Plane.")
        print("Cuando el plan te cuadre, ejecuta de nuevo con --apply.")
        return 0

    # First pass: create all work items without parents.
    linear_to_plane: dict[str, tuple[str, str]] = {}  # Linear ID -> (project_id, Plane work_item_id)
    created_count = 0
    skipped_count = 0
    warnings: list[str] = []

    for project_name, prows in rows_by_project.items():
        cfg = prepared[project_name]
        pid = cfg["project"]["id"]
        states = cfg["states"]
        labels = cfg["labels"]

        for idx, row in enumerate(prows, 1):
            linear_id = clean(row.get("ID"))
            external_id = clean(row.get("UUID")) or linear_id
            existing = plane.find_external_work_item(pid, external_id)
            if existing:
                linear_to_plane[linear_id] = (pid, existing["id"])
                skipped_count += 1
                print(f"  = {linear_id} ya importado, se omite")
                continue

            state_name = clean(row.get("Status"))
            state_obj = states.get(norm(state_name))
            if not state_obj:
                warnings.append(f"{linear_id}: no se encontró estado {state_name!r}; se usará el default de Plane")

            assignee_ids: list[str] = []
            assignee = clean(row.get("Assignee"))
            if assignee:
                member = member_by_email.get(norm(assignee))
                if member:
                    assignee_ids = [member["id"]]
                else:
                    warnings.append(f"{linear_id}: assignee {assignee} no existe como miembro del workspace")

            label_ids = []
            for label_name in split_csv_list(row.get("Labels", "")):
                obj = labels.get(norm(label_name))
                if obj:
                    label_ids.append(obj["id"])

            payload: dict[str, Any] = {
                "name": clean(row.get("Title")) or linear_id,
                "description_html": description_html(row),
                "priority": PRIORITIES.get(norm(row.get("Priority", "")), "none"),
                "assignees": assignee_ids,
                "labels": label_ids,
                "external_id": external_id,
                "external_source": "linear",
            }
            if state_obj:
                payload["state"] = state_obj["id"]
            start = first_date(row.get("Started", ""))
            due = first_date(row.get("Due Date", ""))
            if start:
                payload["start_date"] = start
            if due:
                payload["target_date"] = due

            created = plane.create_work_item(pid, payload)
            linear_to_plane[linear_id] = (pid, created["id"])
            created_count += 1
            print(f"  + {linear_id}: {payload['name']}")

    # Second pass: parent/child relationships once all IDs are known.
    parent_updates = 0
    for row in rows:
        linear_id = clean(row.get("ID"))
        parent_linear_id = clean(row.get("Parent issue"))
        if not parent_linear_id:
            continue
        child = linear_to_plane.get(linear_id)
        parent = linear_to_plane.get(parent_linear_id)
        if not child or not parent:
            warnings.append(f"{linear_id}: no se pudo resolver parent {parent_linear_id}")
            continue
        child_pid, child_wid = child
        parent_pid, parent_wid = parent
        if child_pid != parent_pid:
            warnings.append(f"{linear_id}: parent {parent_linear_id} está en otro proyecto; se omite")
            continue
        plane.update_work_item(child_pid, child_wid, {"parent": parent_wid})
        parent_updates += 1

    print("\n=== Resultado ===")
    print(f"Creados: {created_count}")
    print(f"Ya existentes / omitidos: {skipped_count}")
    print(f"Parents aplicados: {parent_updates}")
    if warnings:
        print(f"Warnings: {len(warnings)}")
        for w in warnings[:50]:
            print(f"  ! {w}")
        if len(warnings) > 50:
            print(f"  ... y {len(warnings)-50} más")
    else:
        print("Warnings: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

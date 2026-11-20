import io
import json
import re
import tarfile
import tempfile
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from syncret.server.db import now

router = APIRouter()
RELEASE_ID = re.compile(r"[0-9a-f]{12}")
AGENTS = ("claude", "copilot", "zed")


# which agents link a skill: local agents get it, except claude code when the claude account
# also has it, since claude code loads account skills too and would see two copies
def links(targets):
    local, account = "local" in targets, "account" in targets
    return {"claude": local and not account, "copilot": local, "zed": local}


# a release: shipped skills under skills/<folder>/ and a manifest of which agents link them
def build_release(repo, commit, release_id, path):
    entries = repo.skills(commit)["skills"]
    manifest = {"release": release_id, "commit": commit, "created": now(), "skills": []}
    used = set()
    with tempfile.TemporaryDirectory() as temp, tarfile.open(path, "w:gz") as archive:
        for key in sorted(entries):
            entry = entries[key]
            agents = links(entry.get("targets", []))
            if not any(agents.values()) or not repo.files(commit, key):
                continue
            base = re.sub(r"[^a-z0-9._-]+", "-", entry["name"].lower()).strip("-.") or "skill"
            folder, n = base, 2
            while folder in used:
                folder, n = f"{base}-{n}", n + 1
            used.add(folder)
            source = repo.export(commit, key, Path(temp) / folder)
            archive.add(source, arcname=f"skills/{folder}", filter=_clean)
            manifest["skills"].append({"folder": folder, "key": key, "name": entry["name"],
                                       "kind": entry["kind"], "links": agents})
        data = (json.dumps(manifest, indent=2) + "\n").encode()
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mode = len(data), 0o644
        archive.addfile(info, io.BytesIO(data))
    return manifest


# tar entries without owner names or times from the build machine
def _clean(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


# the machine a request's bearer token belongs to, or None
def _machine(request):
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    return request.app.state.service.machines.check(token)


def _refused():
    return JSONResponse({"detail": "a valid machine token is needed"}, 401,
                        headers={"WWW-Authenticate": "Bearer"})


@router.get("/release/latest")
async def latest(request: Request):
    machine = _machine(request)
    if machine is None:
        return _refused()
    service = request.app.state.service
    release = service.latest_release()
    if release is None:
        return JSONResponse({"detail": "nothing published yet"}, 404)
    return {"id": release["id"], "sha256": release["sha256"],
            "url": f"/release/{release['id']}.tar.gz",
            "interval_minutes": service.settings.load()["sync_interval_minutes"]}


@router.get("/release/{release_id}.tar.gz")
async def download(request: Request, release_id: str):
    machine = _machine(request)
    if machine is None:
        return _refused()
    service = request.app.state.service
    path = service.releases / f"{release_id}.tar.gz"
    if not RELEASE_ID.fullmatch(release_id) or not path.is_file():
        return JSONResponse({"detail": "no such release"}, 404)
    service.machines.synced(machine.id, release_id)
    return FileResponse(path, media_type="application/gzip", filename=f"{release_id}.tar.gz")

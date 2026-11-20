from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from syncret.core.llm import LlmError
from syncret.server.app_base import check_csrf, page, user
from syncret.server.mcp import new_token
from syncret.server.service import Refused, safe

router = APIRouter()
# proposal states shown first, since they need the user
WAITING = ("open", "checking", "approving")


def _service(request):
    return request.app.state.service


async def _form(request):
    form = await request.form()
    return {key: value for key, value in form.multi_items() if isinstance(value, str)}


# published skills, or none while the worker has not created the repo yet
def _published(service):
    try:
        return service.repo.skills("published")["skills"]
    except RuntimeError:
        return {}


# first-run steps as label, done and link, shown on the proposals page until all are done
def _setup(service, rows):
    machines = [m for m in service.machines.list() if not m.revoked]
    return [("Add a skill from GitHub, skills.sh or a folder", bool(rows), "/add"),
            ("Review the proposal and approve it", bool(_published(service)), "/proposals"),
            ("Create a token for a computer", bool(machines), "/machines"),
            ("Install the client on that computer", any(m.last_sync for m in machines),
             "/machines")]


@router.get("/proposals")
async def proposals(request: Request):
    user(request)
    service = _service(request)
    rows = service.proposals.list()
    waiting = [p for p in rows if p.state in WAITING]
    done = [p for p in rows if p.state not in WAITING]
    return page(request, "proposals.html", waiting=waiting, done=done, setup=_setup(service, rows),
                busy=any(p.state in ("checking", "approving") for p in waiting))


@router.get("/proposals/{proposal_id}")
async def proposal(request: Request, proposal_id: int, error: str = ""):
    user(request)
    try:
        found, diff = _service(request).show(proposal_id)
    except Refused as refused:
        return page(request, "message.html", status=404, message=str(refused))
    return page(request, "proposal.html", p=found, diff=diff, error=error,
                sync_minutes=_service(request).settings.load()["sync_interval_minutes"])


@router.post("/proposals/{proposal_id}/approve")
async def approve(request: Request, proposal_id: int):
    user(request)
    await check_csrf(request)
    form = await _form(request)
    try:
        job = _service(request).approve(proposal_id, form.get("base", ""))
    except Refused as refused:
        found, diff = _service(request).show(proposal_id)
        return page(request, "proposal.html", status=409, p=found, diff=diff, error=str(refused),
                    sync_minutes=_service(request).settings.load()["sync_interval_minutes"])
    return RedirectResponse(f"/jobs/{job}", 303)


@router.post("/proposals/{proposal_id}/reject")
async def reject(request: Request, proposal_id: int):
    user(request)
    await check_csrf(request)
    try:
        job = _service(request).reject(proposal_id)
    except Refused as refused:
        return page(request, "message.html", status=409, message=str(refused))
    return RedirectResponse(f"/jobs/{job}", 303)


@router.get("/add")
async def add_page(request: Request):
    user(request)
    return page(request, "add.html", source="", choices=[], error="")


@router.post("/add")
async def add(request: Request):
    user(request)
    await check_csrf(request)
    form = await request.form()
    source = form.get("source")
    source = source if isinstance(source, str) else ""
    skills = [s for s in form.getlist("skills") if isinstance(s, str) and s]
    targets = [t for t in form.getlist("targets") if isinstance(t, str)] or ["local"]
    try:
        job = _service(request).add(source, skills, targets)
    except Refused as refused:
        return page(request, "add.html", status=400, source=source, choices=[],
                    error=str(refused))
    return RedirectResponse(f"/jobs/{job}", 303)


@router.get("/skills")
async def skills(request: Request):
    user(request)
    return page(request, "skills.html", skills=sorted(_published(_service(request)).items()))


@router.post("/skills/remove")
async def remove(request: Request):
    user(request)
    await check_csrf(request)
    form = await _form(request)
    try:
        job = _service(request).remove(form.get("key", ""))
    except Refused as refused:
        return page(request, "message.html", status=409, message=str(refused))
    return RedirectResponse(f"/jobs/{job}", 303)


@router.get("/skills/edit")
async def edit_page(request: Request, key: str):
    user(request)
    service = _service(request)
    entry = _published(service).get(key)
    if entry is None or entry["kind"] != "own":
        return page(request, "message.html", status=404,
                    message=f"{key} is not a published own skill")
    content = service.repo.git("show", f"published:{key}/SKILL.md").decode(errors="replace")
    return page(request, "edit.html", key=key, content=content, error="")


@router.post("/skills/edit")
async def edit(request: Request):
    user(request)
    await check_csrf(request)
    form = await _form(request)
    key, content = form.get("key", ""), form.get("content", "")
    try:
        job = _service(request).edit(key, content.replace("\r\n", "\n"))
    except Refused as refused:
        return page(request, "edit.html", status=409, key=key, content=content,
                    error=str(refused))
    return RedirectResponse(f"/jobs/{job}", 303)


@router.get("/scan")
async def scan_page(request: Request):
    user(request)
    jobs = [j for j in _service(request).queue.jobs(500) if j.kind == "scan-all"]
    last = next((j for j in jobs if j.state == "done"), None)
    running = next((j for j in jobs if j.state in ("queued", "running")), None)
    return page(request, "scan.html", last=last, running=running)


@router.post("/scan")
async def scan(request: Request):
    user(request)
    await check_csrf(request)
    return RedirectResponse(f"/jobs/{_service(request).scan_all()}", 303)


@router.get("/jobs")
async def jobs(request: Request):
    user(request)
    rows = _service(request).queue.jobs()
    return page(request, "jobs.html", jobs=rows,
                busy=any(j.state in ("queued", "running") for j in rows))


@router.get("/jobs/{job_id}")
async def job(request: Request, job_id: int):
    user(request)
    found = _service(request).queue.get(job_id)
    if found is None:
        return page(request, "message.html", status=404, message=f"no job {job_id}")
    partial = bool(request.headers.get("hx-request"))
    return page(request, "_job.html" if partial else "job.html", job=found)


def _settings_page(request, status=200, **context):
    service = _service(request)
    providers, llm_error = [], ""
    try:
        chain = service.llm()
        providers = list(chain.providers.values()) if chain else []
    except LlmError as error:
        llm_error = str(error)
    jobs = [j for j in service.queue.jobs(200) if j.kind == "llm-check"]
    updates = [j for j in service.queue.jobs(500) if j.kind == "check-updates"]
    context = {"settings": service.settings.load(), "token": service.settings.token_info(),
               "llm_text": service.settings.llm_text(), "providers": providers,
               "llm_error": llm_error, "check": jobs[0] if jobs else None, "message": "",
               "error": "", "mcp_token": "", "updates": updates[0] if updates else None,
               "mcp_set": (service.data / "secrets" / "mcp-token.json").is_file(), **context}
    return page(request, "settings.html", status=status, **context)


@router.get("/settings")
async def settings(request: Request):
    user(request)
    return _settings_page(request)


@router.post("/settings/intervals")
async def save_intervals(request: Request):
    user(request)
    await check_csrf(request)
    form = await _form(request)
    try:
        values = {key: int(form.get(key, "")) for key in ("check_interval_hours",
                                                          "sync_interval_minutes")}
        _service(request).settings.save(**values)
    except ValueError as error:
        return _settings_page(request, status=400, error=str(error))
    return _settings_page(request, message="Intervals saved.")


@router.post("/settings/token")
async def save_token(request: Request):
    user(request)
    await check_csrf(request)
    form = await _form(request)
    try:
        _service(request).settings.save_token(form.get("token", ""))
    except ValueError as error:
        return _settings_page(request, status=400, error=str(error))
    return _settings_page(request, message="Claude token saved. Run the LLM check to test it.")


@router.post("/settings/llm")
async def save_llm(request: Request):
    user(request)
    await check_csrf(request)
    text = (await _form(request)).get("llm", "").replace("\r\n", "\n")
    try:
        names = _service(request).settings.save_llm(text)
    except ValueError as error:
        return _settings_page(request, status=400, llm_text=text, error=str(error))
    return _settings_page(request, message=f"Providers saved: {', '.join(names) or 'none'}.")


# a new mcp token, shown in this response only; it replaces any earlier one
@router.post("/settings/mcp-token")
async def mcp_token(request: Request):
    user(request)
    await check_csrf(request)
    token = new_token(_service(request).data)
    return _settings_page(request, message="New MCP token made; the old one no longer works.",
                          mcp_token=token)


@router.post("/settings/check-updates")
async def check_updates(request: Request):
    user(request)
    await check_csrf(request)
    return RedirectResponse(f"/jobs/{_service(request).check_updates()}", 303)


@router.post("/settings/llm-check")
async def llm_check(request: Request):
    user(request)
    await check_csrf(request)
    return RedirectResponse(f"/jobs/{_service(request).llm_check()}", 303)


@router.get("/machines")
async def machines(request: Request):
    user(request)
    return page(request, "machines.html", machines=_service(request).machines.list(),
                token="", machine_name="", error="")


# the new token is shown in this response only, never in a url or stored in clear
@router.post("/machines")
async def create_machine(request: Request):
    user(request)
    await check_csrf(request)
    service = _service(request)
    name = (await _form(request)).get("name", "")
    try:
        token = service.machines.create(name)
    except ValueError as error:
        return page(request, "machines.html", status=400, machines=service.machines.list(),
                    token="", machine_name=name, error=str(error))
    return page(request, "machines.html", machines=service.machines.list(), token=token,
                machine_name=name.strip(), error="")


@router.post("/machines/{machine_id}/revoke")
async def revoke_machine(request: Request, machine_id: int):
    user(request)
    await check_csrf(request)
    _service(request).machines.revoke(machine_id)
    return RedirectResponse("/machines", 303)


# published skills targeted at the claude account, with whether the upload is current
def _account_skills(service):
    uploads = service.account.all()
    rows = []
    for key, entry in sorted(_published(service).items()):
        if "account" not in entry.get("targets", []):
            continue
        tree = service.repo.text("rev-parse", f"published:{key}")
        upload = uploads.get(key)
        rows.append({"key": key, "entry": entry, "tree": tree, "upload": upload,
                     "current": bool(upload and upload["tree"] == tree)})
    return rows


@router.get("/account")
async def account(request: Request):
    user(request)
    return page(request, "account.html", skills=_account_skills(_service(request)))


@router.get("/account/zip")
async def account_zip(request: Request, key: str):
    user(request)
    service = _service(request)
    entry = _published(service).get(key)
    if entry is None or "account" not in entry.get("targets", []):
        return page(request, "message.html", status=404,
                    message=f"{key} is not a published skill targeted at the Claude account")
    name = safe(entry["name"])
    data = service.repo.git("archive", "--format=zip", f"--prefix={name}/", f"published:{key}")
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


@router.post("/account/done")
async def account_done(request: Request):
    user(request)
    await check_csrf(request)
    service = _service(request)
    key = (await _form(request)).get("key", "")
    rows = {row["key"]: row for row in _account_skills(service)}
    if key in rows:
        service.account.mark(key, rows[key]["tree"])
    return RedirectResponse("/account", 303)

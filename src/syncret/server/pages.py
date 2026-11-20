from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from syncret.server.app_base import check_csrf, page, user
from syncret.server.service import Refused

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


@router.get("/proposals")
async def proposals(request: Request):
    user(request)
    service = _service(request)
    rows = service.proposals.list()
    waiting = [p for p in rows if p.state in WAITING]
    done = [p for p in rows if p.state not in WAITING]
    return page(request, "proposals.html", waiting=waiting, done=done,
                busy=any(p.state in ("checking", "approving") for p in waiting))


@router.get("/proposals/{proposal_id}")
async def proposal(request: Request, proposal_id: int, error: str = ""):
    user(request)
    try:
        found, diff = _service(request).show(proposal_id)
    except Refused as refused:
        return page(request, "message.html", status=404, message=str(refused))
    return page(request, "proposal.html", p=found, diff=diff, error=error)


@router.post("/proposals/{proposal_id}/approve")
async def approve(request: Request, proposal_id: int):
    user(request)
    await check_csrf(request)
    form = await _form(request)
    try:
        job = _service(request).approve(proposal_id, form.get("base", ""))
    except Refused as refused:
        found, diff = _service(request).show(proposal_id)
        return page(request, "proposal.html", status=409, p=found, diff=diff, error=str(refused))
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

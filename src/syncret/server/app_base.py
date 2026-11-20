from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from fastapi.templating import Jinja2Templates

from syncret.core.text import shown
from syncret.server import auth

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
# skill text and diffs show hidden and control characters as <U+XXXX>, after autoescaping
templates.env.filters["shown"] = lambda text: shown(text, keep_newlines=True)


# an iso time as a short utc time, anything else unchanged
def when(value):
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y, %H:%M UTC")
    except (TypeError, ValueError):
        return value or ""


# each diff line with the class that colours it
def diff_lines(diff):
    for line in diff.split("\n"):
        if line.startswith(("diff --git", "+++", "---", "index ")):
            yield "file", line
        elif line.startswith("@@"):
            yield "hunk", line
        elif line.startswith("+"):
            yield "add", line
        elif line.startswith("-"):
            yield "del", line
        else:
            yield "", line


templates.env.filters["when"] = when
templates.env.filters["diff_lines"] = diff_lines
# scripts only from syncret itself; no framing, no plugins, no referrer
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                               "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; "
                               "form-action 'self'; object-src 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer"
}


# a page request without a login: browsers go to the login page, htmx is told to go there
class NotLoggedIn(Exception):
    pass


# the logged-in user, or NotLoggedIn
def user(request):
    if request.session.get("user") != "owner":
        raise NotLoggedIn
    return "owner"


# reject a write whose csrf token does not match the session's
async def check_csrf(request):
    token = request.headers.get("x-csrf-token")
    if token is None:
        token = (await request.form()).get("csrf")
    if not auth.csrf_ok(request.session, token if isinstance(token, str) else None):
        raise HTTPException(403, "missing or wrong CSRF token; reload the page and try again")


# a template rendered with the session's csrf token
def page(request, name, status=200, **context):
    context |= {"csrf": auth.csrf_token(request.session),
                "logged_in": request.session.get("user") == "owner"}
    return templates.TemplateResponse(request, name, context, status_code=status)

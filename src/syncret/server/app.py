import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Route

from syncret.server import auth
from syncret.server.app_base import HEADERS, HERE, NotLoggedIn, check_csrf, page, user
from syncret.server.mcp import BearerGate, transport_security
from syncret.server.mcp import build as build_mcp
from syncret.server.pages import router
from syncret.server.release import router as release_router
from syncret.server.service import Service
from syncret.server.worker import Worker


# the dashboard app; with start_worker the only writer runs inside the same process
def create_app(data, start_worker=True):
    service = Service(data)

    mcp = build_mcp(service)
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                      json_response=True, transport_security=transport_security())

    # queue an upstream check whenever one is due; a failed tick waits for the next one
    async def schedule():
        while True:
            try:
                await asyncio.to_thread(service.schedule_due)
            # the loop must outlive one bad tick
            except Exception as error:
                print(f"syncret: scheduling upstream checks failed: {error}")
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(app):
        task = scheduler = None
        if start_worker:
            worker = Worker(service.queue, service.repo, service.handlers)
            task = asyncio.create_task(worker.run())
            scheduler = asyncio.create_task(schedule())
        app.state.worker = task
        async with mcp.session_manager.run():
            yield
        for running in (task, scheduler):
            if running:
                running.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.worker = None
    app.add_middleware(SessionMiddleware, secret_key=auth.session_key(data),
                       session_cookie="syncret", same_site="strict",
                       https_only=os.environ.get("SYNCRET_HTTPS") == "1")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.update(HEADERS)
        return response

    @app.exception_handler(NotLoggedIn)
    async def not_logged_in(request, error):
        if request.headers.get("hx-request"):
            return JSONResponse({"detail": "log in"}, 401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", 303)

    @app.get("/health")
    async def health():
        task = app.state.worker
        if task is not None and task.done():
            return JSONResponse({"status": "worker stopped"}, 503)
        return {"status": "ok"}

    @app.get("/login")
    async def login_page(request: Request):
        has_password = auth.password_record(data) is not None
        return page(request, "login.html", has_password=has_password)

    @app.post("/login")
    async def login(request: Request):
        await check_csrf(request)
        record = auth.password_record(data)
        password = (await request.form()).get("password")
        if record is None or not isinstance(password, str) \
                or not auth.verify_password(password, record):
            await asyncio.sleep(1)
            return page(request, "login.html", status=401, has_password=record is not None,
                        error="Wrong password.")
        request.session.clear()
        request.session["user"] = "owner"
        return RedirectResponse("/", 303)

    @app.post("/logout")
    async def logout(request: Request):
        user(request)
        await check_csrf(request)
        request.session.clear()
        return RedirectResponse("/login", 303)

    app.include_router(router)
    app.include_router(release_router)
    app.router.routes.append(Route("/mcp", BearerGate(mcp_app, data)))

    @app.get("/")
    async def home(request: Request):
        user(request)
        return RedirectResponse("/proposals", 303)

    return app

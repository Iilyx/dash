"""Dashboard page: Stimulsoft viewer beside a chat that sends the current filters."""

import asyncio
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from stimulsoft_reports.report import StiReport
from stimulsoft_reports.viewer import StiViewer

from pipeline import load_env

ROOT = Path(__file__).resolve().parent
DASHBOARD = "dashboards/SalesOfProducts.mrt"
load_env()
ASK_URL = os.environ.get("ASK_URL", "http://127.0.0.1:8090/ask")
FILTERS_JS = (ROOT / "static" / "filters.js").read_text(encoding="utf-8")

app = FastAPI()
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def http_response(viewer: StiViewer, origin: str) -> Response:
    result = viewer.getResponse()
    return Response(
        content=result.data,
        media_type=result.contentType,
        headers={
            "Access-Control-Allow-Origin": result.origin or origin,
            "X-Stimulsoft-Result": result.result.type or "",
        },
    )


def forward_ask(body: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(
        ASK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        payload = json.dumps({"detail": f"Хост не отвечает: {exc.reason}"}, ensure_ascii=False).encode()
        return 502, payload


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.post("/ask")
async def ask(request: Request) -> Response:
    status, payload = await asyncio.to_thread(forward_ask, await request.body())
    return Response(content=payload, status_code=status, media_type="application/json")


@app.api_route("/viewer", methods=["GET", "POST"])
async def viewer(request: Request) -> Response:
    origin = str(request.base_url).rstrip("/")
    page = StiViewer()
    page.options.appearance.fullScreenMode = True
    page.handler.url = f"{origin}/viewer"
    page.handler.origin = origin
    page.javascript.appendHead(f"<script>\n{FILTERS_JS}\n</script>")
    page.onInteraction += "publishDashboardFilters"
    page.onAfterInitialize += "publishDashboardFilters"

    if page.processRequest(query=dict(request.query_params), body=await request.body()):
        return http_response(page, origin)

    report = StiReport()
    # loadFile checks the file with a request. That request has to be served
    # while this handler is still running, so it cannot stay on the event loop.
    await asyncio.to_thread(report.loadFile, f"{origin}/static/{DASHBOARD}")
    page.report = report
    return http_response(page, origin)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=5050)

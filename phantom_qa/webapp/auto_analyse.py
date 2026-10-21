"""Optional routes. Dependencies flow from this adapter into the existing app."""
from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from .. import auto_analyse, pipeline
from .display import display_range


def create_router(backend):
    router = APIRouter(prefix="/api/auto-analyse", tags=["auto-analyse"])

    def process(scans, data, labels, request):
        analyses, seen = [], {}
        for scan in scans:
            # Shared persistence rechecks the hash atomically BEFORE inserting.
            # This is authoritative even if another worker uploaded meanwhile.
            aid, created = backend._save_upload(scan, data, labels, request, reuse_existing=True)
            if aid in seen:
                continue  # identical members of the same archive count once
            duplicate = not created
            if created:
                backend._run_default_analysis(aid, request)
            else:
                # Open the saved result; every displayed measurement has a durable source.
                backend.audit("upload", user=backend._current_user(request),
                              client=backend._client_key(request), analysis=aid,
                              outcome="duplicate", workflow="auto_analyse", existing=[aid])
            record = backend.store.get(aid)
            if record is None:
                from fastapi import HTTPException
                raise HTTPException(404, "Analysis was deleted while processing. Upload the scan again.")
            payload = auto_analyse.result_payload(record, backend.pdef, duplicate, saved=created)
            payload["image"] = {"rows": scan.shape[0], "cols": scan.shape[1]}
            payload["display_range"] = display_range(scan.pixels)
            analyses.append(payload)
            seen[aid] = True
        return pipeline.to_jsonable({"analyses": analyses})

    @router.post("")
    async def upload(request: Request, file: UploadFile = File(...),
                     site: str = Form(""), phantom: str = Form(""),
                     operator: str = Form(""), notes: str = Form("")):
        data, scans = await backend._read_upload(request, file)
        labels = {key: value.strip() for key, value in
                  {"site": site, "phantom": phantom, "operator": operator, "notes": notes}.items()}
        # CPU work and database writes must not block the async server loop.
        return await run_in_threadpool(process, scans, data, labels, request)

    @router.get("/{aid}")
    def saved_result(aid: str, duplicate: bool = False):
        from fastapi import HTTPException
        record = backend.store.get(aid)
        if record is None:
            raise HTTPException(404, "analysis not found")
        payload = auto_analyse.result_payload(record, backend.pdef, duplicate, saved=False)
        payload["image"] = {"rows": (record.get("meta") or {}).get("Rows", 1),
                            "cols": (record.get("meta") or {}).get("Columns", 1)}
        try:
            scan = backend._scan(aid)
            payload["image"] = {"rows": scan.shape[0], "cols": scan.shape[1]}
            payload["display_range"] = display_range(scan.pixels)
        except Exception:
            payload["display_range"] = None
        return pipeline.to_jsonable(payload)

    return router

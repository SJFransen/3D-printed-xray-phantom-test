"""Optional routes. Dependencies flow from this adapter into the existing app."""
from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from .. import auto_analyse, pipeline


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
            automatic = auto_analyse.run_automatic(scan, backend.pdef)
            if created:
                backend.store.update(aid, reg=automatic["reg"], sid_mm=1000.0,
                                     layout_source="auto")
                backend.store.set_geometry_baseline(aid, automatic["geometry"],
                                                    action="auto_propose", user=backend._current_user(request))
                backend.store.audit(aid, "B", "automatic proposals generated")
                backend.store.update(aid, results=automatic["results"], status=automatic["status"], stage="F")
                backend.store.audit(aid, "E", "computed", {"overall": automatic["status"],
                                                         "sid_mm": 1000.0, "workflow": "auto_analyse"})
                backend.audit("compute", user=backend._current_user(request),
                              client=backend._client_key(request), analysis=aid,
                              outcome=automatic["status"], workflow="auto_analyse", sid_mm=1000.0)
                backend.finalize(aid, backend.FinalizeBody(), request)
            else:
                # Reassess automatically in memory: an existing Analyze record
                # may contain manual corrections or an administrator's ruling.
                # Never replace its measurements, positions, labels or sign-off.
                backend.audit("upload", user=backend._current_user(request),
                              client=backend._client_key(request), analysis=aid,
                              outcome="duplicate", workflow="auto_analyse", existing=[aid])
            record = backend.store.get(aid)
            if record is None:
                from fastapi import HTTPException
                raise HTTPException(404, "Analysis was deleted while processing. Upload the scan again.")
            payload = auto_analyse.result_payload(record, automatic, duplicate, saved=created)
            payload["image"] = {"rows": scan.shape[0], "cols": scan.shape[1]}
            analyses.append(payload)
            seen[aid] = True
        return pipeline.to_jsonable({"analyses": analyses})

    @router.post("")
    async def upload(request: Request, file: UploadFile = File(...),
                     site: str = Form(""), phantom: str = Form(""),
                     operator: str = Form(""), notes: str = Form("")):
        data, scans = await backend._read_upload(request, file)
        labels = {"site": site, "phantom": phantom, "operator": operator, "notes": notes}
        # CPU work and database writes must not block the async server loop.
        return await run_in_threadpool(process, scans, data, labels, request)

    return router

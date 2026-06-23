"""FastAPI application exposing the XAPK cloning endpoint.

Run with::

    uvicorn app.main:app --host 0.0.0.0 --port 4015

The endpoint ``POST /upload`` accepts a ``.xapk`` file plus optional fields
and returns the cloned XAPK as an attachment download.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.responses import Response

from .xapk_processor import cleanup, clone_xapk

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("xapk.api")

# --- Limit constants ---------------------------------------------------------------

# XAPK files can be large; let users upload up to ~3 GB to be safe. The exact
# value is read from the env so operators can override it.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(3 * 1024 * 1024 * 1024)))
TEMP_DIR = Path(os.environ.get("TEMP_DIR", "/temp/workflows"))
TOOLS_DIR = Path(os.environ.get("TOOLS_DIR", "/tools"))


# --- App factory --------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(
        title="XAPK Multi-Account Cloner",
        version="1.0.0",
    )

    # CORS wide-open: the frontend serves on :3015 while the API is on :4015.
    # Authentication is intentionally disabled per the project spec, so allowing
    # all origins is safe here. Tighten this if you put it on the public internet.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    # Accept both GET (humans / curl) and HEAD (wget --spider, docker healthcheck).
    # In FastAPI / Starlette, HEAD on a route decorated with @api_route is served
    # with the body stripped — exactly what healthcheck probes expect.
    @app.api_route("/health", methods=["GET", "HEAD"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "xapk-cloner"}

    @app.get("/tools/check")
    async def tools_check() -> dict[str, object]:
        """Report which tools are present (debugging helper)."""
        from .xapk_processor import get_tool_paths

        paths = get_tool_paths()
        return {
            "apktool_jar": str(paths.get("apktool_jar") or "missing"),
            "apktool_present": bool(paths.get("apktool_jar")),
            "zipalign": str(paths.get("zipalign") or "missing"),
            "zipalign_present": bool(paths.get("zipalign")),
            "apksigner": str(paths.get("apksigner") or "missing"),
            "apksigner_present": bool(paths.get("apksigner")),
            "debug_keystore": str(paths.get("debug_keystore") or "missing"),
            "debug_keystore_present": bool(paths.get("debug_keystore")),
            "tools_dir": str(TOOLS_DIR),
            "temp_dir": str(TEMP_DIR),
        }

    @app.post("/upload")
    async def upload(
        request: Request,
        file: UploadFile = File(..., description="Source .xapk archive"),
        suffix: str = Form(..., description="Suffix appended to the package name, e.g. _clone1"),
        new_name: Optional[str] = Form(None, description="Optional new visible app label"),
        new_icon: Optional[UploadFile] = File(None, description="Optional PNG icon"),
    ) -> Response:
        """Clone a XAPK file inside a workdir and return the result.

        Important: we deliberately do NOT use Starlette's ``BaseHTTPMiddleware``
        to enforce ``MAX_UPLOAD_BYTES`` because it forces a full-body read into
        memory before the handler runs, breaking the multipart streaming flow
        and dropping the connection mid-upload (the browser then reports
        "Network error — could not reach the backend" as soon as the first
        ``xhr.onerror`` fires). Instead we read the Content-Length header here,
        before any File/Form parameter is materialised; the body bytes flow
        straight from the socket into ``UploadFile.read`` chunks.
        """
        cl_header = request.headers.get("content-length")
        if cl_header and cl_header.isdigit() and int(cl_header) > MAX_UPLOAD_BYTES:
            cl_value = int(cl_header)
            logger.warning(
                "Rejecting oversize upload: content_length=%d > max=%d", cl_value, MAX_UPLOAD_BYTES
            )
            raise HTTPException(
                status_code=413,
                detail={
                    "error": "upload_too_large",
                    "max_bytes": MAX_UPLOAD_BYTES,
                    "received": cl_value,
                },
            )

        if not file.filename or not file.filename.lower().endswith(".xapk"):
            raise HTTPException(status_code=400, detail="A .xapk file is required.")

        # Per-request workdir under /temp to avoid cross-request collisions.
        work_id = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
        work_root = TEMP_DIR / work_id
        work_root.mkdir(parents=True, exist_ok=True)

        xapk_input_path = work_root / file.filename
        icon_input_path: Optional[Path] = None

        try:
            logger.info(
                "Receiving upload: filename=%s, suffix=%s, new_name=%s, has_icon=%s",
                file.filename,
                suffix,
                new_name,
                bool(new_icon),
            )
            # Stream the file to disk so we never buffer the entire body in memory.
            with open(xapk_input_path, "wb") as out:
                while True:
                    chunk = await file.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)

            if new_icon is not None:
                icon_filename = new_icon.filename or "icon.png"
                if not icon_filename.lower().endswith(".png"):
                    raise HTTPException(status_code=400, detail="Icon must be a PNG image.")
                icon_input_path = work_root / icon_filename
                with open(icon_input_path, "wb") as out:
                    while True:
                        chunk = await new_icon.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)

            result = clone_xapk(
                xapk_input_path=xapk_input_path,
                work_root=work_root,
                suffix=suffix,
                new_name=new_name or None,
                icon_input_path=icon_input_path,
            )

            output_basename = (
                f"{Path(file.filename).stem}-{suffix.strip('_') or 'clone'}.xapk"
            )

            logger.info(
                "Upload done: orig=%s/%s new=%s/%s size=%d",
                result.original_package,
                result.original_label,
                result.new_package,
                result.new_label,
                result.bytes_written,
            )

            return FileResponse(
                path=str(result.output_path),
                media_type="application/octet-stream",
                filename=output_basename,
                headers={
                    "X-Original-Package": result.original_package,
                    "X-New-Package": result.new_package,
                    "X-New-Label": result.new_label,
                },
            )

        except HTTPException:
            raise
        except ValueError as exc:
            logger.exception("Bad request")
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Processing failed")
            raise HTTPException(status_code=500, detail=f"Processing failed: {exc}")
        finally:
            try:
                cleanup(work_root)
            except Exception:  # noqa: BLE001
                logger.exception("Cleanup of work directory failed")

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()

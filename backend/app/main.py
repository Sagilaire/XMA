"""FastAPI application exposing the XAPK cloning endpoint.

Run with::

    uvicorn app.main:app --host 0.0.0.0 --port 4015

The endpoint ``POST /upload`` accepts a ``.xapk`` file plus optional fields
and returns the cloned XAPK as an attachment download.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from functools import wraps
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.formparsers import MultiPartParser
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


# --- Multipart parser widening ------------------------------------------------------
#
# Symptom we are defending against (DevTools after a real upload):
#   Status Code : 500 Internal Server Error
#   content-length: 21         (- the literal bytes "Internal Server Error\n")
#
# That is Starlette's default exception page — an exception escaped the handler
# stack. Investigation: Starlette's MultiPartParser.__init__ has the default
# ``max_part_size: int = 1024 * 1024`` baked into the function signature; Python
# evaluates default arguments at function-definition time, so reassigning the
# class attribute (``MultiPartParser.max_part_size = X``) is a no-op for the
# actual default value used when a caller omits the kwarg. Starlette's
# ``Request._get_form()`` / multipart code path typically calls
# ``MultiPartParser(headers, stream)`` WITHOUT passing max_part_size, so the
# 1 MiB default kicks in for every >1 MiB file. The constraint is enforced
# during FastAPI's parameter resolution, BEFORE our upload handler body runs,
# which is why our ``except Exception`` never catches it.
#
# The robust cross-version fix is to wrap ``MultiPartParser.__init__`` and
# inject our cap when callers omit it. ``setdefault`` keeps any caller-supplied
# value (e.g. unit tests) authoritative.
_original_multipart_init = MultiPartParser.__init__


@wraps(_original_multipart_init)
def _patched_multipart_init(self, headers, stream, *args, **kwargs):
    # Starlette uses ``max_part_size``; python-multipart also exposes
    # ``max_file_size``. We set both because the underlying class constants
    # evolved across versions and we want the cap lifted either way.
    kwargs.setdefault("max_part_size", MAX_UPLOAD_BYTES)
    kwargs.setdefault("max_file_size", MAX_UPLOAD_BYTES)
    return _original_multipart_init(self, headers, stream, *args, **kwargs)


MultiPartParser.__init__ = _patched_multipart_init


# --- Pure ASGI size guard ---------------------------------------------------------
#
# Even with the parser widened, we still want to short-circuit overly large
# payloads BEFORE they ever reach FastAPI parameter resolution so the browser
# gets a clean 413 instead of having the connection half-close mid-stream.
# This class implements the ASGI 3.0 triple ``(scope, receive, send)`` so it
# can be installed via ``app.add_middleware(_UploadSizeGuard, ...)``. Because
# Starlette wraps middleware in the reverse of ``add_middleware`` order, we
# register this AFTER CORS so it ends up as the outermost ran first on the
# request path.
class _UploadSizeGuard:
    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] != "POST" or scope["path"] != "/upload":
            await self.app(scope, receive, send)
            return

        cl_value: int = -1
        # HTTP header names are case-insensitive. ASGI 3.0 says names MUST be
        # lowercase, but we lowercase defensively so an upstream proxy that
        # forwards ``Content-Length`` mixed-case still triggers our 413.
        for k, v in scope["headers"]:
            if k.lower() == b"content-length":
                try:
                    cl_value = int(v.decode("ascii"))
                except (UnicodeDecodeError, ValueError):
                    cl_value = -1
                break

        if cl_value > self.max_bytes:
            logger.warning(
                "Rejecting oversize upload (ASGI guard): content_length=%d > max=%d",
                cl_value,
                self.max_bytes,
            )
            payload = json.dumps(
                {
                    "error": "upload_too_large",
                    "max_bytes": self.max_bytes,
                    "received": cl_value,
                }
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload})
            return

        await self.app(scope, receive, send)


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

    # Registered AFTER CORS so the size guard ends up as the outermost layer
    # on the request path (Starlette wraps middleware in reverse-of-add order).
    app.add_middleware(_UploadSizeGuard, max_bytes=MAX_UPLOAD_BYTES)

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
        }        # Synchronous workdir cleanup that swallows exceptions (logged).
        # Used both as a Starlette BackgroundTask after a successful response
        # is fully streamed, and synchronously inside every except clause
        # where no response object is yet available to attach a task to.
        def _safe_cleanup_workdir(work_root: Path) -> None:
            try:
                cleanup(work_root)
            except Exception:  # noqa: BLE001
                logger.exception("Cleanup of work directory failed")

    @app.post("/upload")
    async def upload(
        request: Request,
        file: UploadFile = File(..., description="Source .xapk archive"),
        suffix: str = Form(..., description="Suffix appended to the package name, e.g. _clone1"),
        new_name: Optional[str] = Form(None, description="Optional new visible app label"),
        new_icon: Optional[UploadFile] = File(None, description="Optional PNG icon"),
    ) -> Response:
        """Clone a XAPK file inside a workdir and return the result.

        Note: the ``Content-Length`` rejection logic used to live here, but it
        has moved to the ``_UploadSizeGuard`` ASGI middleware so the request
        is rejected BEFORE Starlette's multipart parser is invoked. The
        parser-widening ``__init__`` patch above means parts smaller than
        ``MAX_UPLOAD_BYTES`` parse cleanly.
        """
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

            # CRITICAL: cleanup is attached as a BackgroundTask so the workdir
            # is removed AFTER Starlette's FileResponse has streamed the clone
            # to the client. If we ran cleanup in a `finally` block, Python
            # would execute the cleanup BEFORE the response was sent, deleting
            # cloned.xapk before FileResponse.__call__ could os.stat() it.
            return FileResponse(
                path=str(result.output_path),
                media_type="application/octet-stream",
                filename=output_basename,
                headers={
                    "X-Original-Package": result.original_package,
                    "X-New-Package": result.new_package,
                    "X-New-Label": result.new_label,
                },
                background=BackgroundTask(_safe_cleanup_workdir, work_root),
            )

        # No `finally:` here — cleanup is delegated either to the
        # BackgroundTask (success path) or to explicit `_safe_cleanup_workdir`
        # calls in each except clause (error paths).
        except HTTPException:
            _safe_cleanup_workdir(work_root)
            raise
        except ValueError as exc:
            _safe_cleanup_workdir(work_root)
            logger.exception("Bad request")
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            _safe_cleanup_workdir(work_root)
            logger.exception("Processing failed")
            raise HTTPException(status_code=500, detail=f"Processing failed: {exc}")

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()

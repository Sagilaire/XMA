# XAPK Multi-Account Cloner

A dockerised web app that clones an Android **XAPK** under a new package name so
you can install two (or more) copies of the same app side-by-side — perfect
for running multiple accounts in parallel.

* **Frontend** — React + Vite, served by Nginx on port **3015**.
* **Backend** — FastAPI on port **4015**, driven by `apktool`, `zipalign`,
  `apksigner` and ImageMagick.

No auth. Drop a XAPK, choose a suffix, optionally rename the visible label or
replace the launcher icon, and download the cloned archive.

---

## Quick start

```bash
docker compose up --build
```

Open:

* UI:    http://localhost:3015
* API:   http://localhost:4015/health

The first build downloads and caches:

* `apktool.jar`  (~ a few MB)
* Android SDK command-line tools + `build-tools;34.0.0` (~ 100 MB)
* A debug.keystore (generated locally)

into the persistent `xapk_tools` named volume. Subsequent boots are
near-instant.

---

## How it works

1. The XAPK is uploaded via multipart POST to `POST /upload`. Each request is
   processed inside a per-request scratch directory under `/temp/workflows`.
2. The backend **extracts** the XAPK, picks the largest `*.apk` file as the
   "base APK" (config/split APKs and `.obb` files are kept as-is).
3. The base APK goes through `apktool d` (decompile). Two edits are made:
   * **`apktool.yml`** — `renameManifestPackage: <new>` is set so apktool
     delegates to `aapt --rename-manifest-package` at rebuild time. This
     rewrites only the binary manifest and keeps `R` class references intact,
     so the final APK installs as the new package without crashing at launch.
   * **`res/values/strings.xml`** (or the manifest's `android:label`) is
     updated with the new visible label.
4. If a new launcher icon is provided, the source PNG is re-encoded at every
   density (`mdpi`/`hdpi`/`xhdpi`/`xxhdpi`/`xxxhdpi`) using ImageMagick. A
   Pillow fallback is in place in case ImageMagick isn't installed.
5. `apktool b` rebuilds the APK.
6. **Every** APK inside the XAPK (base + splits) is **re-signed with the
   shared debug keystore** so the installer sees matching signatures across
   the whole split set.
7. The XAPK is rebuilt, swapping the modified base in while preserving
   original ZIP metadata (timestamps, Unix attributes) for the splits and the
   manifest.

The result is streamed back to the browser as a binary download via
`FileResponse`.

---

## Project layout

```
.
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── entrypoint.sh        # bootstraps /tools once, then exec uvicorn
│   └── app/
│       ├── main.py          # FastAPI app: /upload, /health, /tools/check
│       ├── xapk_processor.py
│       └── utils.py
├── frontend/
│   ├── Dockerfile
│   ├── default.conf         # nginx on port 3015; /api proxy is optional
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   ├── .env.example
│   └── src/
│       ├── main.jsx
│       ├── App.jsx
│       ├── index.css
│       └── components/Uploader.jsx
└── docker-compose.yml
```

---

## Configuring the API base URL

By default the SPA talks to `http://localhost:4015`. Override this at build
time:

```bash
VITE_API_BASE_URL=https://my.domain/api docker compose build frontend
docker compose up -d
```

For Nginx Proxy Manager, leave the build arg at `http://localhost:4015`
and proxy the API in NPM as you normally would. The nginx config inside the
frontend container also exposes a commented-out `/api/` -> `backend:4015`
proxy you can enable by editing `frontend/default.conf` if your NPM
forwards `/api` to the frontend container.

---

## Sizing limits

| Concern            | Limit        | Where                                |
|--------------------|--------------|--------------------------------------|
| Upload size        | 3 GiB        | FastAPI (`MAX_UPLOAD_BYTES`)         |
|                      |              | Nginx `client_max_body_size 3072m`   |
| Suffix             | 32 chars     | Regex `[A-Za-z0-9_]{1,32}`           |
| Icon               | PNG only     | Frontend + backend MIME check        |

---

## Notes on the implementation

* **Renaming strategy** — we deliberately **only** touch
  `apktool.yml`'s `renameManifestPackage` and never edit the
  `package="..."` attribute in the decompiled manifest. Editing the manifest
  attribute forces apktool to regenerate `R` classes under the new package
  while existing `smali` files keep referencing the old package's `R`
  classes — the resulting APK crashes on launch. Using
  `aapt --rename-manifest-package` rewrites the binary manifest only,
  leaving the compiled `R` references untouched, which is the safe option.
* **Multiple APKs in the XAPK** — every split APK is re-signed with the
  same debug keystore so matching signatures are presented to the Package
  Manager. Their binaries are preserved verbatim apart from the signing
  block.
* **`/tools` persistence** — the named volume `xapk_tools` is mounted at
  `/tools`. The entrypoint downloads `apktool.jar`, Android SDK
  command-line tools and a debug keystore lazily; once on disk they are
  reused on every restart.
* **ImageMagick crop syntax** — the naive `convert ... -crop 1:1` actually
  crops to a 1×1 pixel because `1:1` is interpreted as a literal size. We
  use `-resize WxH^` (oversized, aspect-preserving) followed by
  `-gravity center -crop WxH+0+0` so the output is exactly the desired
  square.

---

## Service endpoints (backend)

| Method & Path    | Purpose                                               |
|------------------|-------------------------------------------------------|
| `GET /health`    | Liveness                                               |
| `GET /tools/check` | Reports which tool artefacts are present under `/tools` |
| `POST /upload`   | Form fields `file`, `suffix`, `new_name`, `new_icon`   |

`POST /upload` returns:

* `200 OK` with the cloned `.xapk` as an attachment.
* Headers `X-Original-Package`, `X-New-Package`, `X-New-Label`.
* `413 Payload Too Large` if the body exceeds `MAX_UPLOAD_BYTES`.
* `4xx` / `5xx` JSON `{"detail": "..."}` for bad input / processing errors.

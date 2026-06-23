"""Utility helpers used by the XAPK processor."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("xapk.utils")

# Android launcher icon densities and their pixel sizes.
ICON_DENSITIES: dict[str, int] = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}

# ``drawable-<density>`` / ``mipmap-<density>`` -> pixel size for launcher icons.
DENSITY_PREFIX_SIZE: dict[str, int] = {
    f"{kind}-{name}": size
    for kind in ("drawable", "mipmap")
    for name, size in ICON_DENSITIES.items()
}

# Default size used for folders whose density name we don't recognise.
DEFAULT_ICON_SIZE = 96

# Common launcher icon file basenames.
ICON_BASENAMES: tuple[str, ...] = (
    "ic_launcher.png",
    "ic_launcher_round.png",
    "ic_launcher_foreground.png",
)


def run_command(
    cmd: list[str] | str,
    *,
    cwd: Optional[Path | str] = None,
    env: Optional[dict[str, str]] = None,
    timeout: int = 600,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command and return the CompletedProcess.

    Logs stderr on failure to ease debugging. ``check=True`` raises on error.
    """
    logger.info("Running: %s", " ".join(cmd) if isinstance(cmd, list) else cmd)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        shell=isinstance(cmd, str),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        logger.error("Command failed (%s): %s", proc.returncode, proc.stderr or proc.stdout)
        if check:
            raise RuntimeError(
                f"Command `{cmd if isinstance(cmd, str) else ' '.join(cmd)}` "
                f"failed with code {proc.returncode}: {(proc.stderr or proc.stdout)[:1500]}"
            )
    return proc


def safe_rmtree(path: Path | str, ignore_errors: bool = True) -> None:
    """Recursively delete a directory tree without raising if missing."""
    shutil.rmtree(path, ignore_errors=ignore_errors)


def is_safe_suffix(suffix: str) -> bool:
    """Suffix must be a small alphanumeric token to avoid breaking package names."""
    if not suffix:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_]{1,32}", suffix))


def normalize_new_package(original_package: str, suffix: str) -> str:
    """Append the suffix to the original package name in a safe way."""
    if not original_package:
        return original_package
    if not suffix.startswith("_"):
        suffix = f"_{suffix}"
    return f"{original_package}{suffix}"


def choose_base_apk(entries_with_sizes: list[tuple[str, int]]) -> str:
    """Pick the base/main APK from a list of ``(name, size)`` tuples.

    Strategy, in priority order:

    1. Largest ``*.apk`` file (base APK is usually the biggest).
    2. First entry ending with ``.apk`` not containing "config" or "split".
    3. First entry ending with ``.apk``.
    """
    apk_entries = [
        (name, size) for name, size in entries_with_sizes
        if name.lower().endswith(".apk") and not name.endswith("/")
    ]
    if not apk_entries:
        raise ValueError("No APK file found inside the XAPK archive.")

    # Filter out split/config APKs first.
    main_candidates = [
        name for name, _ in apk_entries
        if "config" not in name.lower() and "split" not in name.lower()
    ] or [name for name, _ in apk_entries]

    # Sort candidates: prefer the largest size first, then by length of filename
    # (long names often encode the full package, e.g. ``com.example.app.apk``).
    sizes_by_name = dict(apk_entries)
    return sorted(main_candidates, key=lambda n: (sizes_by_name[n], len(n)), reverse=True)[0]


def resize_icon_for_density(
    source_image: Path | str,
    output_image: Path | str,
    size: int,
) -> None:
    """Render ``source_image`` as a square PNG of the requested pixel size.

    ImageMagick incantation:

    1. ``-resize WxH^`` inflates the smaller dimension to ``H`` while preserving
       aspect ratio (the ``^`` flag is the key).
    2. ``-gravity center -crop WxH+0+0`` then takes the centered crop so the
       output is exactly ``W×H``.

    This works correctly for both square and non-square source images. A
    ``-gravity center -crop 1:1`` invocation (the obvious alternative)
    actually produces a 1×1 pixel in ImageMagick 6 — ``1:1`` is interpreted
    as the literal crop size, not an aspect ratio.
    """
    source_image = str(source_image)
    output_image = str(output_image)
    try:
        run_command(
            [
                "convert",
                source_image,
                "-background", "none",
                "-resize", f"{size}x{size}^",
                "-gravity", "center",
                "-crop", f"{size}x{size}+0+0",
                "+repage",
                "-strip",
                output_image,
            ],
            check=True,
        )
        return
    except (RuntimeError, FileNotFoundError):
        logger.warning("ImageMagick unavailable; falling back to Pillow for %s", output_image)

    # Pillow fallback: crop to a centered square, then resize.
    from PIL import Image  # imported lazily so the dep is optional elsewhere

    with Image.open(source_image) as im:
        im = im.convert("RGBA")
        side = min(im.size)
        left = (im.size[0] - side) // 2
        top = (im.size[1] - side) // 2
        im = im.crop((left, top, left + side, top + side))
        im = im.resize((size, size), Image.LANCZOS)
        im.save(output_image, format="PNG")


def replace_icons_in_res(
    decoded_dir: Path | str,
    icon_source: Path | str,
) -> int:
    """Replace the launcher icon PNGs in every ``res/drawable-*`` / ``res/mipmap-*`` folder.

    Returns the number of icons successfully produced.
    """
    decoded_dir = Path(decoded_dir)
    res_dir = decoded_dir / "res"
    if not res_dir.is_dir():
        logger.warning("No res/ directory in decoded APK at %s", decoded_dir)
        return 0

    produced = 0
    for folder in sorted(res_dir.iterdir()):
        if not folder.is_dir():
            continue
        if not (folder.name.startswith("drawable-") or folder.name.startswith("mipmap-")):
            continue

        target_size = DENSITY_PREFIX_SIZE.get(folder.name, DEFAULT_ICON_SIZE)

        existing = {p.name: p for p in folder.iterdir() if p.is_file()}

        for basename in ICON_BASENAMES:
            target = existing.get(basename)
            if target is None or target.suffix.lower() != ".png":
                continue
            try:
                resize_icon_for_density(icon_source, target, target_size)
                produced += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to write icon %s: %s", target, exc)

    return produced


def xapk_entries_with_sizes(xapk_path: Path | str) -> list[tuple[str, int]]:
    """List ``(entry_name, uncompressed_size)`` for every file in the archive."""
    with zipfile.ZipFile(xapk_path, "r") as zf:
        return [
            (info.filename, info.file_size)
            for info in zf.infolist()
            if not info.is_dir()
        ]


def repack_xapk(
    source_xapk: Path | str,
    output_xapk: Path | str,
    replacements: dict[str, Path | str],
    additional_replacements: Optional[Iterable[tuple[str, Path | str]]] = None,
) -> None:
    """Rebuild a XAPK archive, swapping in replacement files for matching entries.

    ``replacements`` maps the original entry name -> new file on disk.
    ``additional_replacements`` is an iterable of ``(entry_name, path)`` pairs;
    entries not present in the source are *added* (used for the rare case
    where the rebuild produces a new file not in the original manifest).

    All metadata (modification time, external attributes such as Unix
    permissions) is preserved by passing each original ``ZipInfo`` to
    ``writestr`` rather than rebuilding a fresh archive header.
    """
    source_xapk = Path(source_xapk)
    output_xapk = Path(output_xapk)

    extra = list(additional_replacements or [])
    extra_names = {name for name, _ in extra}

    with zipfile.ZipFile(source_xapk, "r") as src:
        with zipfile.ZipFile(output_xapk, "w") as dst:
            for info in src.infolist():
                if info.is_dir():
                    continue

                if info.filename in replacements:
                    dst.write(
                        str(replacements[info.filename]),
                        arcname=info.filename,
                        compress_type=info.compress_type,
                    )
                else:
                    # Preserve the original ZipInfo (date, attributes, internal attrs).
                    with src.open(info.filename) as fp:
                        data = fp.read()
                    new_info = zipfile.ZipInfo(
                        filename=info.filename,
                        date_time=info.date_time,
                    )
                    new_info.compress_type = info.compress_type
                    new_info.external_attr = info.external_attr
                    new_info.create_system = info.create_system
                    new_info.internal_attr = info.internal_attr
                    new_info.flag_bits = info.flag_bits & ~0x0800  # drop "language flag" bit
                    dst.writestr(new_info, data)

            # Add any extra entries not present in the original.
            for name, path in extra:
                if name in extra_names and not any(i.filename == name for i in src.infolist()):
                    dst.write(str(path), arcname=name)


def ensure_dir(path: Path | str) -> Path:
    """Create *path* (including parents) and return it as a Path."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def file_size_human(num_bytes: int) -> str:
    """Format a byte count as a human-readable string."""
    if num_bytes is None or num_bytes < 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def read_text(path: Path | str) -> str:
    """Read a UTF-8 text file."""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def write_text(path: Path | str, content: str) -> None:
    """Write a UTF-8 text file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def derive_display_name(original_name: str, suffix: str, new_name: Optional[str]) -> str:
    """Pick the user-supplied new name or append the suffix to the original one."""
    if new_name:
        return new_name.strip()
    if original_name:
        return f"{original_name} {suffix.strip('_')}".strip()
    return "Cloned App"


def cleanup_workdir(workdir: Path | str) -> None:
    """Remove a processing work directory but never the /tools directory."""
    workdir = Path(workdir)
    resolved = str(workdir.resolve())
    protected = {"/tools", str(Path("/tools").resolve()), "/temp", str(Path("/temp").resolve())}
    if resolved in protected:
        # Never wipe whole mount points — only sub-directories under /temp.
        return
    if workdir.exists():
        safe_rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)


__all__ = [
    "DEFAULT_ICON_SIZE",
    "DENSITY_PREFIX_SIZE",
    "ICON_BASENAMES",
    "ICON_DENSITIES",
    "choose_base_apk",
    "cleanup_workdir",
    "derive_display_name",
    "ensure_dir",
    "file_size_human",
    "is_safe_suffix",
    "normalize_new_package",
    "read_text",
    "replace_icons_in_res",
    "repack_xapk",
    "resize_icon_for_density",
    "run_command",
    "safe_rmtree",
    "write_text",
    "xapk_entries_with_sizes",
]

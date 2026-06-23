"""Core XAPK processing pipeline.

Pipeline:

    XAPK -> extract -> find base APK
        -> apktool decode base APK
        -> set renameManifestPackage in apktool.yml
        -> mutate manifest label (and res strings)
        -> optionally replace launcher icons
        -> apktool build
        -> zipalign + apksigner base APK
        -> zipalign + apksigner every split APK (same keystore!)
        -> repack XAPK with original splits/OBBs preserved

Why we only touch ``apktool.yml`` (not the ``package=`` attribute in the
decompiled AndroidManifest.xml):

    When apktool rebuilds with ``renameManifestPackage`` set, it delegates to
    ``aapt --rename-manifest-package``. aapt then rewrites the binary manifest
    with the new package but leaves the compiled R class files (and smali R
    references) at their original package. That is exactly what we want — the
    final APK installs under the new package and the existing ``R$*`` static
    references keep working.

    The alternative — modifying ``package="..."`` in the decompiled manifest —
    forces apktool to regenerate the R class with the new package, while smali
    files in the same tree still reference ``L<oldpkg>/R$id;``. That ships an
    APK that crashes on launch with ``NoClassDefFoundError``.

Tools required (populated by ``entrypoint.sh``):

* ``/tools/apktool.jar``             — invoked as ``java -jar apktool.jar``
* ``/tools/android-sdk/build-tools/<v>/{zipalign,apksigner}``
                                       (apksigner ≥ build-tools 32 is a Python
                                       wrapper script that needs sibling .jar;
                                       keep it inside its build-tools dir).
* ``/tools/debug.keystore``           — generated if missing.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .utils import (
    choose_base_apk,
    cleanup_workdir,
    derive_display_name,
    ensure_dir,
    is_safe_suffix,
    normalize_new_package,
    read_text,
    replace_icons_in_res,
    repack_xapk,
    run_command,
    write_text,
    xapk_entries_with_sizes,
)

logger = logging.getLogger("xapk.processor")

# --- Tool locations (resolved against TOOLS_DIR, default /tools) --------------------

TOOLS_DIR = Path(os.environ.get("TOOLS_DIR", "/tools"))
APKTOOL_JAR = TOOLS_DIR / "apktool.jar"
DEBUG_KEYSTORE = TOOLS_DIR / "debug.keystore"


# --- Result object ------------------------------------------------------------------


@dataclass
class ProcessResult:
    """Outcome of cloning a XAPK."""

    output_path: Path
    new_package: str
    new_label: str
    original_package: str
    original_label: str
    bytes_written: int
    base_apk_name: str


# --- Tool resolution -----------------------------------------------------------------


def _find_build_tools_dir() -> Path:
    """Locate the latest installed build-tools directory under /tools."""
    sdk_root = TOOLS_DIR / "android-sdk" / "build-tools"
    if not sdk_root.is_dir():
        raise FileNotFoundError(
            "Android build-tools missing under /tools/android-sdk/build-tools. "
            "Did the container entrypoint run successfully?"
        )
    versions = sorted((p for p in sdk_root.iterdir() if p.is_dir()), reverse=True)
    if not versions:
        raise FileNotFoundError("No Android build-tools versions installed.")
    return versions[0]


def _zipalign_bin() -> Path:
    """Return the zipalign executable inside the build-tools directory."""
    return _find_build_tools_dir() / "zipalign"


def _apksigner_bin() -> Path:
    """Return the apksigner executable inside the build-tools directory.

    Recent build-tools ship apksigner as a script wrapper that depends on
    sibling ``apksigner.jar``. We therefore resolve the helper that lives next
    to its jar — never copy just the wrapper to /tools.
    """
    return _find_build_tools_dir() / "apksigner"


# --- Public entry point --------------------------------------------------------------


def clone_xapk(
    xapk_input_path: Path | str,
    work_root: Path | str,
    suffix: str,
    new_name: Optional[str] = None,
    icon_input_path: Optional[Path | str] = None,
) -> ProcessResult:
    """Clone a XAPK file applying the given transformations.

    Parameters
    ----------
    xapk_input_path
        Path to the source ``.xapk`` archive.
    work_root
        A directory used for intermediate work. Must not be ``/tools``.
    suffix
        Token appended to the package name (e.g. ``_clone1``).
    new_name
        Optional new label for the app; falls back to ``"<label> <suffix>"``.
    icon_input_path
        Optional PNG image to use as the new launcher icon.
    """
    if not is_safe_suffix(suffix):
        raise ValueError(
            "Invalid suffix. Use only letters, digits and underscores (max 32 chars)."
        )

    xapk_input_path = Path(xapk_input_path)
    work_root = Path(work_root)
    ensure_dir(work_root)

    extract_dir = work_root / "extract"
    decoded_dir = work_root / "decoded"
    rebuilt_path = work_root / "rebuilt.apk"
    final_xapk_path = work_root / "cloned.xapk"

    for path in (extract_dir, decoded_dir):
        if path.exists():
            shutil.rmtree(path)
    # Reset intermediate APKs.
    for stale in (rebuilt_path, final_xapk_path):
        if stale.exists():
            stale.unlink()

    extract_dir.mkdir(parents=True)

    # --- Step 1: extract -------------------------------------------------------
    logger.info("Extracting XAPK %s -> %s", xapk_input_path, extract_dir)
    with zipfile.ZipFile(xapk_input_path, "r") as zf:
        zf.extractall(extract_dir)

    # --- Step 2: choose base APK ----------------------------------------------
    entries = xapk_entries_with_sizes(xapk_input_path)
    base_apk_rel = choose_base_apk(entries)
    base_apk_path = extract_dir / base_apk_rel
    if not base_apk_path.is_file():
        raise FileNotFoundError(f"Detected base APK `{base_apk_rel}` is not a file.")
    logger.info("Selected base APK: %s", base_apk_rel)

    # --- Step 3: apktool decode ----------------------------------------------
    logger.info("Running apktool decode on %s", base_apk_path)
    run_command(
        [
            "java",
            "-jar",
            str(APKTOOL_JAR),
            "d",
            "-f",
            str(base_apk_path),
            "-o",
            str(decoded_dir),
        ]
    )

    # --- Step 4: read original metadata ---------------------------------------
    android_manifest = decoded_dir / "AndroidManifest.xml"
    apktool_yml = decoded_dir / "apktool.yml"
    strings_xml = (
        decoded_dir / "res" / "values" / "strings.xml"
        if (decoded_dir / "res" / "values" / "strings.xml").is_file()
        else None
    )

    original_manifest = read_text(android_manifest)
    original_package = _extract_package_attr(original_manifest)
    original_label = _extract_label_value(original_manifest, strings_xml)
    if not original_package:
        raise RuntimeError("Could not determine the original package name from the manifest.")

    new_package = normalize_new_package(original_package, suffix)
    new_label = derive_display_name(original_label, suffix, new_name)
    logger.info("Original pkg/label: %s / %s", original_package, original_label)
    logger.info("Cloning to pkg/label: %s / %s", new_package, new_label)

    # --- Step 5: package rename via apktool.yml + safe -----------------------
    # See module docstring for the rationale.
    _set_rename_manifest_package(apktool_yml, new_package)

    # --- Step 6: replace the visible label -----------------------------------
    _replace_label(android_manifest, new_label, strings_xml)

    # --- Step 7: icon replacement (optional) ----------------------------------
    if icon_input_path is not None:
        logger.info("Replacing launcher icons using %s", icon_input_path)
        replace_icons_in_res(decoded_dir, icon_input_path)

    # --- Step 8: rebuild base APK ---------------------------------------------
    logger.info("Running apktool build -> %s", rebuilt_path)
    run_command(
        [
            "java",
            "-jar",
            str(APKTOOL_JAR),
            "b",
            str(decoded_dir),
            "-o",
            str(rebuilt_path),
        ]
    )

    # --- Step 9: sign all APKs with the same keystore -------------------------
    # The base APK went through apktool, so we zipalign + sign it.
    # Split APKs are kept as-is but must be re-signed with our keystore so the
    # final install sees a consistent signature across all APKs in the set.
    signed_base = _zipalign_and_sign(rebuilt_path, work_root / "signed-base.apk")
    logger.info("Base APK signed: %s", signed_base)

    signed_splits: dict[str, Path] = {base_apk_rel: signed_base}
    for rel, size in entries:
        if not rel.lower().endswith(".apk") or rel == base_apk_rel:
            continue
        original_on_disk = extract_dir / rel
        if not original_on_disk.is_file():
            continue
        signed_split_path = work_root / f"split-{Path(rel).stem}.apk"
        signed_splits[rel] = _zipalign_and_sign(original_on_disk, signed_split_path)

    # --- Step 10: repack ------------------------------------------------------
    final_xapk_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Repacking XAPK -> %s", final_xapk_path)
    repack_xapk(
        source_xapk=xapk_input_path,
        output_xapk=final_xapk_path,
        replacements=signed_splits,
    )

    return ProcessResult(
        output_path=final_xapk_path,
        new_package=new_package,
        new_label=new_label,
        original_package=original_package,
        original_label=original_label,
        bytes_written=final_xapk_path.stat().st_size,
        base_apk_name=base_apk_rel,
    )


# --- Pipeline helpers ----------------------------------------------------------------


def _zipalign_and_sign(input_apk: Path, output_apk: Path) -> Path:
    """zipalign the APK then sign it with our debug keystore."""
    aligned = input_apk.with_suffix(".aligned.apk")
    run_command(
        [str(_zipalign_bin()), "-p", "-f", "4", str(input_apk), str(aligned)],
        check=True,
    )
    run_command(
        [
            str(_apksigner_bin()),
            "sign",
            "--ks",
            str(DEBUG_KEYSTORE),
            "--ks-pass",
            "pass:android",
            "--key-pass",
            "pass:android",
            "--ks-key-alias",
            "androiddebugkey",
            "--out",
            str(output_apk),
            str(aligned),
        ],
        check=True,
    )
    # Clean up the intermediate aligned copy.
    try:
        aligned.unlink()
    except OSError:
        pass
    return output_apk


def cleanup(work_root: Path | str) -> None:
    """Remove all temporary files (NOT /tools)."""
    cleanup_workdir(work_root)


def get_tool_paths() -> dict[str, Optional[Path]]:
    """Resolve every tool artefact at runtime; safe for the ``/tools/check`` endpoint."""
    bt_root = TOOLS_DIR / "android-sdk" / "build-tools"
    latest: Optional[Path] = None
    if bt_root.is_dir():
        versions = sorted((p for p in bt_root.iterdir() if p.is_dir()), reverse=True)
        latest = versions[0] if versions else None
    return {
        "apktool_jar": APKTOOL_JAR if APKTOOL_JAR.is_file() else None,
        "zipalign": (latest / "zipalign") if latest else None,
        "apksigner": (latest / "apksigner") if latest else None,
        "debug_keystore": DEBUG_KEYSTORE if DEBUG_KEYSTORE.is_file() else None,
    }


# --- Regex / mutation helpers --------------------------------------------------------

_PACKAGE_RE = re.compile(r'(package\s*=\s*")[^"]*(")')
# Single regex for ``android:label="..."`` OR ``android:label='...'``.
_LABEL_RE = re.compile(
    r'(android:label\s*=\s*")([^"]*)(")|(android:label\s*=\s*\')([^\']*)(\')'
)


def _extract_package_attr(manifest_text: str) -> Optional[str]:
    m = _PACKAGE_RE.search(manifest_text)
    if not m:
        return None
    # groups: 1=opening quote, 2=closing quote; value sits between them.
    return manifest_text[m.start():m.end()].split('"')[1]


def _set_rename_manifest_package(apktool_yml_path: Path, new_package: str) -> None:
    """Write ``renameManifestPackage: <new_pkg>`` into apktool.yml.

    * If the key already exists, replace its value.
    * Otherwise inject it under ``packageInfo:`` if that block exists,
      else append at the end of the file.
    """
    if not apktool_yml_path.is_file():
        logger.warning(
            "apktool.yml missing at %s; rebuild will use the manifest package as-is.",
            apktool_yml_path,
        )
        return

    text = read_text(apktool_yml_path)
    line_re = re.compile(r"^renameManifestPackage:.*$", flags=re.MULTILINE)

    if line_re.search(text):
        text = line_re.sub(f"renameManifestPackage: {new_package}", text)
    elif re.search(r"^packageInfo:", text, flags=re.MULTILINE):
        text = re.sub(
            r"^(packageInfo:.*$)",
            rf"\1\n  renameManifestPackage: {new_package}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        text = text.rstrip() + f"\nrenameManifestPackage: {new_package}\n"

    write_text(apktool_yml_path, text)
    logger.info("Set apktool.yml renameManifestPackage -> %s", new_package)


def _extract_label_value(
    manifest_text: str,
    strings_xml_path: Optional[Path],
) -> str:
    """Resolve ``android:label`` (looking up ``@string/...`` references)."""
    m = _LABEL_RE.search(manifest_text)
    if not m:
        return ""

    # The regex has two alternatives; pick the one that matched.
    label_value = m.group(2) or m.group(5) or ""

    if label_value.startswith("@string/") and strings_xml_path and strings_xml_path.is_file():
        ref = label_value.split("/", 1)[1]
        strings_text = read_text(strings_xml_path)
        ref_match = re.search(
            rf"<string\s+name=\"{re.escape(ref)}\"\s*>([^<]+)</string>",
            strings_text,
        )
        if ref_match:
            return ref_match.group(1).strip()

    return label_value


def _replace_label(
    manifest_path: Path,
    new_label: str,
    strings_xml_path: Optional[Path],
) -> None:
    """Update the app's visible label.

    Two cases:

    * If ``android:label="@string/<ref>"``: update the matching string in
      ``strings.xml`` so the new label flows through resource resolution.
    * Otherwise: rewrite the literal value of ``android:label`` inline.

    Uses a closure to capture ``new_label`` cleanly (no module globals).
    """
    manifest_text = read_text(manifest_path)
    match = _LABEL_RE.search(manifest_text)
    if not match:
        logger.warning("Manifest has no android:label attribute; leaving label untouched.")
        return

    current_raw = match.group(2) or match.group(5) or ""

    def _replace_manifest_label(m: re.Match[str]) -> str:
        # Branch for double-quoted alternative (groups 1,2,3).
        if m.group(2) is not None:
            return f"{m.group(1)}{new_label}{m.group(3)}"
        # Branch for single-quoted alternative (groups 4,5,6).
        if m.group(5) is not None:
            return f"{m.group(4)}{new_label}{m.group(6)}"
        return m.group(0)

    if current_raw.startswith("@string/") and strings_xml_path is not None and strings_xml_path.is_file():
        ref = current_raw.split("/", 1)[1]
        strings_text = read_text(strings_xml_path)

        def _replace_string_entry(m: re.Match[str]) -> str:
            return f"{m.group(1)}{new_label}{m.group(3)}"

        new_strings, count = re.subn(
            rf'(<string\s+name="{re.escape(ref)}"\s*>)([^<]*)(</string>)',
            _replace_string_entry,
            strings_text,
            count=1,
        )

        if count:
            write_text(strings_xml_path, new_strings)
            logger.info("Updated res/values/strings.xml [%s] -> %s", ref, new_label)
            return

        logger.warning(
            "android:label references @string/%s but the key was missing; "
            "overriding android:label inline instead.",
            ref,
        )

    new_manifest = _LABEL_RE.sub(_replace_manifest_label, manifest_text, count=1)
    write_text(manifest_path, new_manifest)
    logger.info("Updated AndroidManifest.xml android:label -> %s", new_label)

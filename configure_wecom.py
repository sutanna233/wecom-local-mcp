"""Detect the local WXWork Data directory and write the collector config."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "third_party" / "wecom-decrypt" / "config.json"
DEFAULTS = {
    "wxwork_keys_file": "wxwork_keys.json",
    "wxwork_decrypted_dir": "wxwork_decrypted",
    "wxwork_export_dir": "wxwork_export",
    "wxwork_process": "WXWork.exe",
}
PLACEHOLDERS = ("ACCOUNT_ID", "Path\\To", "Path/To", "your_wxid")


def _known_documents() -> Path | None:
    """Return the redirected Windows Documents folder without shelling out."""
    if os.name != "nt":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    # CSIDL_PERSONAL = 5; SHGFP_TYPE_CURRENT = 0.
    if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) != 0:
        return None
    return Path(buffer.value) if buffer.value else None


def _add_path(items: list[Path], seen: set[str], value: str | Path | None) -> None:
    if not value:
        return
    path = Path(value).expanduser()
    key = os.path.normcase(os.path.normpath(str(path)))
    if key not in seen:
        seen.add(key)
        items.append(path)


def search_roots(
    environ: Mapping[str, str] | None = None,
    *,
    documents: str | Path | None = None,
    extra_roots: Iterable[str | Path] = (),
) -> list[Path]:
    """Build a bounded list of common WXWork data roots, in priority order."""
    env = os.environ if environ is None else environ
    roots: list[Path] = []
    seen: set[str] = set()
    _add_path(roots, seen, env.get("WECOM_WXWORK_ROOT"))
    for root in extra_roots:
        _add_path(roots, seen, root)

    document_dirs: list[Path] = []
    document_seen: set[str] = set()
    _add_path(document_dirs, document_seen, documents)
    _add_path(document_dirs, document_seen, _known_documents() if documents is None else None)
    profile = env.get("USERPROFILE", "")
    if profile:
        _add_path(document_dirs, document_seen, Path(profile) / "Documents")
    for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        base = env.get(name, "")
        if base:
            _add_path(document_dirs, document_seen, Path(base) / "Documents")
            _add_path(document_dirs, document_seen, Path(base) / "文档")
    for documents_dir in document_dirs:
        _add_path(roots, seen, documents_dir / "WXWork")
        _add_path(roots, seen, documents_dir / "WeCom")

    # These locations cover portable/custom installations without a recursive
    # scan of AppData or an entire drive.
    for name in ("APPDATA", "LOCALAPPDATA"):
        base = env.get(name, "")
        if base:
            _add_path(roots, seen, Path(base) / "WXWork")
            _add_path(roots, seen, Path(base) / "WeCom")
    if os.name == "nt" and environ is None:
        for drive in ("D:", "E:"):
            for name in ("WXWork", "WeCom"):
                _add_path(roots, seen, Path(drive + os.sep) / name)
            for documents_dir in ("Documents", "Backup\\Documents"):
                _add_path(roots, seen, Path(drive + os.sep) / documents_dir / "WXWork")
                _add_path(roots, seen, Path(drive + os.sep) / documents_dir / "WeCom")
    return roots


def discover_data_dirs(
    environ: Mapping[str, str] | None = None,
    *,
    documents: str | Path | None = None,
    extra_roots: Iterable[str | Path] = (),
) -> list[Path]:
    env = os.environ if environ is None else environ
    candidates: list[Path] = []
    seen: set[str] = set()

    direct = env.get("WECOM_DATA_DIR", "")
    if direct and Path(direct).is_dir():
        _add_path(candidates, seen, Path(direct).resolve())

    for root in search_roots(env, documents=documents, extra_roots=extra_roots):
        try:
            if not root.is_dir():
                continue
            if root.name.casefold() == "data":
                _add_path(candidates, seen, root.resolve())
                continue
            children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            continue
        for account in children:
            try:
                data_dir = account / "Data"
                if account.is_dir() and data_dir.is_dir():
                    _add_path(candidates, seen, data_dir.resolve())
            except OSError:
                continue
    return candidates


def _activity(path: Path) -> tuple[int, float]:
    latest = 0.0
    has_db = 0
    try:
        latest = path.stat().st_mtime
        for current, dirs, files in os.walk(path):
            dirs[:] = [name for name in dirs if name != "-journal"]
            for name in files:
                if not name.lower().endswith((".db", ".db-wal", ".db-shm")):
                    continue
                has_db = 1
                try:
                    latest = max(latest, (Path(current) / name).stat().st_mtime)
                except OSError:
                    pass
    except OSError:
        pass
    return has_db, latest


def choose_data_dir(candidates: Iterable[str | Path]) -> Path | None:
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        _add_path(unique, seen, Path(candidate).resolve())
    if not unique:
        return None
    return max(unique, key=lambda path: (_activity(path), -unique.index(path)))


def _valid_existing_path(config_path: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if any(marker.casefold() in value.casefold() for marker in PLACEHOLDERS):
        return None
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    if not expanded.is_absolute():
        expanded = config_path.parent / expanded
    return expanded.resolve() if expanded.is_dir() else None


def ensure_config(
    config_path: Path,
    *,
    data_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    documents: str | Path | None = None,
    extra_roots: Iterable[str | Path] = (),
) -> tuple[Path, bool]:
    """Ensure config_path names an existing Data directory.

    Returns (selected_data_dir, automatically_detected). Existing valid paths
    win over detection; an explicit data_dir wins over both.
    """
    config_path = config_path.resolve()
    config: dict = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8-sig") as stream:
            loaded = json.load(stream)
        if not isinstance(loaded, dict):
            raise ValueError(f"configuration must be a JSON object: {config_path}")
        config = loaded

    selected: Path | None = None
    detected = False
    if data_dir:
        selected = Path(data_dir).expanduser().resolve()
        if not selected.is_dir():
            raise FileNotFoundError(f"WXWork Data directory does not exist: {selected}")
    else:
        selected = _valid_existing_path(config_path, config.get("wxwork_db_dir"))
        if selected is None:
            candidates = discover_data_dirs(
                environ, documents=documents, extra_roots=extra_roots
            )
            selected = choose_data_dir(candidates)
            detected = selected is not None
    if selected is None:
        raise FileNotFoundError(
            "WXWork Data directory was not found. Set WECOM_DATA_DIR, or run "
            f"{Path(__file__).name} --config \"{config_path}\" --data-dir \"D:\\WXWork\\ACCOUNT_ID\\Data\"."
        )

    for key, value in DEFAULTS.items():
        config.setdefault(key, value)
    config["wxwork_db_dir"] = str(selected)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(config_path.name + ".tmp")
    temporary.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, config_path)
    return selected, detected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args()
    try:
        selected, detected = ensure_config(
            args.config, data_dir=args.data_dir
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"WECOM_CONFIG_ERROR={exc}", file=sys.stderr)
        return 2
    print(f"WECOM_DATA_DIR={selected}")
    print(f"WECOM_CONFIG={args.config.resolve()}")
    print(f"WECOM_AUTO_DETECTED={int(detected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

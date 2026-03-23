import hashlib
import os
import zipfile
from pathlib import Path
from typing import Optional

def build_rom_search_index(*roots: Path):
    index = {
        "files_by_name": {},
        "dirs_by_name": {},
        "files_by_stem": {},
    }

    for root in roots:
        if not root:
            continue
        try:
            root = Path(root)
        except Exception:
            continue
        if not root.exists() or not root.is_dir():
            continue

        for dirpath, dirnames, filenames in os.walk(str(root)):
            try:
                dp = Path(dirpath)
            except Exception:
                continue

            for d in dirnames:
                if d not in index["dirs_by_name"]:
                    index["dirs_by_name"][d] = dp / d

            for f in filenames:
                if f not in index["files_by_name"]:
                    p = dp / f
                    index["files_by_name"][f] = p
                    stem = p.stem
                    index["files_by_stem"].setdefault(stem, []).append(p)

    return index


def resolve_local_rom_path(
    game: dict,
    config_data: dict,
    search_index: Optional[dict] = None,
    prefer_m3u_for_multi: bool = False,
) -> Optional[Path]:
    """
    Robustly find a ROM on disk using multiple strategies:
    1. Check base_rom_path / platform / filename
    2. Check base_rom_path / filename
    3. Fuzzy extension matching (.chd, .iso, .z64, etc)
    4. PS3/Folder-based fallback: base_rom_path / platform / folder_name
    5. Recursive search in base_rom_path (last resort)
    6. Windows-specific: base_rom_path / windows / folder_name (from filename stem)
    """
    from pathlib import Path
    import os
    import re
    
    platform = game.get('platform_slug')
    rom_name = game.get('fs_name')
    files = game.get('files') or []
    file_names = []
    file_obj_name = None
    if files and isinstance(files, list):
        for f in files:
            if isinstance(f, dict):
                n = f.get('file_name')
                if n:
                    file_names.append(n)
        if file_names:
            file_obj_name = file_names[0]
    if not rom_name and not file_obj_name:
        return None
        
    # Windows Native Logic
    is_windows = platform in ["windows", "win", "pc", "pc-windows", "windows-games", "win95", "win98"]
    if is_windows:
        base_rom = config_data.get("base_rom_path")
        wd = str(Path(base_rom) / "windows") if base_rom else ""
        if wd:
            candidates = []
            if file_obj_name:
                candidates.append(file_obj_name)
            if rom_name and rom_name not in candidates:
                candidates.append(rom_name)

            for name in candidates:
                # Check for folder named after archive stem (standard Wingosy Windows install)
                folder = Path(wd) / Path(name).stem
                if folder.exists() and folder.is_dir():
                    return folder

                # Check direct file if not a folder-based install
                direct = Path(wd) / name
                if direct.exists():
                    return direct

    # Standard Emulator ROM Logic
    base_rom = config_data.get("base_rom_path")
    if not base_rom:
        return None
    
    base_path = Path(base_rom)
    stem = Path(rom_name or file_obj_name).stem
    
    multi_file_game = len(file_names) > 1
    prefer_m3u = bool(prefer_m3u_for_multi and multi_file_game and not is_windows)

    # Exclusion list: .cue files are often metadata and not what we want to launch/hash
    excluded_exts = {'.cue'}

    def is_excluded(p: Path) -> bool:
        return p.suffix.lower() in excluded_exts

    exact_candidates = []
    for n in [rom_name, *file_names]:
        if n and n not in exact_candidates:
            exact_candidates.append(n)

    if prefer_m3u:
        m3u_candidates = [n for n in exact_candidates if Path(n).suffix.lower() == '.m3u']
        non_m3u_candidates = [n for n in exact_candidates if Path(n).suffix.lower() != '.m3u']
        exact_candidates = m3u_candidates + non_m3u_candidates

    # 1-2. Exact matches (platform subdir first, then base)
    for name in exact_candidates:
        if platform:
            p_platform = base_path / platform / name
            if p_platform.exists() and not is_excluded(p_platform):
                return p_platform

        p_base = base_path / name
        if p_base.exists() and not is_excluded(p_base):
            return p_base

    # 3. Fuzzy extension matching fallbacks
    # Common disc and ROM formats: .chd, .iso, .cso, .pbp, .bin, .img, .mdf, .z64, .n64, .v64
    extensions = ['.chd', '.iso', '.cso', '.pbp', '.bin', '.img', '.mdf', '.z64', '.n64', '.v64']
    for ext in extensions:
        if ext in excluded_exts: continue
        candidate = stem + ext
        if platform:
            p_cand = base_path / platform / candidate
            if p_cand.exists(): return p_cand
        p_cand = base_path / candidate
        if p_cand.exists(): return p_cand

    # 4. PS3/Folder-based fallback (e.g. RPCS3 games stored as folders)
    if platform:
        p_folder = base_path / platform / stem
        if p_folder.exists() and p_folder.is_dir():
            return p_folder

    # 4b. Extracted-file fallback: look for any file with matching stem in the most likely folders.
    # This helps when a downloaded archive (zip/7z) contains a ROM with a different extension than fs_name.
    scan_dirs = []
    if platform:
        scan_dirs.append(base_path / platform)
    scan_dirs.append(base_path)

    for d in scan_dirs:
        if not d.exists() or not d.is_dir():
            continue
        try:
            for p in d.glob(stem + ".*"):
                if p.exists() and p.is_file() and not is_excluded(p):
                    return p
        except Exception:
            pass
        
    # 5. Recursive Search (v0.5.7 legacy fallback)
    # Only do this if base_rom is a valid directory to avoid hangs
    if search_index:
        try:
            p_dir = search_index.get("dirs_by_name", {}).get(stem)
            if p_dir and Path(p_dir).exists() and Path(p_dir).is_dir():
                return Path(p_dir)

            all_names = [n for n in exact_candidates if n]
            for name in all_names:
                p = search_index.get("files_by_name", {}).get(name)
                if p and Path(p).exists() and not is_excluded(Path(p)):
                    return Path(p)

            for ext in extensions:
                p = search_index.get("files_by_name", {}).get(stem + ext)
                if p and Path(p).exists() and not is_excluded(Path(p)):
                    return Path(p)

            for p in search_index.get("files_by_stem", {}).get(stem, []) or []:
                p = Path(p)
                if p.exists() and p.is_file() and not is_excluded(p):
                    return p
        except Exception:
            pass

    if base_path.is_dir():
        # Build set of all candidate names including original
        all_candidates = {n for n in exact_candidates if n} | {stem + ext for ext in extensions} | {stem}
        
        for root, dirs, files in os.walk(base_rom):
            # Check files first
            for f in files:
                if f in all_candidates:
                    p_res = Path(root) / f
                    if not is_excluded(p_res):
                        return p_res
            # Check directories (for folder-based fallbacks like PS3)
            for d in dirs:
                if d == stem:
                    return Path(root) / d
                
    return None

def calculate_file_hash(file_path):
    if not os.path.exists(file_path):
        return None
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def calculate_folder_hash(folder_path):
    """
    Matches RomM/Wingosy logic: sorted list of 'name:md5' lines.
    """
    if not os.path.exists(folder_path):
        return None
    
    files_data = []
    for root, _, files in os.walk(folder_path):
        for file in files:
            full_path = Path(root) / file
            rel_path = full_path.relative_to(folder_path).as_posix()
            
            md5_hash = hashlib.md5()
            with open(full_path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    md5_hash.update(byte_block)
            
            files_data.append(f"{rel_path}:{md5_hash.hexdigest()}")
    
    files_data.sort()
    combined = "\n".join(files_data).encode('utf-8')
    return hashlib.sha256(combined).hexdigest()

def calculate_zip_content_hash(zip_path):
    """
    Simulate folder hash for a ZIP by hashing its internal members.
    """
    if not os.path.exists(zip_path) or not zipfile.is_zipfile(zip_path):
        return None
        
    files_data = []
    with zipfile.ZipFile(zip_path, 'r') as z:
        for member in z.infolist():
            if not member.is_dir():
                content = z.read(member)
                md5_h = hashlib.md5(content).hexdigest()
                files_data.append(f"{member.filename}:{md5_h}")
                
    files_data.sort()
    combined = "\n".join(files_data).encode('utf-8')
    return hashlib.sha256(combined).hexdigest()

def zip_path(source_path, output_zip):
    source = Path(source_path)
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        if source.is_dir():
            for file in source.rglob('*'):
                if file.is_file():
                    zf.write(file, source.name / file.relative_to(source))
        else:
            zf.write(source, source.name)

def extract_strip_root(zip_path, dest_dir, progress_cb=None):
    """
    Extract a ZIP file to dest_dir, stripping common root folder if it exists.
    """
    with zipfile.ZipFile(zip_path, 'r') as zf:
        members = zf.namelist()
        if not members: return
        
        # Detect common root prefix
        first = members[0]
        root = first.split('/')[0] + '/'
        all_have_root = all(m.startswith(root) for m in members)
        
        total = len(members)
        for i, member in enumerate(members):
            if all_have_root:
                rel = member[len(root):]
            else:
                rel = member
            
            if not rel: continue
            
            target = Path(dest_dir) / rel
            if member.endswith('/'):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, 'wb') as dst:
                    dst.write(src.read())
            
            if progress_cb:
                progress_cb(int((i + 1) / total * 100))

def read_retroarch_cfg(cfg_path):
    """
    Parse a retroarch.cfg file into a dict.
    Returns {} if file doesn't exist or can't be read.
    Lines look like: key = "value" or key = value
    """
    result = {}
    try:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                key, _, val = line.partition('=')
                key = key.strip()
                val = val.strip().strip('"')
                result[key] = val
    except Exception:
        pass
    return result

def write_retroarch_cfg_values(cfg_path, updates: dict):
    """
    Write key=value pairs into an existing retroarch.cfg.
    Updates existing keys in-place, appends new ones at end.
    Preserves all other lines exactly.
    Returns True on success, False on failure.
    """
    try:
        cfg_path = Path(cfg_path)
        if cfg_path.exists():
            lines = cfg_path.read_text(encoding='utf-8').splitlines()
        else:
            lines = []

        updated_keys = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if '=' in stripped and not stripped.startswith('#'):
                key = stripped.partition('=')[0].strip()
                if key in updates:
                    new_lines.append(f'{key} = "{updates[key]}"')
                    updated_keys.add(key)
                    continue
            new_lines.append(line)

        # Append any keys that weren't already in the file
        for key, val in updates.items():
            if key not in updated_keys:
                new_lines.append(f'{key} = "{val}"')

        cfg_path.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')
        return True
    except Exception as e:
        print(f"[retroarch_cfg] Failed to write {cfg_path}: {e}")
        return False

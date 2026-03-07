import json
import os
import logging
from pathlib import Path

DEFAULT_EMULATORS = [
    {
        "id": "retroarch",
        "name": "Multi-Console (RetroArch)",
        "executable_path": "",
        "launch_args": ["-L", "{core_path}", "{rom_path}"],
        "platform_slugs": ["multi", "nes", "snes", "n64", "gb", "gbc", "gba", "genesis", "mastersystem", "segacd", "gamegear", "atari2600", "psx", "psp"],
        "save_resolution": {
            "mode": "retroarch",
            "srm_dir": "",
            "state_dir": ""
        },
        "user_defined": False
    },
    {
        "id": "eden",
        "name": "Switch (Eden)",
        "executable_path": "",
        "launch_args": ["{rom_path}"],
        "platform_slugs": ["switch", "nintendo-switch"],
        "save_resolution": {
            "mode": "folder",
            "path": ""
        },
        "user_defined": False
    },
    {
        "id": "rpcs3",
        "name": "PlayStation 3",
        "executable_path": "",
        "launch_args": ["{rom_path}"],
        "platform_slugs": ["ps3", "playstation-3", "playstation3"],
        "save_resolution": {
            "mode": "folder",
            "path": ""
        },
        "user_defined": False
    },
    {
        "id": "dolphin",
        "name": "GameCube / Wii",
        "executable_path": "",
        "launch_args": ["{rom_path}"],
        "platform_slugs": ["gc", "ngc", "wii", "gamecube", "nintendo-gamecube", "nintendo-wii", "wii-u-vc"],
        "save_resolution": {
            "mode": "file",
            "path": ""
        },
        "user_defined": False
    },
    {
        "id": "pcsx2",
        "name": "PlayStation 2",
        "executable_path": "",
        "launch_args": ["{rom_path}"],
        "platform_slugs": ["ps2", "playstation-2", "playstation2"],
        "save_resolution": {
            "mode": "file",
            "path": ""
        },
        "user_defined": False
    },
    {
        "id": "cemu",
        "name": "Wii U (Cemu)",
        "executable_path": "",
        "launch_args": ["-g", "{rom_path}"],
        "platform_slugs": ["wiiu", "wii-u", "nintendo-wii-u", "nintendo-wiiu"],
        "save_resolution": {
            "mode": "folder",
            "path": ""
        },
        "user_defined": False
    },
    {
        "id": "azahar",
        "name": "Nintendo 3DS (Azahar)",
        "executable_path": "",
        "launch_args": ["{rom_path}"],
        "platform_slugs": ["n3ds", "3ds", "nintendo-3ds", "nintendo3ds", "new-nintendo-3ds", "new-nintendo-3ds-xl"],
        "save_resolution": {
            "mode": "folder",
            "path": ""
        },
        "user_defined": False
    },
    {
        "id": "windows_native",
        "name": "Windows (Native)",
        "executable_path": "",
        "launch_args": ["{rom_path}"],
        "platform_slugs": ["windows", "win", "pc", "pc-windows", "windows-games", "win95", "win98"],
        "save_resolution": {
            "mode": "none"
        },
        "user_defined": False,
        "is_native": True
    }
]

EMULATORS_FILE = Path.home() / ".wingosy" / "emulators.json"

def load_emulators_raw():
    """Load the full emulators.json content."""
    if not EMULATORS_FILE.exists():
        data = {"migration_done": False, "emulators": DEFAULT_EMULATORS}
        save_emulators_raw(data)
        return data
    
    try:
        with open(EMULATORS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
            # Problem 2: Filter out Yuzu
            initial_count = len(data.get("emulators", []))
            data["emulators"] = [
                e for e in data.get("emulators", [])
                if not (e.get("id", "").lower() == "yuzu" or "yuzu" in e.get("name", "").lower())
            ]
            if len(data["emulators"]) < initial_count:
                logging.info("Removed deprecated Yuzu entry from emulators")
                save_emulators_raw(data)
                
            return data
    except Exception as e:
        logging.error(f"Failed to load emulators.json: {e}")
    
    return {"migration_done": False, "emulators": DEFAULT_EMULATORS}

def load_emulators():
    """Return only the list of emulator dicts."""
    return load_emulators_raw().get("emulators", DEFAULT_EMULATORS)

def save_emulators_raw(data):
    """Save full content to emulators.json."""
    EMULATORS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(EMULATORS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Failed to save emulators.json: {e}")

def save_emulators(emulators_list):
    """Update only the emulators list in the JSON file."""
    data = load_emulators_raw()
    data["emulators"] = emulators_list
    save_emulators_raw(data)

def migrate_old_config(config_manager):
    """Migrate emulator paths from config.json to emulators.json once."""
    data = load_emulators_raw()
    if data.get("migration_done"):
        return

    logging.info("Starting emulator path migration from old config...")
    old_emus = config_manager.get("emulators", {})
    changed = False
    
    # Map old config names/ids to new schema IDs
    id_map = {
        "Multi-Console (RetroArch)": "retroarch",
        "Switch (Eden)": "eden",
        "PlayStation 3": "rpcs3",
        "GameCube / Wii": "dolphin",
        "PlayStation 2": "pcsx2",
        "Wii U (Cemu)": "cemu",
        "Nintendo 3DS (Azahar)": "azahar"
    }

    for old_name, old_data in old_emus.items():
        new_id = id_map.get(old_name)
        path = old_data.get("path")
        if new_id and path:
            for emu in data["emulators"]:
                if emu["id"] == new_id and not emu["executable_path"]:
                    emu["executable_path"] = path
                    logging.info(f"Migrated {new_id} path from old config: {path}")
                    changed = True
                    break
    
    data["migration_done"] = True
    save_emulators_raw(data)
    if changed:
        logging.info("Emulator path migration complete.")

def get_emulator_for_platform(slug):
    """Return the first emulator that supports the given platform slug."""
    all_emus = load_emulators()
    for emu in all_emus:
        if slug in emu.get("platform_slugs", []):
            return emu
    return None

def get_all_emulators():
    """Return the full list of emulators."""
    return load_emulators()

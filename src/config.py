import json
import os
from pathlib import Path

class ConfigManager:
    DEFAULT_CONFIG = {
        "host": "http://localhost:8285",
        "username": "admin",
        "password": "",
        "token": None,
        "auto_track": False,
        "auto_pull_saves": True,
        "device_id": "argosy-win-default",
        "base_rom_path": str(Path.home() / "Games" / "ROMs"),
        "base_emu_path": str(Path.home() / "Games" / "Emulators"),
        "preferred_emulators": {
            "switch": "Switch (Eden)"
        },
        "emulators": {
            "Switch (Yuzu)": {
                "exe": "yuzu.exe", 
                "type": "folder", 
                "title_id_regex": r"01[0-9a-f]{14}",
                "path": "",
                "config_path": str(Path(os.path.expandvars(r'%APPDATA%\yuzu\config'))),
                "github": "pineapple-emu/pineapple-src",
                "platform_slug": "switch",
                "folder": "yuzu",
                "portable_trigger": "user"
            },
            "Switch (Eden)": {
                "exe": "eden.exe",
                "type": "folder",
                "path": "",
                "config_path": str(Path(os.path.expandvars(r'%APPDATA%\eden\config'))),
                "url": "https://github.com/eden-emulator/Releases/releases/download/v0.2.0-rc1/Eden-Windows-v0.2.0-rc1-amd64-msvc-standard.zip",
                "platform_slug": "switch",
                "folder": "eden",
                "portable_trigger": "user"
            },
            "PlayStation 3": {
                "exe": "rpcs3.exe", 
                "type": "folder", 
                "path": "",
                "config_path": "",
                "github": "RPCS3/rpcs3-binaries-win",
                "platform_slug": "ps3",
                "folder": "rpcs3"
            },
            "Multi-Console (RetroArch)": {
                "exe": "retroarch.exe", 
                "type": "file", 
                "ext": "srm",
                "path": "",
                "config_path": str(Path(os.path.expandvars(r'%APPDATA%\RetroArch\retroarch.cfg'))),
                "url": "https://buildbot.libretro.com/stable/1.22.2/windows/x86_64/RetroArch.7z",
                "platform_slug": "multi",
                "folder": "retroarch"
            },
            "GameCube / Wii": {
                "exe": "Dolphin.exe", 
                "type": "file", 
                "ext": "sav",
                "path": "",
                "config_path": str(Path.home() / "Documents" / "Dolphin Emulator" / "Config"),
                "dolphin_latest": True,
                "platform_slug": "gc",
                "folder": "dolphin",
                "portable_trigger": "portable.txt"
            },
            "PlayStation 2": {
                "exe": "pcsx2-qt.exe", 
                "type": "file", 
                "ext": "ps2",
                "path": "",
                "config_path": str(Path(os.path.expandvars(r'%APPDATA%\PCSX2\config'))),
                "github": "PCSX2/pcsx2",
                "platform_slug": "ps2",
                "folder": "pcsx2",
                "portable_trigger": "portable.txt"
            }
        }
    }

    def __init__(self):
        self.config_dir = Path.home() / ".argosy"
        self.config_file = self.config_dir / "config.json"
        self.data = self.DEFAULT_CONFIG.copy()
        self.load()

    def load(self):
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    loaded_data = json.load(f)
                    for k, v in loaded_data.items():
                        if k != "emulators":
                            self.data[k] = v
                    loaded_emus = loaded_data.get("emulators", {})
                    for name, new_data in self.DEFAULT_CONFIG["emulators"].items():
                        for old_name, old_data in loaded_emus.items():
                            if old_data.get("exe") == new_data["exe"]:
                                new_data["path"] = old_data.get("path", "")
                                break
                        self.data["emulators"][name] = new_data
            except Exception as e:
                print(f"Error loading config: {e}")
        else:
            self.save()

    def save(self):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.data, f, indent=4)
        except Exception as e:
            print(f"Error saving config: {e}")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

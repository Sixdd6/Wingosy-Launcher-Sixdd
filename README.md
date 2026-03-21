<p align="center">
  <img src="gif_example.gif" alt="Wingosy in action" width="850">
</p>

# Wingosy-Launcher-Sixdd
> (A fork of) a game launcher for RomM on Windows. Browse your library, launch games, and keep saves backed up automatically.
> This fork is created utilizing AI as a coder while I handle the ideation and testing. The end goal is to learn, have fun and create something useful.

![Tests](https://github.com/Sixdd6/Wingosy-Launcher-Sixdd/actions/workflows/test.yml/badge.svg)

## Features
- Browse your full RomM library with cover art, metadata, platform filter and search
- Launch games via configured emulators with one click
- Auto save sync - pulls latest save before launch, pushes new saves on exit
- Smart conflict detection - per-emulator behavior: always ask, prefer cloud, or prefer local
- Windows native game support - download, extract and launch archived PC games
- PCGamingWiki integration - attempts to look up save folder locations for Windows games with fallback to manual selection
- Per-game Windows settings - select the executable to run and the save folder
- Custom emulator support - add any emulator with full config (name, exe, launch args, platforms, save mode)
- Per-platform emulator assignment - e.g. use native PPSSPP instead of RetroArch for PSP
- Secure credential storage via OS keyring
- Parallel library loading for large collections
- Configurable sync interval and log level

## Getting Started (for regular users)

1. Download the latest `Wingosy.exe` from [Releases](https://github.com/Sixdd6/Wingosy-Launcher-Sixdd/releases)
2. Run it - no install needed
3. Enter your RomM server URL and credentials
4. Set emulator paths in the **Emulators** tab
5. Click any game to download and play

## Save Sync
- Pulls before launch if cloud is newer, folder is empty, or folder is missing
- Pushes on exit - zips save dir, uploads to RomM
- Per-emulator conflict behavior in the Sync tab
- Sync interval configurable (default 120s)

## For Developers & Contributors

### Requirements
- Python 3.13
- A running RomM instance

### Run from source

```bash
git clone https://github.com/Sixdd6/Wingosy-Launcher-Sixdd
cd Wingosy-Launcher-Sixdd
pip install -r requirements.txt
python main.py
```

### Run tests

```bash
python -m pytest tests/ -v
```

### Build exe

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --name Wingosy --icon "assets/icon.png" --add-data "assets;assets" --hidden-import sqlite3 --hidden-import src.ui --hidden-import src.ui.main_window --hidden-import src.ui.dialogs --hidden-import src.ui.threads --hidden-import src.ui.widgets --hidden-import src.ui.tabs --hidden-import src.ui.tabs.library --hidden-import src.ui.tabs.emulators main.py
```

## License
GPL-3.0

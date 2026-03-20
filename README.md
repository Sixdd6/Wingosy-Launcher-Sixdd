<p align="center">
  <img src="gif_example.gif" alt="Wingosy in action" width="850">
</p>

# Wingosy-Launcher-Sixdd
> A (fork of a) game launcher for RomM on Windows. Browse your library, launch games, and keep saves backed up automatically.

![Tests](https://github.com/abduznik/Wingosy-Launcher/actions/workflows/test.yml/badge.svg)

## Features
- Browse full RomM library with cover art, ratings, platform filter and search
- Launch games via configured emulators with one click
- Auto save sync — pulls latest save before launch, pushes on exit
- Smart conflict detection — per-emulator behavior: always ask, prefer cloud, or prefer local
- Windows native game support — download, extract and launch .zip / .7z PC games directly
- PCGamingWiki integration — attempts to lookup save folder locations for Windows games
- Per-game Windows settings — select the executable to run and the save folder, in the event that PCGamingWiki fails to find it
- Custom emulator support — add any emulator with full config (name, exe, launch args, platforms, save mode)
- Per-platform emulator assignment — e.g. use native PPSSPP instead of RetroArch for PSP
- Secure credential storage via OS keyring
- Parallel library loading for large collections
- Configurable sync interval and log level

## Getting Started (for regular users)

1. Download the latest `Wingosy.exe` from [Releases](https://github.com/abduznik/Wingosy-Launcher/releases)
2. Run it — no install needed
3. Enter your RomM server URL and credentials
4. Set emulator paths in the **Emulators** tab
5. Click any game to download and play

## Save Sync
- Pulls before launch if cloud is newer, folder is empty, or folder is missing
- Pushes on exit — zips save dir, uploads to RomM
- Per-emulator conflict behavior in the Sync tab
- Sync interval configurable (default 120s)

## For Developers & Contributors

### Requirements
- Python 3.13
- A running RomM instance

### Run from source
    git clone https://github.com/abduznik/Wingosy-Launcher
    cd Wingosy-Launcher
    pip install -r requirements.txt
    python main.py

### Run tests
    python -m pytest tests/ -v

### Build exe
    pip install pyinstaller
    pyinstaller wingosy.spec

## License
GPL-3.0

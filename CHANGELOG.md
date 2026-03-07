# Changelog

## v0.6.0
### Security
- Migrated authentication token storage from plaintext `config.json` to the system's native secure credential manager (Windows Credential Picker / Keychain) using the `keyring` library.
- Automatic migration: existing tokens are moved to the secure store and removed from disk on first startup.

### Fixes
- Standardized `RomMClient` methods to handle token lifecycle internally.
- Improved logout to clear credentials from both memory and system storage.

## v0.5.7
### Fixes
- Library now loads all pages in parallel — users with 300+ ROMs and slow servers go from 6+ minutes to ~60s
- Added loading status label: shows connecting/loading progress inline instead of popup banners
- Connection timeout increased with better error messages
- MEI cleanup delay increased to prevent access denied on restart

## v0.5.6
### Fixes
- Connection timeout: app no longer hangs indefinitely on slow or unreachable servers
- Better error messages: distinguishes timeout vs wrong host vs auth failure
- MEI cleanup delay increased to prevent access denied errors on restart

## v0.5.5
### Critical Fixes
- Fixed app crash on restart (certifi TLS path after MEI cleanup)
- Fixed restart not launching new process on Windows
- Fixed platform filter resetting after any UI action
- Fixed false connection failure banner on startup
- Fixed stdout/stderr crash when running as frozen exe
- Fixed PSP state never syncing when SAVEDATA unchanged
- Fixed upload_state going to wrong endpoint
- Fixed save conflict dialog appearing after emulator launched

### New Features
- RetroArch dual sync: SRM + savestate on every session
- PSP full sync: SAVEDATA folder + state file
- Cloud pull blocks before emulator launches
- Live connection tests added to test suite
- File logging to ~/.wingosy/app.log for diagnostics

## v0.5.4
### New Features
- RetroArch dual save sync: SRM + savestate synced on every session for all RetroArch cores
- PSP full sync: SAVEDATA folder AND state file both uploaded and downloaded per session
- Cloud pull now happens before emulator launches (blocking)
- Conflict dialog shown before launch, not after

### Bug Fixes
- Fixed skip_next_pull firing on every launch instead of only after conflict resolution
- Fixed upload_state sending to wrong endpoint
- Fixed PSP permission error on SAVEDATA folder at launch
- Fixed PSP state never uploading when SAVEDATA was unchanged
- Fixed state file written without .auto suffix on download
- Fixed _ppsspp_assets_checked NameError on PSP launch
- Added missing save folder mappings for 3DO, MSX, Saturn

### Cleanup
- Removed all diagnostic debug prints and temp scripts

## v0.5.3
### New Features
- Cards-per-row setting (1–12, live resize)
- Background library fetch with instant cache on startup
- Live host editing in Settings with test + apply + restart
- Network error handling with reconnect banner
- MEI folder cleanup on startup

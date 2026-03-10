import time
import psutil
import os
import re
import shutil
import zipfile
import json
import hashlib
import logging
import traceback
from pathlib import Path
from PySide6.QtCore import QThread, Signal, QTimer
from src.utils import calculate_folder_hash, calculate_file_hash, calculate_zip_content_hash, zip_path, extract_strip_root
from src import emulators
from src.save_strategies import get_strategy

class PostSessionSyncThread(QThread):
    done = Signal(str, bool)   # rom_name, success
    log  = Signal(str)         # log message for GUI
    notify = Signal(str, str)  # title, body for tray notification

    def __init__(self, watcher, data):
        super().__init__()
        self.watcher = watcher
        self.data = data

    def run(self):
        title = self.data.get('title', 'Unknown Game')
        rom_id = str(self.data.get('rom_id'))
        emu_id = self.data.get('emulator', {}).get('id', 'unknown')
        strategy = self.data.get('strategy')
        rom = self.data.get('game_data')

        try:
            logging.info(f"[SyncThread] Starting sync for {title}")
            save_files = strategy.get_save_files(rom)
            logging.info(f"[SyncThread] Save files: {save_files}")

            if not save_files:
                logging.info(f"[SyncThread] No save files found for {title}, skipping")
                self.log.emit(f"💤 No saves found for {title}, skipping upload")
                self.done.emit(title, True)
                return

            ok = True  # innocent until proven guilty
            srm_files   = [f for f in save_files if not f.is_dir() and f.suffix in ('.srm', '.sav')]
            state_files  = [f for f in save_files if not f.is_dir() and '.state' in f.name]
            folder_files = [f for f in save_files if f.is_dir()]

            if folder_files:
                # Folder (e.g. PSP SAVEDATA) — zip and upload as save
                save_dir = folder_files[0]
                temp_zip = Path.home() / ".wingosy" / "tmp" / f"upload_{rom_id}.zip"
                temp_zip.parent.mkdir(parents=True, exist_ok=True)
                zip_path(str(save_dir), str(temp_zip))
                ok2, msg = self.watcher.client.upload_save(rom_id, emu_id, str(temp_zip), slot="wingosy-srm")
                logging.info(f"[SyncThread] Folder upload {'ok' if ok2 else 'failed'}: {msg}")
                if temp_zip.exists(): temp_zip.unlink()
                ok = ok and ok2

            for srm in srm_files:
                ok2, msg = self.watcher.client.upload_save(rom_id, emu_id, str(srm), slot="wingosy-srm")
                logging.info(f"[SyncThread] SRM upload {'ok' if ok2 else 'failed'}: {msg}")
                ok = ok and ok2

            for st in state_files:
                ok2, msg = self.watcher.client.upload_state(rom_id, emu_id, str(st), slot="wingosy-state")
                logging.info(f"[SyncThread] State upload {'ok' if ok2 else 'failed'}: {msg}")
                ok = ok and ok2

            status = "✅" if ok else "❌"
            self.log.emit(f"{status} Sync complete: {title}")
            self.notify.emit("Wingosy", f"{'✅ Saved to cloud' if ok else '❌ Sync failed'}: {title}")
            self.done.emit(title, ok)
        except Exception as e:
            logging.error(f"[SyncThread] Error for {title}: {e}", exc_info=True)
            self.log.emit(f"❌ Sync error for {title}: {e}")
            self.notify.emit("Wingosy", f"❌ Sync error: {title}")
            self.done.emit(title, False)

class WingosyWatcher(QThread):
    log_signal = Signal(str)
    path_detected_signal = Signal(str, str) # emu_display_name, path
    conflict_signal = Signal(str, str, str, str) # title, local_path, temp_dl, rom_id
    notify_signal = Signal(str, str) # title, msg

    def __init__(self, client, config):
        super().__init__()
        self.client = client
        self.config = config
        self.running = True
        self.active_sessions = {}
        self.session_errors = {} # rom_id -> consecutive error count
        self.skip_next_pull_rom_id = None # Flag to prevent double-pull when launching from app
        self._sync_threads = []
        
        self.tmp_dir = Path.home() / ".wingosy" / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        
        self.cache_path = Path.home() / ".wingosy" / "sync_cache.json"
        self.sync_cache = {}
        if self.cache_path.exists():
            try:
                with open(self.cache_path, 'r') as f:
                    self.sync_cache = json.load(f)
            except Exception as e:
                logging.error(f"[Watcher] Cache load error: {e}")

    def save_cache(self):
        try:
            with open(self.cache_path, 'w') as f:
                json.dump(self.sync_cache, f)
        except Exception as e:
            logging.error(f"[Watcher] Cache save error: {e}")

    def run(self):
        logging.info("🚀 Watcher Active (Process-Specific Mode).")
        while self.running:
            for pid, data in list(self.active_sessions.items()):
                try:
                    if not psutil.pid_exists(pid):
                        try:
                            self.handle_exit(data)
                        except Exception as e:
                            logging.error(f"[Watcher] Error in handle_exit for {data.get('title')}:\n{traceback.format_exc()}")
                        del self.active_sessions[pid]
                    else:
                        now = time.time()
                        interval = self.config.get("sync_interval_seconds", 120)
                        if now - data.get("last_sync_time", 0) >= interval:
                            try:
                                self._do_mid_session_sync(data)
                            except Exception as e:
                                logging.error(f"[Watcher] Error in mid-session sync for {data.get('title')}: {e}")
                            data["last_sync_time"] = now
                except Exception as e:
                    logging.error(f"❌ Error monitoring PID {pid}: {e}")
                    del self.active_sessions[pid]
            time.sleep(5)

    def _get_current_hash(self, strategy, rom):
        try:
            files = strategy.get_save_files(rom)
            if not files: return None
            h = hashlib.md5()
            found = False
            for p in sorted(files):
                if not p.exists(): continue
                found = True
                if p.is_dir():
                    # Handle folder-based saves (e.g. PSP)
                    f_hash = calculate_folder_hash(str(p))
                    if f_hash: h.update(f_hash.encode('utf-8'))
                else:
                    with open(p, 'rb') as f:
                        while True:
                            chunk = f.read(8192)
                            if not chunk: break
                            h.update(chunk)
            return h.hexdigest() if found else None
        except Exception as e:
            logging.error(f"[Watcher] Hash calculation failed for {rom.get('name')}: {e}")
            return None

    def _get_max_mtime(self, strategy, rom):
        try:
            files = strategy.get_save_files(rom)
            if not files: return 0
            
            mtimes = []
            for p in files:
                if not p.exists(): continue
                if p.is_dir():
                    # For directories, get max mtime of any file inside
                    for root, _, walk_files in os.walk(p):
                        for f in walk_files:
                            try: mtimes.append(os.path.getmtime(os.path.join(root, f)))
                            except: pass
                    # Also include the directory's own mtime
                    mtimes.append(os.path.getmtime(p))
                else:
                    mtimes.append(os.path.getmtime(p))
            
            return max(mtimes, default=0)
        except Exception as e:
            logging.error(f"[Watcher] MTime check failed for {rom.get('name')}: {e}")
            return 0

    def track_session(self, proc, emu_display_name, game_data, local_rom_path, emu_path, skip_pull=False, windows_save_dir=None):
        try:
            pid = proc.pid
            rom_id = game_data['id']
            title = game_data['name']
            
            logging.debug(f"[Sync] track_session for {title} (ROM ID: {rom_id})")

            all_emus = emulators.load_emulators()
            this_emu = next((e for e in all_emus if e["name"] == emu_display_name or e["id"] == emu_display_name), None)
            
            if not this_emu and (windows_save_dir or game_data.get("platform_slug") == "windows"):
                this_emu = {"id": "windows_native", "is_native": True, "name": "Windows (Native)"}

            if not this_emu:
                logging.error(f"[Watcher] Could not find emulator metadata for {emu_display_name}")
                return

            strategy = get_strategy(self.config, this_emu)
            strategy.set_session_context(start_time=time.time(), rom_path=local_rom_path)
            logging.debug(f"[Sync] Strategy: {strategy.__class__.__name__}")
            
            # 1. Pull if needed
            should_pull = (self.config.get("auto_pull_saves", True) and not skip_pull)
            if self.skip_next_pull_rom_id == str(rom_id):
                should_pull = False
                self.skip_next_pull_rom_id = None
            
            if should_pull:
                save_dir = strategy.get_save_dir(game_data)
                if save_dir:
                    self.pull_server_save(rom_id, title, str(save_dir), True, emu_id=this_emu["id"])
                else:
                    files = strategy.get_save_files(game_data)
                    if files:
                        self.pull_server_save(rom_id, title, str(files[0]), False, emu_id=this_emu["id"])

            # 2. Capture initial state
            h = self._get_current_hash(strategy, game_data)
            m = self._get_max_mtime(strategy, game_data)

            session_data = {
                'rom_id': rom_id,
                'title': title,
                'game_data': game_data,
                'strategy': strategy,
                'emulator': this_emu,
                'initial_hash': h,
                'initial_mtime': m,
                'start_time': time.time(),
                'last_sync_time': time.time(),
            }
            self.active_sessions[pid] = session_data
            self.log_signal.emit(f"🎮 Tracking {title} on {this_emu['name']} (PID: {pid})")

        except Exception as e:
            logging.error(f"Error starting session tracking: {e}")

    def handle_exit(self, data):
        title, rom_id, strategy, rom, emu = data['title'], data['rom_id'], data['strategy'], data['game_data'], data['emulator']

        if not emu.get("sync_enabled", True):
            logging.info(f"[Watcher] Sync disabled for {emu['name']}, skipping upload for {title}")
            return

        if rom_id and self.session_errors.get(str(rom_id), 0) >= 5:
            logging.warning(f"[Watcher] Giving up on save sync for {title} after 5 consecutive errors")
            return

        self.log_signal.emit(f"🛑 Session Ended: {title}")
        
        new_h = self._get_current_hash(strategy, rom)
        new_m = self._get_max_mtime(strategy, rom)
        
        cached_mtime = self.sync_cache.get(str(rom_id), {}).get("save_mtime")
        
        should_sync = False
        if cached_mtime is None:
            should_sync = True
        else:
            should_sync = new_m > cached_mtime

        if not should_sync and new_h == data.get('initial_hash'):
            self.log_signal.emit(f"💤 No changes in {title}. Skipping sync.")
            self._update_playtime(data)
            return

        self.log_signal.emit(f"📤 Syncing {title} in background...")

        # Start background sync thread — upload happens entirely in the thread
        thread = PostSessionSyncThread(self, data)
        thread.log.connect(self.log_signal)
        thread.notify.connect(self.notify_signal)
        thread.done.connect(lambda name, ok, rid=rom_id, m=new_m: self._on_sync_thread_done(rid, m, ok))

        self._sync_threads.append(thread)
        thread.finished.connect(lambda t=thread: self._sync_threads.remove(t) if t in self._sync_threads else None)
        thread.start()
        
        self._update_playtime(data)

    def _on_sync_thread_done(self, rom_id, new_m, success):
        rom_id_str = str(rom_id)
        if success:
            self.session_errors[rom_id_str] = 0
            entry = self.sync_cache.get(rom_id_str, {})
            entry["save_mtime"] = new_m
            self.sync_cache[rom_id_str] = entry
            self.save_cache()
        else:
            self.session_errors[rom_id_str] = self.session_errors.get(rom_id_str, 0) + 1

    def _do_mid_session_sync(self, data):
        strategy, rom = data['strategy'], data['game_data']
        rom_id = data['rom_id']
        title = data['title']
        
        new_h = self._get_current_hash(strategy, rom)
        new_m = self._get_max_mtime(strategy, rom)
        
        cached_mtime = self.sync_cache.get(str(rom_id), {}).get("save_mtime")
        
        should_sync = False
        if cached_mtime is None:
            should_sync = True
        else:
            should_sync = new_m > cached_mtime

        if should_sync or (new_h and new_h != data.get('last_mid_sync_hash', data.get('initial_hash'))):
            logging.info(f"🔄 Mid-session changes detected for {title}. Syncing...")
            # Mid-session we still do async via thread — upload happens entirely in the thread
            thread = PostSessionSyncThread(self, data)
            thread.log.connect(self.log_signal)
            thread.notify.connect(self.notify_signal)
            thread.done.connect(lambda name, ok, rid=rom_id, m=new_m: self._on_sync_thread_done(rid, m, ok))
            self._sync_threads.append(thread)
            thread.finished.connect(lambda t=thread: self._sync_threads.remove(t) if t in self._sync_threads else None)
            thread.start()
            data['last_mid_sync_hash'] = new_h

    def _update_playtime(self, data):
        try:
            elapsed = int(time.time() - data.get('start_time', time.time()))
            if elapsed > 10:
                self.client.update_playtime(data['rom_id'], elapsed)
                logging.info(f"[Watcher] Playtime updated for {data['title']}: {elapsed}s")
        except Exception as e:
            logging.error(f"[Watcher] Playtime update failed: {e}")

    def pull_server_save(self, rom_id, title, local_path, is_folder, force=False, emu_id=None):
        behavior = self.config.get("conflict_behavior", "ask")
        if emu_id:
            all_emus = emulators.load_emulators()
            emu = next((e for e in all_emus if e["id"] == emu_id), None)
            if emu: behavior = emu.get("conflict_behavior", "ask")

        if behavior == "prefer_local" and not force: return

        try:
            latest_save = self.client.get_latest_save(rom_id)
            if latest_save:
                self._apply_cloud_file(rom_id, title, latest_save, local_path, is_folder, force, behavior=behavior)
        except Exception as e:
            logging.error(f"[Watcher] pull_server_save failed for {title}: {e}")

    def _apply_cloud_file(self, rom_id, title, cloud_obj, local_path, is_folder, force, behavior="ask"):
        try:
            server_updated_at = cloud_obj.get('updated_at', '')
            cached_val = self.sync_cache.get(str(rom_id), {})
            cached_ts = cached_val.get('save_updated_at', '') if isinstance(cached_val, dict) else ""

            if not force and cached_ts == server_updated_at and os.path.exists(local_path):
                return

            temp_dl = str(self.tmp_dir / f"cloud_pull_{rom_id}")
            if self.client.download_save(cloud_obj, temp_dl):
                if os.path.exists(local_path) and behavior == "ask" and not force:
                    remote_h = calculate_zip_content_hash(temp_dl) if zipfile.is_zipfile(temp_dl) else calculate_file_hash(temp_dl)
                    local_h = calculate_folder_hash(local_path) if is_folder else calculate_file_hash(local_path)
                    if remote_h == local_h:
                        self.sync_cache[str(rom_id)] = {"save_updated_at": server_updated_at}
                        self.save_cache()
                        return
                    self.conflict_signal.emit(title, local_path, temp_dl, str(rom_id))
                    return

                if is_folder:
                    os.makedirs(local_path, exist_ok=True)
                    extract_strip_root(temp_dl, local_path)
                else:
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    shutil.copy2(temp_dl, local_path)

                self.sync_cache[str(rom_id)] = {"save_updated_at": server_updated_at}
                self.save_cache()
                self.log_signal.emit(f"✨ Cloud save applied for {title}!")
        except Exception as e:
            logging.error(f"[Watcher] _apply_cloud_file failed: {e}")
        finally:
            if 'temp_dl' in locals() and os.path.exists(temp_dl): os.remove(temp_dl)

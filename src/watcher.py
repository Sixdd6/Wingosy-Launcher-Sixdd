import time
import psutil
import os
import re
import shutil
import zipfile
import json
from pathlib import Path
from PySide6.QtCore import QThread, Signal
from src.utils import calculate_folder_hash, calculate_file_hash, calculate_zip_content_hash, zip_path

class ArgosyWatcher(QThread):
    log_signal = Signal(str)
    path_detected_signal = Signal(str, str) # emu_display_name, path

    def __init__(self, client, config_manager):
        super().__init__()
        self.client = client
        self.config = config_manager
        self.running = True
        self.active_sessions = {}
        self.failed_pids = set()
        self.skip_next_pull_rom_id = None # Flag to prevent double-pull when launching from app
        
        self.cache_path = Path.home() / ".argosy" / "sync_cache.json"
        self.sync_cache = {}
        if self.cache_path.exists():
            try:
                with open(self.cache_path, 'r') as f:
                    self.sync_cache = json.load(f)
            except:
                pass

    def save_cache(self):
        try:
            with open(self.cache_path, 'w') as f:
                json.dump(self.sync_cache, f)
        except:
            pass

    def run(self):
        self.log_signal.emit("🚀 Watcher Active.")
        while self.running:
            target_emus = self.config.get("emulators", {})
            target_exes = [cfg['exe'].lower() for cfg in target_emus.values()]
            
            found_pids = []
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    name = proc.info['name'].lower()
                    if name in target_exes:
                        pid = proc.info['pid']
                        found_pids.append(pid)
                        if pid not in self.active_sessions and pid not in self.failed_pids:
                            self.handle_new_launch(pid, name, proc.info.get('cmdline', []))
                except:
                    continue

            for pid, data in list(self.active_sessions.items()):
                if pid not in found_pids:
                    self.handle_exit(data)
                    del self.active_sessions[pid]
            
            self.failed_pids = {pid for pid in self.failed_pids if pid in found_pids}
            time.sleep(2)

    def handle_new_launch(self, pid, emu_exe, cmdline):
        try:
            proc = psutil.Process(pid)
            full_cmd = " ".join(cmdline).lower()
            emu_path = proc.exe()
            
            # 1. Auto-detect display name
            emu_display_name = None
            emus = self.config.get("emulators")
            for disp_name, data in emus.items():
                if data['exe'].lower() == emu_exe.lower():
                    emu_display_name = disp_name
                    if data.get('path') != emu_path:
                        self.log_signal.emit(f"📍 Auto-detected {disp_name} at {emu_path}")
                        self.path_detected_signal.emit(disp_name, emu_path)
                    break

            if not emu_display_name:
                return

            # 2. Match game
            matched_game = None
            for game in self.client.user_games:
                fs_name = (game.get('fs_name') or "").lower()
                if fs_name and fs_name in full_cmd:
                    matched_game = game
                    break
            
            if not matched_game:
                try:
                    open_files = [f.path.lower() for f in proc.open_files()]
                    for game in self.client.user_games:
                        fs_name = (game.get('fs_name') or "").lower()
                        if any(fs_name in f for f in open_files):
                            matched_game = game
                            break
                except:
                    pass

            if not matched_game:
                self.log_signal.emit(f"⚠️ Could not match ROM for {emu_display_name}.")
                self.failed_pids.add(pid)
                return

            rom_id = matched_game['id']
            title = matched_game['name']
            
            # 3. Resolve Save Path
            save_path = self.resolve_save_path(emu_display_name, title, full_cmd, emu_path)

            if save_path:
                # Standardize folder detection
                is_folder = "Switch" in emu_display_name or "rpcs3" in emu_exe.lower()
                if os.path.exists(save_path):
                    is_folder = os.path.isdir(save_path)
                
                # Double-Pull Protection
                should_pull = self.config.get("auto_pull_saves", True)
                if self.skip_next_pull_rom_id == str(rom_id):
                    self.log_signal.emit(f"⚡ Skipping initial pull for {title} (already handled).")
                    self.skip_next_pull_rom_id = None
                    should_pull = False

                if should_pull:
                    self.pull_server_save(rom_id, title, save_path, is_folder)
                
                # Track the session
                h = None
                if os.path.exists(save_path):
                    h = calculate_folder_hash(save_path) if is_folder else calculate_file_hash(save_path)

                self.active_sessions[pid] = {
                    'emu': emu_display_name, 
                    'rom_id': rom_id, 
                    'save_path': str(save_path),
                    'title': title,
                    'initial_hash': h,
                    'is_folder': is_folder
                }
                self.log_signal.emit(f"🎮 Tracking {title} on {emu_display_name}")
            else:
                self.log_signal.emit(f"⚠️ Identified {title} but could not resolve local save path.")
                self.failed_pids.add(pid)
                
        except Exception as e:
            self.log_signal.emit(f"❌ Error in launch: {e}")

    def pull_server_save(self, rom_id, title, local_path, is_folder, force=False):
        self.log_signal.emit(f"☁️ Checking cloud for {title}...")
        latest_save = self.client.get_latest_save(rom_id)
        if not latest_save: 
            self.log_signal.emit("☁️ No cloud saves found.")
            return

        save_id = str(latest_save['id'])
        if not force and self.sync_cache.get(str(rom_id)) == save_id:
            self.log_signal.emit(f"☁️ Cloud save ({save_id}) already applied.")
            return

        temp_dl = "cloud_check_file"
        if self.client.download_save(latest_save, temp_dl):
            is_zip = zipfile.is_zipfile(temp_dl)
            if is_zip:
                server_hash = calculate_zip_content_hash(temp_dl)
            else:
                server_hash = calculate_file_hash(temp_dl)
                
            local_hash = None
            if os.path.exists(local_path):
                local_hash = calculate_folder_hash(local_path) if is_folder else calculate_file_hash(local_path)

            if not force and local_hash and server_hash == local_hash:
                self.log_signal.emit("☁️ Local save identical to cloud.")
                self.sync_cache[str(rom_id)] = save_id
                self.save_cache()
                os.remove(temp_dl)
                return

            self.log_signal.emit(f"📥 Cloud save is different. Updating...")
            
            # Ensure parent dir exists
            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            # Backup
            if os.path.exists(local_path):
                bak_path = str(local_path) + ".bak"
                if os.path.exists(bak_path):
                    if os.path.isdir(bak_path):
                        shutil.rmtree(bak_path)
                    else:
                        os.remove(bak_path)
                
                if is_folder:
                    shutil.copytree(local_path, bak_path)
                else:
                    shutil.copy2(local_path, bak_path)

            # Apply
            try:
                if is_zip:
                    if is_folder:
                        with zipfile.ZipFile(temp_dl, 'r') as z:
                            z.extractall(Path(local_path).parent)
                    else:
                        with zipfile.ZipFile(temp_dl, 'r') as z:
                            target_member = None
                            for name in z.namelist():
                                if name.endswith(('.ps2', '.srm', '.sav', '.dat', '.sv')):
                                    target_member = name
                                    break
                            
                            if target_member:
                                with z.open(target_member) as source, open(local_path, 'wb') as target:
                                    shutil.copyfileobj(source, target)
                            else:
                                names = z.namelist()
                                if names:
                                    with z.open(names[0]) as source, open(local_path, 'wb') as target:
                                        shutil.copyfileobj(source, target)
                else:
                    shutil.copy2(temp_dl, local_path)
                
                self.sync_cache[str(rom_id)] = save_id
                self.save_cache()
                self.log_signal.emit("✨ Cloud save applied!")
            except Exception as e:
                self.log_signal.emit(f"❌ Failed to apply save: {e}")
            
            if os.path.exists(temp_dl):
                os.remove(temp_dl)

    def resolve_save_path(self, emu_display_name, title, full_cmd, emu_path):
        emu_dir = Path(emu_path).parent
        
        if "Switch" in emu_display_name:
            # 1. Known Title ID Mapping
            title_id = None
            t_lower = title.lower()
            if "monster hunter rise" in t_lower:
                title_id = "0100B04011422000"
            elif "monster hunter generations" in t_lower or "mhgu" in t_lower:
                title_id = "0100770008DD8000"
            elif "tears of the kingdom" in t_lower or "totk" in t_lower:
                title_id = "0100F2C0115B6000"
            elif "breath of the wild" in t_lower or "botw" in t_lower:
                title_id = "01007EF00011E000"
            
            # 2. Extract from command line if not hardcoded
            if not title_id:
                m = re.search(r'01[0-9a-f]{14}', full_cmd)
                if m:
                    title_id = m.group(0).upper()
            
            if title_id:
                search_roots = [
                    emu_dir / "user",
                    emu_dir / "data",
                    Path(os.path.expandvars(r'%APPDATA%\yuzu')),
                    Path(os.path.expandvars(r'%APPDATA%\eden')),
                    Path(os.path.expandvars(r'%APPDATA%\sudachi')),
                    Path(os.path.expandvars(r'%APPDATA%\ryujinx'))
                ]
                
                # 3. Profile Detection Logic
                for root in search_roots:
                    if not root.exists():
                        continue
                    
                    # Search for existing save folders first (Deep Search)
                    for p in root.rglob(title_id):
                        if p.is_dir() and "save" in str(p).lower():
                            return p
                    
                    # If not found, try to find the actual User Profile folder
                    save_base = root / "nand/user/save"
                    if save_base.exists():
                        profiles = [d for d in save_base.iterdir() if d.is_dir() and d.name != "0000000000000000"]
                        if profiles:
                            profile_id = profiles[0].name
                            return save_base / "0000000000000000" / profile_id / title_id
                
                # 4. Final Fallback
                for root in search_roots:
                    if root.exists():
                        return root / "nand/user/save/0000000000000000/0000000000000000" / title_id
        
        elif "PlayStation 2" in emu_display_name:
            search_paths = [
                emu_dir / "memcards" / "Mcd001.ps2",
                emu_dir / "memcards" / "Mcd000.ps2",
                emu_dir / "user" / "memcards" / "Mcd001.ps2",
                Path(os.path.expandvars(r'%APPDATA%\PCSX2\memcards\Mcd001.ps2')),
                Path.home() / "Documents" / "PCSX2" / "memcards" / "Mcd001.ps2"
            ]
            for p in search_paths:
                if p.exists():
                    return p
            return search_paths[0]

        elif "RetroArch" in emu_display_name:
            rom_name = Path(title).stem.lower()
            for game in self.client.user_games:
                if game['name'] == title:
                    rom_name = Path(game['fs_name']).stem.lower()
            search_paths = [emu_dir / "saves", Path(os.path.expandvars(r'%APPDATA%\RetroArch\saves'))]
            for p in search_paths:
                if p.exists():
                    for f in p.glob(f"*{rom_name}*.srm"):
                        return f
            return search_paths[0] / f"{rom_name}.srm"
            
        return None

    def handle_exit(self, data):
        self.log_signal.emit(f"🛑 Session Ended: {data['title']}")
        if not os.path.exists(data['save_path']):
            return
        if data['is_folder'] and not any(Path(data['save_path']).iterdir()):
            return
            
        new_h = calculate_folder_hash(data['save_path']) if data['is_folder'] else calculate_file_hash(data['save_path'])
        if new_h == data['initial_hash']:
            self.log_signal.emit(f"⏭️ No changes in {data['title']}. Skipping sync.")
            return

        self.log_signal.emit(f"📝 Changes detected! Syncing...")
        temp_zip = f"sync_{data['rom_id']}.zip"
        try:
            zip_path(data['save_path'], temp_zip)
            success, msg = self.client.upload_save(data['rom_id'], data['emu'], temp_zip)
            if success:
                self.log_signal.emit("✨ Sync Complete!")
                if str(data['rom_id']) in self.sync_cache:
                    del self.sync_cache[str(data['rom_id'])]
                    self.save_cache()
            else:
                self.log_signal.emit(f"❌ Sync Failed: {msg}")
        finally:
            if os.path.exists(temp_zip):
                os.remove(temp_zip)

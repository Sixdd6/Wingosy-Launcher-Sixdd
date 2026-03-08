import os
import sys
import shutil
import zipfile
import logging
from pathlib import Path

from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLabel, QPushButton, QTabWidget, QTextEdit, 
                             QSystemTrayIcon, QMenu, QApplication, QFileDialog, 
                             QMessageBox, QDialog, QLineEdit, QDialogButtonBox, 
                             QScrollArea)
from PySide6.QtGui import QIcon, QPixmap, QKeySequence, QShortcut
from PySide6.QtCore import Qt, QSettings, Slot, Signal, QThread, QTimer, QEvent

from src.ui.threads import (ImageFetcher, BiosDownloader, DolphinDownloader, 
                            DirectDownloader, GithubDownloader, ConflictResolveThread)
from src.ui.widgets import get_resource_path, DownloadQueueWidget, format_speed
from src.ui.dialogs import SetupDialog, SettingsDialog, WelcomeDialog, ConflictDialog
from src.ui.tabs.library import LibraryTab
from src.ui.tabs.emulators import EmulatorsTab
from src.utils import zip_path
from src.platforms import RETROARCH_PLATFORMS, platform_matches
from src import emulators

class LibraryFetchWorker(QThread):
    finished = Signal(object)    # emits the final list or "REAUTH_REQUIRED"
    error = Signal()             # emitted on network failure
    retrying = Signal()          # emitted on Stage 1 timeout
    batch_ready = Signal(list, int) # emits a batch of games and total count

    def __init__(self, client, cached_non_empty=False):
        super().__init__()
        self.client = client
        self.cached_non_empty = cached_non_empty

    def run(self):
        def _on_page(batch, total):
            # Pre-calculate local ROM existence for this batch
            base_rom = self.client.config.get("base_rom_path")
            if base_rom:
                base_path = Path(base_rom)
                for g in batch:
                    rom_name = g.get('fs_name')
                    platform = g.get('platform_slug')
                    exists = False
                    if rom_name:
                        if (base_path / platform / rom_name).exists() or (base_path / rom_name).exists():
                            exists = True
                    g['_local_exists'] = exists
            self.batch_ready.emit(batch, total)

        try:
            result = self.client.fetch_library(
                retry_callback=lambda: self.retrying.emit(),
                page_callback=_on_page
            )
        except Exception:
            result = None
        
        if result is None:
            self.error.emit()
            return

        # Final result emission (used for final cache and cleanup)
        self.finished.emit(result)

from src.ui.title_bar import WingosyTitleBar

class WingosyMainWindow(QMainWindow):
    def __init__(self, config_manager, client, watcher_class, version):
        super().__init__()
        self.config, self.client, self.watcher_class, self.version = config_manager, client, watcher_class, version
        self.watcher = None
        self.active_threads = []
        self.image_fetch_queue = []
        self.active_image_fetchers = []
        self.fetch_generation = 0
        self.all_games = []
        
        # Frameless window setup
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        self.setWindowTitle("Wingosy Launcher")
        self.resize(1100, 800)
        
        settings = QSettings("Wingosy", "WingosyLauncher")
        geometry = settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        
        icon_path = get_resource_path("icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
            
        self.setup_ui()
        self.setup_tray()
        self.ensure_watcher_running()

        # 1. Load cache immediately — show games NOW
        self._load_library_from_cache()
        # 2. Then fetch fresh data in background
        QTimer.singleShot(500, self.fetch_library_and_populate)

        # Edge resize support

        self.setMouseTracking(True)
        self.centralWidget().setMouseTracking(True)
        self._resize_border = 8
        self.installEventFilter(self)
        
        if self.config.data.get("keyring_failed"):
            QMessageBox.warning(
                self,
                "Credential Storage Warning",
                "Your system's secure credential manager is unavailable.\n\n"
                "Wingosy has stored your login token using local encryption instead.\n\n"
                "This is less secure than keyring. Consider enabling your OS keyring."
            )
            self.config.data.pop("keyring_failed", None)

        if self.config.get("first_run", True):
            WelcomeDialog(self).exec()
            self.config.set("first_run", False)

    def setup_ui(self):
        central_widget = QWidget()
        central_widget.setObjectName("centralWidget")
        central_widget.setStyleSheet("#centralWidget { background: #1a1a1a; border-radius: 10px; border: 1px solid #333; }")
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Custom Title Bar
        self.title_bar = WingosyTitleBar(self)
        self.title_bar.tab_changed.connect(self._on_tab_changed)
        self.title_bar.settings_requested.connect(self.open_settings)
        main_layout.addWidget(self.title_bar)
        
        # Update connection status
        host = self.config.get("host", "")
        self.title_bar.update_connection_status("connected" if self.client.token else "disconnected", host)

        self.tabs = QTabWidget()
        self.tabs.tabBar().hide() # Hide default tab bar
        self.tabs.setStyleSheet("""
            QTabWidget::pane { border: none; background: #1a1a1a; }
        """)

        self.library_tab = LibraryTab(self)
        self.tabs.addTab(self.library_tab, "Library")

        self.emulators_tab = EmulatorsTab(self)
        self.tabs.addTab(self.emulators_tab, "Emulators")

        # Logs & Downloads Tab
        self.info_tabs = QTabWidget()
        self.info_tabs.setStyleSheet("QTabWidget::pane { border: none; }")
        self.download_queue = DownloadQueueWidget()
        self.info_tabs.addTab(self.download_queue, "Downloads")        

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setStyleSheet("background: #121212; color: #bbdefb; font-family: Consolas; border: none;")
        self.info_tabs.addTab(self.log_area, "Logs")

        self.tabs.addTab(self.info_tabs, "Logs")
        
        main_layout.addWidget(self.tabs)

    def _on_tab_changed(self, index):
        self.tabs.setCurrentIndex(index)
        self.title_bar.set_active_tab(index)

    def eventFilter(self, obj, event):
        # MUST call super() and return immediately for any object that isn't self
        if obj is not self:
            return super().eventFilter(obj, event)
            
        if (event.type() == QEvent.Type.MouseMove
                and not self.isMaximized()):
            try:
                pos = event.position().toPoint()
                edge = self._get_edge(pos)
                self._update_cursor(edge)
            except Exception:
                pass
        return super().eventFilter(obj, event)

    def _load_library_from_cache(self):
        """Load library_cache.json synchronously on startup for instant display."""
        import json
        cache_path = Path.home() / ".wingosy" / "library_cache.json"
        if not cache_path.exists():
            return
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Validate — must be a list of dicts
            if not isinstance(data, list):
                logging.warning("[Library] Cache invalid format — skipping")
                return

            # Filter out any non-dict entries
            games = [g for g in data if isinstance(g, dict)]

            if not games:
                logging.warning("[Library] Cache empty or all entries invalid")
                return

            self.all_games = games
            self._update_platform_filter(games)
            self.library_tab.populate_games(
                games,
                status=f"📚 Loaded from cache ({len(games)} games)"
            )
            logging.info(f"[Library] Cache loaded: {len(games)} games")
        except Exception as e:
            logging.warning(f"[Library] Cache load failed: {e}")

    def _get_edge(self, pos):
        edges = []
        b = self._resize_border
        if pos.x() < b:
            edges.append(Qt.Edge.LeftEdge)
        if pos.x() > self.width() - b:
            edges.append(Qt.Edge.RightEdge)
        if pos.y() < b:
            edges.append(Qt.Edge.TopEdge)
        if pos.y() > self.height() - b:
            edges.append(Qt.Edge.BottomEdge)
        return edges

    def _update_cursor(self, edges):
        left  = Qt.Edge.LeftEdge  in edges
        right = Qt.Edge.RightEdge in edges
        top   = Qt.Edge.TopEdge   in edges
        bot   = Qt.Edge.BottomEdge in edges

        if (left and top) or (right and bot):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif (right and top) or (left and bot):
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif left or right:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif top or bot:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and not self.isMaximized()):
            edges = self._get_edge(event.position().toPoint())
            if edges:
                combined = edges[0]
                for e in edges[1:]:
                    combined = combined | e
                try:
                    self.windowHandle().startSystemResize(combined)
                    event.accept()
                    return
                except Exception:
                    pass
        super().mousePressEvent(event)

    def _on_image_fetched(self, fetcher, generation=None):
        if generation is not None and generation != self.fetch_generation:
            if fetcher in self.active_image_fetchers:
                self.active_image_fetchers.remove(fetcher)
            return
        if fetcher in self.active_image_fetchers:
            self.active_image_fetchers.remove(fetcher)
        if self.image_fetch_queue:
            next_card = self.image_fetch_queue.pop(0)
            new_fetcher = next_card.start_image_fetch(self, self.fetch_generation)
            if new_fetcher:
                self.active_image_fetchers.append(new_fetcher)

    def fetch_library_and_populate(self, force_refresh=False):
        """
        force_refresh=False (default): show cache instantly, 
                                        refresh in background silently.
        force_refresh=True: wipe cache display, fetch fresh from server.
        """
        self.library_tab.refresh_btn.setEnabled(False)
        self.library_tab.retry_btn.setVisible(False)
        self._library_fetch_done = False
        
        if not force_refresh:
            # Step A — Load from cache immediately
            cached, _ = self.client.load_library_cache()
            if cached:
                self.all_games = cached
                # Ensure platform filter is updated for cached games (saves/restores current)
                self._update_platform_filter(cached)
                # Respect current filters instead of showing all
                self.library_tab.apply_filters()
                self.log(f"📦 Loaded {len(cached)} games from cache.")
            else:
                self.log("🔄 Loading library...")
        else:
            self.log("🔄 Force refresh — fetching from server...")
            self.all_games = []
            self.library_tab.populate_grid([]) # Clear grid for fresh fetch

        # Step B — Show status
        self.library_tab.set_status("Connecting to RomM server...")

        # Step C — Start worker
        cached_non_empty = len(self.all_games) > 0
        self._fetch_thread = LibraryFetchWorker(self.client, cached_non_empty=cached_non_empty)
        self._fetch_thread.finished.connect(self._on_library_fetched)
        self._fetch_thread.error.connect(lambda: self.library_tab.set_status("Could not connect to RomM server. Check your settings.", color="#b71c1c"))
        self._fetch_thread.retrying.connect(lambda: self.library_tab.set_status("Server is slow, retrying with longer timeout... (this may take a few minutes)", color="#e65100"))
        self._fetch_thread.batch_ready.connect(self._on_library_batch)
        self._fetch_thread.start()

    def _on_library_batch(self, batch, total):
        """Called as each page arrives from parallel fetcher."""
        if self._library_fetch_done: return
        
        # Avoid duplication if we are building on top of cache 
        # (server data replaces cache batch-by-batch)
        # For simplicity in this progressive view, if we're not force-refreshing,
        # we might just wait for final fetch. But user wants progressive.
        
        # If this is the FIRST batch of a fresh fetch or first launch:
        is_first_batch = (len(self.all_games) == 0 or len(self.all_games) == len(batch))
        
        if is_first_batch:
            self.all_games = list(batch)
            self.library_tab.populate_grid(self.all_games)
        else:
            # Append subsequent batches
            self.all_games.extend(batch)
            self.library_tab.append_batch(batch)
        
        # Update status
        self.library_tab.set_status(f"Loading library... ({len(self.all_games)} / {total} games)")

    def _on_library_fetched(self, res):
        self._library_fetch_done = True
        self.library_tab.set_status(None) # Hide
        self.library_tab.refresh_btn.setEnabled(True)
        
        if res == "REAUTH_REQUIRED":
            QMessageBox.warning(self, "Session Expired", 
                "Your session has expired. Please log in again.")
            self.open_settings()
            return
        
        if res is None:
            self.log("❌ Failed to fetch library from server.")
            self.library_tab.set_status("Connection failed.", color="#b71c1c")
            return
        
        if not isinstance(res, list):
            self.log("❌ Unexpected response from server. Check your RomM version.")
            return
        
        self.log(f"✅ Library fully loaded: {len(res)} games")
        # Ensure final state is correct (in case batches arrived out of order or were incomplete)
        self.all_games = res
        self._update_platform_filter(res)
        # Final render to ensure everything is in place
        self.library_tab.apply_filters()

    def _update_platform_filter(self, games):
        platforms = sorted(set(
            g.get('platform_display_name') for g in games
            if g.get('platform_display_name')
        ))
        self.library_tab.platform_filter.blockSignals(True)
        previously_selected = self.library_tab.platform_filter.currentText()
        self.library_tab.platform_filter.clear()
        self.library_tab.platform_filter.addItem("All Platforms")
        self.library_tab.platform_filter.addItems(platforms)
        
        # Add No Emulator filter if needed
        all_known = set()
        for emu in emulators.load_emulators():
            all_known.update(emu.get("platform_slugs", []))
            
        has_unknown = any(g.get("platform_slug") not in all_known for g in games)
        if has_unknown:
            self.library_tab.platform_filter.addItem("⚠️ No Emulator")
            
        idx = self.library_tab.platform_filter.findText(previously_selected)
        if idx >= 0:
            self.library_tab.platform_filter.setCurrentIndex(idx)
        self.library_tab.platform_filter.blockSignals(False)

    def _populate_from_games(self, games, is_progressive=False):
        """Populate the UI with a list of games. Called from cache or fresh fetch."""
        # Optimization: If the games list is identical to what we have, 
        # only update if we were previously empty
        self.all_games = games
        
        if not games:
            self._show_empty_library_message(
                "No games found. Check your RomM library or platform filter.")
            return

        # Only rebuild the platform list if not in progressive mode (avoid jitter)
        if not is_progressive:
            self._update_platform_filter(games)
        
        # Force a visual rebuild to update indicators (local exists, etc)
        # But if progressive, only rebuild if it's the first batch or platform changed
        if not is_progressive:
            if hasattr(self.library_tab, '_current_platform'):
                delattr(self.library_tab, '_current_platform')
        
        # Respect current filters instead of showing all
        self.library_tab.apply_filters()

    def _show_empty_library_message(self, message):
        self.library_tab.show_empty_message(message)

    def open_fw(self, emu_name):
        # Local import to avoid circular dependency with dialogs.py
        from src.ui.dialogs import GameDetailDialog 
        all_emus = emulators.load_emulators()
        emu_data = next((e for e in all_emus if e["name"] == emu_name), None)
        if not emu_data: return
        
        slugs = emu_data.get("platform_slugs", [])
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{emu_name} BIOS / Firmware")
        dialog.resize(600, 500)
        layout = QVBoxLayout(dialog)
        
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("Search Library:"))
        # Use first slug as default search, or 'bios'
        default_term = slugs[0] if slugs and slugs[0] != "multi" else "bios"
        self.fw_search_input = QLineEdit(default_term)
        search_layout.addWidget(self.fw_search_input)
        search_btn = QPushButton("Search")
        search_layout.addWidget(search_btn)
        layout.addLayout(search_layout)
        
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        container = QWidget()
        list_layout = QVBoxLayout(container)
        list_layout.setAlignment(Qt.AlignTop)
        scroll_area.setWidget(container)
        layout.addWidget(scroll_area)

        def perform_search():
            for i in reversed(range(list_layout.count())):
                item = list_layout.itemAt(i)
                if item and item.widget():
                    item.widget().setParent(None)
            
            term = self.fw_search_input.text().lower()
            firmwares = self.client.get_firmware()
            matches = [f for f in firmwares if term in f.get('file_name', '').lower() or term in f.get('platform_name', '').lower() or term in f.get('platform_slug', '')]
            
            for game in self.client.user_games:
                if term in game.get('name', '').lower() or term in game.get('fs_name', '').lower():
                    files = game.get('files', [])
                    if files:
                        matches.append({'id': game['id'], 'file_name': files[0].get('file_name'), 'platform_name': game.get('platform_display_name', 'Library'), 'is_rom': True})
            
            if not matches:
                list_layout.addWidget(QLabel("No results found."))
                return
                
            platforms_map = {}
            for fw in matches:
                p = fw.get('platform_name', 'Other')
                if p not in platforms_map: platforms_map[p] = []
                platforms_map[p].append(fw)
                
            for plat_name, files in platforms_map.items():
                if len(files) > 1:
                    group = QWidget()
                    gl = QVBoxLayout(group)
                    group.setStyleSheet("background: #333; border-radius: 5px; margin: 5px;")
                    gl.addWidget(QLabel(f"<b>{plat_name} ({len(files)} files)</b>"))
                    dl_set_btn = QPushButton("Download Full Set")
                    dl_set_btn.clicked.connect(lambda checked, f_list=files: self.dl_fw_list(emu_name, f_list, dialog))
                    gl.addWidget(dl_set_btn)
                    list_layout.addWidget(group)
                else:
                    fw = files[0]
                    row = QWidget()
                    row_layout = QHBoxLayout(row)
                    row_layout.addWidget(QLabel(f"{fw['file_name']} ({fw['platform_name']})"))
                    dl_btn = QPushButton("Download")
                    dl_btn.clicked.connect(lambda checked, f=fw: self.dl_fw(emu_name, f, dialog))
                    row_layout.addWidget(dl_btn)
                    list_layout.addWidget(row)

        search_btn.clicked.connect(perform_search)
        perform_search()
        
        button_box = QDialogButtonBox(QDialogButtonBox.Close, dialog)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        dialog.exec()

    def dl_fw_list(self, emu_name, fw_list, dialog):
        count = 0
        for fw in fw_list:
            if self.start_fw_download(emu_name, fw): count += 1
        self.log(f"✨ BIOS Sync: {count} downloads started.")
        dialog.accept()

    def dl_fw(self, emu_name, fw, dialog):
        if self.start_fw_download(emu_name, fw): dialog.accept()

    def start_fw_download(self, emu_name, fw):
        try:
            all_emus = emulators.load_emulators()
            emu_data = next((e for e in all_emus if e["name"] == emu_name), None)
            if not emu_data: return False

            emu_path = emu_data.get("executable_path")
            emu_folder = emu_data.get("id")
            suggested = Path(emu_path).parent / "bios" if emu_path else Path(self.config.get("base_emu_path")) / emu_folder / "bios"
            os.makedirs(suggested, exist_ok=True)
            target_path = suggested / fw['file_name']
            self.log(f"🚀 BIOS: {fw['file_name']}...")
            fw_dl = BiosDownloader(self.client, fw, str(target_path))
            self.download_queue.add_download(f"BIOS: {fw['file_name']}", fw_dl)
            
            fw_dl.progress.connect(lambda p, s: self.log(f"DL BIOS: {p}% @ {format_speed(s)}"))
            fw_dl.finished.connect(lambda ok, p: self.log(f"✨ BIOS saved to {p}") if ok else self.log(f"❌ BIOS failed: {p}"))
            fw_dl.finished.connect(lambda: self.download_queue.remove_download(fw_dl))
            fw_dl.finished.connect(lambda t=fw_dl: self.active_threads.remove(t) if t in self.active_threads else None)
            self.active_threads.append(fw_dl)
            fw_dl.start()
            return True
        except Exception as e:
            self.log(f"❌ Error starting BIOS download: {e}")
            return False

    def dl_emu(self, name):
        # NOTE: Emulator downloading logic will need update to support schema-based URLs
        # For now, we'll maintain the current logic but use the new list
        try:
            all_emus = emulators.load_emulators()
            emu_data = next((e for e in all_emus if e["name"] == name), None)
            if not emu_data: return

            # Emulator downloading depends on metadata that's not in the new schema yet
            # We'll stick to what we have but it might need hardcoded data for now
            # or we should have kept those fields in DEFAULT_EMULATORS
            pass
        except Exception as e:
            self.log(f"❌ Error starting emulator download: {e}")

    def st_ep(self, name):
        # This is now handled in EmulatorsTab.edit_emulator_path
        pass

    @Slot(str, str)
    def on_path(self, name, path):
        all_emus = emulators.load_emulators()
        updated = False
        for emu in all_emus:
            if name.lower() in emu['name'].lower():
                emu['executable_path'] = path
                updated = True
                break
        if updated:
            emulators.save_emulators(all_emus)
            self.emulators_tab.populate_emus()
    def sy_ec(self, name, mode):
        try:
            emu_data = self.config.get("emulators")[name]
            path = emu_data.get("config_path")
            if not path: return
            
            if mode == "export":
                if not os.path.exists(path):
                    QMessageBox.warning(self, "Export Failed", f"Config path does not exist: {path}")
                    return
                
                target_zip, _ = QFileDialog.getSaveFileName(self, f"Export {name} Config", f"{name}_config.zip", "ZIP Files (*.zip)")
                if target_zip:
                    self.log(f"🔄 Exporting {name} config to {target_zip}...")
                    from src.utils import zip_path
                    zip_path(path, target_zip)
                    self.log(f"✨ {name} config exported.")
            
            elif mode == "import":
                source_zip, _ = QFileDialog.getOpenFileName(self, f"Import {name} Config", "", "ZIP Files (*.zip)")
                if source_zip:
                    self.log(f"🔄 Importing {name} config from {source_zip}...")
                    if os.path.exists(path):
                        shutil.move(path, f"{path}.bak")
                    
                    with zipfile.ZipFile(source_zip, 'r') as z:
                        z.extractall(Path(path).parent)
                    self.log(f"✨ {name} config restored!")
                    
        except Exception as e:
            self.log(f"❌ Config operation error: {e}")

    def log(self, message):
        self.log_area.append(message)

    @Slot(str, str, str, str)
    def handle_conflict(self, title, local_path, temp_dl, rom_id):
        dialog = ConflictDialog(title, self)
        if dialog.exec() == QDialog.Accepted:
            mode = dialog.result_mode
            # Only skip next pull if user explicitly chose to keep their local file
            if mode == "local":
                print(f"[PULL DEBUG] User chose Keep Local. Setting skip_next_pull for {rom_id}")
                self.watcher.skip_next_pull_rom_id = str(rom_id)
            else:
                self.watcher.skip_next_pull_rom_id = None

            # Always clear it after 30 seconds max to prevent it sticking forever
            QTimer.singleShot(30000, lambda: setattr(
                self.watcher, 'skip_next_pull_rom_id', None))

            if mode == "cloud":
                t = ConflictResolveThread(self.watcher, rom_id, title, local_path, os.path.isdir(local_path))
                t.finished.connect(lambda ok: self.log("✅ Cloud save applied." if ok else "❌ Cloud save apply failed."))
                t.finished.connect(lambda t=t: self.active_threads.remove(t) if t in self.active_threads else None)
                self.active_threads.append(t)
                t.start()
            elif mode == "both":
                cloud_bak = str(local_path) + ".cloud_backup"
                if os.path.exists(cloud_bak):
                    if os.path.isdir(cloud_bak): shutil.rmtree(cloud_bak, ignore_errors=True)
                    else: os.remove(cloud_bak)
                shutil.copy2(temp_dl, cloud_bak) if not os.path.isdir(temp_dl) else shutil.copytree(temp_dl, cloud_bak)
                self.log(f"📁 Cloud save backed up to: {cloud_bak}")
        if os.path.exists(temp_dl):
            try: os.remove(temp_dl) if not os.path.isdir(temp_dl) else shutil.rmtree(temp_dl, ignore_errors=True)
            except: pass

    @Slot(str, str)
    def show_notification(self, title, msg):
        self.tray_icon.showMessage(title, msg, QSystemTrayIcon.Information, 3000)

    def open_settings(self):
        SettingsDialog(self.config, self, self).exec()

    def ensure_watcher_running(self):
        if not self.watcher:
            self.watcher = self.watcher_class(self.client, self.config)
            self.watcher.log_signal.connect(self.log)
            self.watcher.path_detected_signal.connect(self.on_path)
            self.watcher.conflict_signal.connect(self.handle_conflict, Qt.QueuedConnection)
            self.watcher.notify_signal.connect(self.show_notification)
            self.watcher.start()

    def setup_tray(self):
        icon_path = get_resource_path("icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon(QPixmap(32, 32))
        self.tray_icon = QSystemTrayIcon(icon, self)
        menu = QMenu()
        menu.addAction("Show", self.showNormal)
        menu.addAction("Exit", QApplication.instance().quit)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.show()

    def closeEvent(self, event):
        # Stop watcher thread gracefully
        if hasattr(self, 'watcher') and self.watcher:
            self.watcher.running = False
            self.watcher.quit()
            self.watcher.wait(3000)  # wait up to 3 seconds
        
        # Stop library fetch worker if running
        if hasattr(self, '_fetch_thread') and self._fetch_thread.isRunning():
            self._fetch_thread.quit()
            self._fetch_thread.wait(2000)
        
        settings = QSettings("Wingosy", "WingosyLauncher")
        settings.setValue("geometry", self.saveGeometry())
        if self.tray_icon.isVisible():
            self.hide()
            event.ignore()
        else:
            event.accept()

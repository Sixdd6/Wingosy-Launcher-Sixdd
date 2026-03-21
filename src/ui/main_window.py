import os
import sys
import shutil
import zipfile
import logging
import time
from pathlib import Path

from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLabel, QPushButton, QTabWidget, QTextEdit, 
                             QApplication, QFileDialog, 
                             QDialog, QLineEdit, QDialogButtonBox, 
                             QScrollArea, QFrame)
from PySide6.QtGui import QIcon, QPixmap, QKeySequence, QShortcut, QDesktopServices
from PySide6.QtCore import Qt, QSettings, Slot, Signal, QThread, QTimer, QEvent, QPoint, QRect, QUrl

from src.ui.threads import (ImageFetcher, BiosDownloader, DolphinDownloader, 
                            DirectDownloader, GithubDownloader, ConflictResolveThread,
                            LocalDiscoveryWorker)
from src.ui.widgets import get_resource_path, DownloadQueueWidget, format_speed, format_size
from src.ui.dialogs.settings_helpers import WelcomeDialog
from src.ui.dialogs.styled_messagebox import StyledMessageBox
from src.ui.dialogs import ConflictDialog
from src.ui.dialogs.emulator_editor import AssetPickerDialog
from src.emulator_sources import EMULATOR_SOURCES
from src.ui.tabs.library import LibraryTab
from src.ui.tabs.emulators import EmulatorsTab
from src.ui.tabs.settings import SettingsTab
from src.utils import zip_path, resolve_local_rom_path
from src.platforms import RETROARCH_PLATFORMS, platform_matches
from src import emulators
from src import download_registry

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
            # Just emit the batch, discovery happens in background later
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
    startup_ready = Signal()
    initial_library_ready = Signal(bool)

    def __init__(self, config_manager, client, watcher_class, version):
        super().__init__()
        self.config, self.client, self.watcher_class, self.version = config_manager, client, watcher_class, version
        self.tray_icon = None
        self.watcher = None
        self.active_threads = []
        self.image_fetch_queue = []
        self.active_image_fetchers = []
        self.fetch_generation = 0
        self.all_games = []
        self._loaded_game_ids = set()
        self._force_refreshing = False
        self._frame_applied = False
        self._last_frame_apply_ts = 0.0
        self._restore_maximized = False
        self._restore_maximized_applied = False
        
        # Custom window frame setup
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)

        # Apply Windows frame tweaks as early as possible to avoid a visible frame transition
        try:
            self.setAttribute(Qt.WA_NativeWindow, True)
            self.winId()
            self._apply_windows_frame()
        except Exception:
            pass

        self.setWindowTitle("Wingosy Launcher")
        self.resize(1100, 800)
        settings = QSettings("Wingosy", "WingosyLauncher")
        try:
            self._restore_maximized = bool(settings.value("was_maximized", False))
        except Exception:
            self._restore_maximized = False

        normal_geometry = settings.value("normal_geometry")
        if isinstance(normal_geometry, QRect) and not normal_geometry.isNull():
            self.setGeometry(normal_geometry)
        else:
            geometry = settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)

        QTimer.singleShot(50, self._ensure_window_within_screen)
        
        icon_path = get_resource_path("assets/icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
            
        self.setup_ui()
        self.ensure_watcher_running()

        # 1. Load cache immediately — show games NOW
        self._load_library_from_cache()
        # 2. Then fetch fresh data in background
        QTimer.singleShot(500, self.fetch_library_and_populate)

        self._pending_keyring_warning = bool(self.config.data.get("keyring_failed"))
        self._pending_welcome = bool(self.config.get("first_run", True))

        self._initial_library_signal_emitted = False

        QTimer.singleShot(0, self.startup_ready.emit)

    def _emit_initial_library_ready(self, ok: bool):
        if self._initial_library_signal_emitted:
            return
        self._initial_library_signal_emitted = True
        self.initial_library_ready.emit(bool(ok))

    def _show_post_startup_dialogs(self):
        if self._pending_keyring_warning:
            StyledMessageBox.warning(
                self,
                "Credential Storage Warning",
                "Your system's secure credential manager is unavailable.\n\n"
                "Wingosy has stored your login token using local encryption instead.\n\n"
                "This is less secure than keyring. Consider enabling your OS keyring."
            )
            self.config.data.pop("keyring_failed", None)
            self._pending_keyring_warning = False

        if self._pending_welcome:
            WelcomeDialog(self).exec()
            self.config.set("first_run", False)
            self._pending_welcome = False

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._show_post_startup_dialogs)
        if self._restore_maximized_applied:
            return
        if sys.platform != "win32":
            return
        if not self._restore_maximized:
            return

        self._restore_maximized_applied = True
        QTimer.singleShot(0, self.showMaximized)

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
        main_layout.addWidget(self.title_bar)
        self.title_bar.set_maximized(self.isMaximized())
        
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
        self.info_tabs.setStyleSheet("""
            QTabWidget::pane {
                border: none;
                background: #1a1a1a;
            }
            QTabWidget > QTabBar {
                background: #1a1a1a;
                border-bottom: 1px solid #2d2d2d;
            }
            QTabBar::tab {
                background: transparent;
                color: #aaaaaa;
                font-size: 11px;
                padding: 8px 20px;
                border: none;
                border-bottom: 2px solid transparent;
                min-width: 80px;
            }
            QTabBar::tab:selected {
                color: #ffffff;
                border-bottom: 2px solid #0d6efd;
                background: transparent;
            }
            QTabBar::tab:hover {
                color: #dddddd;
                background: rgba(255,255,255,0.04);
            }
            QTabBar::scroller {
                width: 0px;
            }
        """)
        self.download_queue = DownloadQueueWidget()
        self.download_queue.refresh_from_registry()
        self.info_tabs.addTab(self.download_queue, "📥 Downloads")        

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setStyleSheet("background: #121212; color: #bbdefb; font-family: Consolas; border: none;")
        self.info_tabs.addTab(self.log_area, "📋 Logs")

        self.tabs.addTab(self.info_tabs, "Logs")
        
        self.settings_tab = SettingsTab(self)
        self.tabs.addTab(self.settings_tab, "Settings")
        
        main_layout.addWidget(self.tabs)
        
        # Shortcuts
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.library_tab.search_input.setFocus)
        QShortcut(QKeySequence("F5"), self, activated=self.fetch_library_and_populate)

    def changeEvent(self, event):
        if event.type() == QEvent.WindowStateChange and hasattr(self, "title_bar"):
            self.title_bar.set_maximized(self.isMaximized())
            QTimer.singleShot(0, self._apply_windows_frame)

        super().changeEvent(event)

    def _on_tab_changed(self, index):
        self.tabs.setCurrentIndex(index)
        self.title_bar.set_active_tab(index)
        if index == 0:  # Library
            self.library_tab.refresh_card_states()

    def eventFilter(self, obj, event):
        return super().eventFilter(obj, event)

    def _ensure_window_within_screen(self):
        try:
            if self.isMaximized() or self.isFullScreen():
                return

            frame_rect = self.frameGeometry()
            client_rect = self.geometry()
            if frame_rect.isNull() or client_rect.isNull() or frame_rect.width() <= 0 or frame_rect.height() <= 0:
                return

            screen = QApplication.screenAt(frame_rect.center()) or self.screen() or QApplication.primaryScreen()
            if screen is None:
                return

            avail = screen.availableGeometry()
            if avail.isNull() or avail.width() <= 0 or avail.height() <= 0:
                return

            w = min(frame_rect.width(), avail.width())
            h = min(frame_rect.height(), avail.height())

            x = frame_rect.x()
            y = frame_rect.y()

            if x < avail.left():
                x = avail.left()
            if y < avail.top():
                y = avail.top()
            if x + w > avail.right():
                x = max(avail.left(), avail.right() - w + 1)
            if y + h > avail.bottom():
                y = max(avail.top(), avail.bottom() - h + 1)

            new_frame = QRect(x, y, w, h)
            if new_frame != frame_rect:
                left_delta = client_rect.left() - frame_rect.left()
                top_delta = client_rect.top() - frame_rect.top()
                right_delta = frame_rect.right() - client_rect.right()
                bottom_delta = frame_rect.bottom() - client_rect.bottom()

                new_client = QRect(
                    new_frame.left() + left_delta,
                    new_frame.top() + top_delta,
                    max(1, new_frame.width() - left_delta - right_delta),
                    max(1, new_frame.height() - top_delta - bottom_delta),
                )
                self.setGeometry(new_client)
        except Exception:
            return

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
            
            # Start background discovery
            self._start_local_discovery(games)
        except Exception as e:
            logging.warning(f"[Library] Cache load failed: {e}")

    def _start_local_discovery(self, games):
        if hasattr(self, '_discovery_worker') and self._discovery_worker.isRunning():
            self._discovery_worker.stop()
            self._discovery_worker.wait()
        
        self._discovery_worker = LocalDiscoveryWorker(games, self.config.data)
        self._discovery_worker.rom_discovered.connect(self._on_rom_discovered)
        self._discovery_worker.rom_missing.connect(self._on_rom_missing)
        self._discovery_worker.finished_discovery.connect(self._on_local_discovery_finished)
        if hasattr(self, 'title_bar'):
            self.title_bar.set_activity("Scanning local ROMs...")
        self._discovery_worker.start()

    def _on_local_discovery_finished(self):
        if hasattr(self, 'title_bar'):
            self.title_bar.clear_activity()

    @Slot(int, str)
    def _on_rom_discovered(self, game_id, local_path):
        # Update flag in main list
        for g in self.all_games:
            if g.get('id') == game_id:
                g['_local_exists'] = True
                break
        
        # Update UI if library tab is showing this game
        self.library_tab.update_game_local_status(game_id, True)

    @Slot(int)
    def _on_rom_missing(self, game_id):
        # Clear flag in main list (important for Windows games where install state can change)
        for g in self.all_games:
            if g.get('id') == game_id:
                g['_local_exists'] = False
                break

        # Update UI if library tab is showing this game
        self.library_tab.update_game_local_status(game_id, False)

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
        self.library_tab.search_input.setEnabled(False)
        self.library_tab.platform_filter.setEnabled(False)
        self._library_fetch_done = False
        
        if not force_refresh:
            # Step A — Load from cache immediately
            cached, _ = self.client.load_library_cache()
            if cached:
                cached = [g for g in cached if isinstance(g, dict)]
                self.all_games = cached
                self._loaded_game_ids = {g.get('id') for g in cached if g.get('id') is not None}
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
            self._loaded_game_ids = set()
            self.library_tab.populate_grid([]) # Clear grid for fresh fetch

        self._force_refreshing = bool(force_refresh)

        # Step B — Show status
        self.library_tab.set_status("Connecting to RomM server...")
        if hasattr(self, 'title_bar'):
            self.title_bar.set_activity("Loading library...")

        # Step C — Start worker
        cached_non_empty = len(self.all_games) > 0
        self._fetch_thread = LibraryFetchWorker(self.client, cached_non_empty=cached_non_empty)
        self._fetch_thread.finished.connect(self._on_library_fetched)
        self._fetch_thread.error.connect(self._on_library_fetch_error)
        self._fetch_thread.retrying.connect(lambda: self.library_tab.set_status("Server is slow, retrying with longer timeout... (this may take a few minutes)", color="#e65100"))
        self._fetch_thread.batch_ready.connect(self._on_library_batch)
        self._fetch_thread.start()

    def _on_library_fetch_error(self):
        self.library_tab.set_status("Could not connect to RomM server. Check your settings.", color="#b71c1c")
        self.library_tab.refresh_btn.setEnabled(True)
        self.library_tab.search_input.setEnabled(True)
        self.library_tab.platform_filter.setEnabled(True)
        if hasattr(self, 'title_bar'):
            self.title_bar.clear_activity()
        self._emit_initial_library_ready(False)

    def _on_library_batch(self, batch, total):
        """Called as each page arrives from parallel fetcher."""
        if self._library_fetch_done: return
        
        # Avoid duplication if we are building on top of cache 
        # (server data replaces cache batch-by-batch)
        # For simplicity in this progressive view, if we're not force-refreshing,
        # we might just wait for final fetch. But user wants progressive.
        
        if self._force_refreshing and not self._loaded_game_ids:
            already_found = {g['id'] for g in self.all_games if g.get('_local_exists')}
            self.all_games = list(batch)
            self._loaded_game_ids = {g.get('id') for g in self.all_games if g.get('id') is not None}
            for g in self.all_games:
                if g.get('id') in already_found:
                    g['_local_exists'] = True
            self.library_tab.populate_grid(self.all_games)
        else:
            unique_batch = []
            for g in batch:
                gid = g.get('id') if isinstance(g, dict) else None
                if gid is None or gid in self._loaded_game_ids:
                    continue
                self._loaded_game_ids.add(gid)
                unique_batch.append(g)

            if unique_batch:
                self.all_games.extend(unique_batch)
                self.library_tab.append_batch(unique_batch)
        
        # Update status
        loaded = len(self._loaded_game_ids)
        if isinstance(total, int) and total >= 0:
            loaded = min(loaded, total)
        if isinstance(total, int) and total >= 0 and loaded >= total:
            self.library_tab.set_status("Rendering library...")
            if hasattr(self, 'title_bar'):
                self.title_bar.set_activity("Rendering library...")
        else:
            self.library_tab.set_status(f"Loading library... ({loaded} / {total} games)")
            if hasattr(self, 'title_bar'):
                self.title_bar.set_activity(f"Loading library... ({loaded}/{total})")

    def _on_library_fetched(self, res):
        self._library_fetch_done = True
        self.library_tab.set_status("Building library view...")
        if hasattr(self, 'title_bar'):
            self.title_bar.set_activity("Building library view...")
        
        if res == "REAUTH_REQUIRED":
            StyledMessageBox.warning(self, "Session Expired", 
                "Your session has expired. Please log in again.")
            self._on_tab_changed(3) # Settings
            self._emit_initial_library_ready(False)
            return
        
        if res is None:
            self.log("❌ Failed to fetch library from server.")
            self.library_tab.set_status("Connection failed.", color="#b71c1c")
            self._emit_initial_library_ready(False)
            return
        
        if not isinstance(res, list):
            self.log("❌ Unexpected response from server. Check your RomM version.")
            self._emit_initial_library_ready(False)
            return

        self.log(f"✅ Library fully loaded: {len(res)} games")
        already_found = {g['id'] for g in self.all_games if g.get('_local_exists')}
        QTimer.singleShot(0, lambda r=res, found=already_found: self._finalize_library_loaded(r, found))

    def _finalize_library_loaded(self, res, already_found):
        # Ensure final state is correct (in case batches arrived out of order or were incomplete)
        self.all_games = res
        self._loaded_game_ids = {g.get('id') for g in self.all_games if g.get('id') is not None}
        for g in self.all_games:
            if g.get('id') in already_found:
                g['_local_exists'] = True

        self._update_platform_filter(res)
        # Final render to ensure everything is in place
        self.library_tab.apply_filters()

        self.library_tab.set_status(None) # Hide
        self.library_tab.refresh_btn.setEnabled(True)
        self.library_tab.search_input.setEnabled(True)
        self.library_tab.platform_filter.setEnabled(True)
        if hasattr(self, 'title_bar'):
            self.title_bar.clear_activity()

        self._start_local_discovery(self.all_games)

        self._emit_initial_library_ready(True)

    def _update_platform_filter(self, games):
        platforms = sorted(set(
            g.get('platform_display_name') for g in games
            if g.get('platform_display_name')
        ))
        self.library_tab.platform_filter.blockSignals(True)
        previously_selected = getattr(self.library_tab, "_platform_selection", None) or self.library_tab.platform_filter.currentText()
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
        else:
            self.library_tab.platform_filter.setCurrentIndex(0)
            previously_selected = "All Platforms"

        self.library_tab._platform_selection = previously_selected
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
        
        emu_id = emu_data.get("id", "").lower()
        
        EMULATOR_BIOS_PLATFORMS = {
            "eden":       ["switch", "nintendo-switch"],
            "rpcs3":      ["ps3", "playstation-3", "playstation3"],
            "pcsx2":      ["ps2", "playstation-2", "playstation2"],
            "duckstation":["ps", "psx", "playstation", "playstation-1"],
            "retroarch":  None,  # None = show ALL platforms (RetroArch supports everything)
            "dolphin":    ["gc", "ngc", "gamecube", "nintendo-gamecube", "wii", "nintendo-wii"],
            "cemu":       ["wiiu", "wii-u", "nintendo-wii-u"],
            "azahar":     ["n3ds", "3ds", "nintendo-3ds", "new-nintendo-3ds"],
            "melonds":    ["nds", "nintendo-ds"],
            "xemu":       ["xbox"],
            "xenia":      [],  # Xbox 360 has no BIOS files needed
            "xenia_canary": [],
        }

        allowed_platforms = EMULATOR_BIOS_PLATFORMS.get(emu_id, None)

        dialog = QDialog(self)
        dialog.setWindowTitle(f"RomM BIOS Manager — {emu_name}")
        dialog.resize(700, 600)
        layout = QVBoxLayout(dialog)
        
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        container = QWidget()
        list_layout = QVBoxLayout(container)
        list_layout.setAlignment(Qt.AlignTop)
        scroll_area.setWidget(container)
        layout.addWidget(scroll_area)

        def refresh():
            for i in reversed(range(list_layout.count())):
                item = list_layout.itemAt(i)
                if item and item.widget():
                    item.widget().setParent(None)
            
            # Re-fetch from API
            firmwares = self.client.get_firmware()
            
            # Filtering constants
            NO_BIOS_PLATFORMS = {
                "windows", "win", "pc", "pc-windows", "dos", "win95", "win98",
                "xbox360", "xbla", "xbox-360",
                "android", "ios", "mac", "linux"
            }
            GAME_ROM_EXTENSIONS = {
                '.cia', '.nsp', '.xci',           # Switch/3DS installable games
                '.z64', '.n64', '.v64',           # N64 ROMs  
                '.sfc', '.smc', '.fig',           # SNES ROMs
                '.nes', '.fds',                   # NES ROMs (fds is Famicom Disk, NOT a BIOS)
                '.gba', '.gbc', '.gb',            # Game Boy ROMs
                '.nds', '.3ds',                   # DS/3DS ROMs
                '.gen', '.md', '.smd',            # Genesis ROMs
                '.iso', '.chd', '.cso', '.pbp',   # Disc images that are games not BIOS
                '.7z', '.rar',                    # Archives (BIOS files are rarely archived)
            }
            FILE_NAME_BLOCKLIST = ["python", "java", "readme", "license", "changelog", "install", "setup", "update"]

            if allowed_platforms == []:
                msg = QLabel(f"No BIOS files required for {emu_name}.")
                msg.setAlignment(Qt.AlignCenter)
                msg.setStyleSheet("color: #aaa; margin: 40px; font-size: 14px;")
                list_layout.addWidget(msg)
                return

            platforms_map = {}
            # Debug tracking
            stats = {
                "total_firmware_items": len(firmwares),
                "platforms_with_firmware": set(),
                "skipped_no_bios_platform": 0,
                "skipped_game_extension": 0,
                "skipped_not_for_emu": 0,
                "skipped_blocklist": 0,
                "skipped_pattern_mismatch": 0
            }

            for fw in firmwares:
                p_slug = str(fw.get('platform_slug', '')).lower()
                p_name = fw.get('platform_name') or fw.get('platform_display_name') or 'Other'
                stats["platforms_with_firmware"].add(p_slug)
                
                # Filter A: Exclude non-BIOS platforms
                if p_slug in NO_BIOS_PLATFORMS:
                    stats["skipped_no_bios_platform"] += 1
                    continue

                # Filter B: Emulator specific platform filtering
                if allowed_platforms is not None and p_slug not in allowed_platforms:
                    stats["skipped_not_for_emu"] += 1
                    continue

                f_name = fw.get('file_name', 'unknown')
                f_name_lower = f_name.lower()
                ext = Path(f_name).suffix.lower()
                size = fw.get('file_size_bytes') or 0
                
                # Filter C: Filename blocklist
                stem = Path(f_name).stem.lower()
                if stem in FILE_NAME_BLOCKLIST:
                    stats["skipped_blocklist"] += 1
                    continue

                # Filter D: Pattern mismatch (scph* belongs to PlayStation)
                if f_name_lower.startswith("scph") and "playstation" not in p_slug and "ps" not in p_slug:
                    stats["skipped_pattern_mismatch"] += 1
                    continue

                # Filter E: Exclude game ROMs by extension (with size caveat)
                if ext in GAME_ROM_EXTENSIONS and size > 16 * 1024 * 1024:
                    stats["skipped_game_extension"] += 1
                    continue

                if p_name not in platforms_map: platforms_map[p_name] = []
                platforms_map[p_name].append(fw)

            if not platforms_map:
                debug_info = (
                    f"DEBUG INFO:\n"
                    f"- Total items from API: {stats['total_firmware_items']}\n"
                    f"- Skipped (Non-BIOS platform): {stats['skipped_no_bios_platform']}\n"
                    f"- Skipped (Not for this emulator): {stats['skipped_not_for_emu']}\n"
                    f"- Skipped (Blocklist): {stats['skipped_blocklist']}\n"
                    f"- Skipped (Pattern mismatch): {stats['skipped_pattern_mismatch']}\n"
                    f"- Skipped (Game ROM >16MB): {stats['skipped_game_extension']}"
                )
                
                msg = QLabel(f"No BIOS files found for this emulator on your RomM server.\n\n{debug_info}")
                msg.setAlignment(Qt.AlignCenter)
                msg.setWordWrap(True)
                msg.setStyleSheet("color: #aaa; margin: 40px; font-size: 13px; line-height: 1.5;")
                list_layout.addWidget(msg)
                return

            for plat_name, files in platforms_map.items():
                group = QWidget()
                gl = QVBoxLayout(group)
                group.setStyleSheet("background: #2b2b2b; border: 1px solid #3d3d3d; border-radius: 8px; margin: 5px; padding: 10px;")
                
                header = QHBoxLayout()
                header.addWidget(QLabel(f"<b>{plat_name}</b> <font color='#888'>({len(files)} files)</font>"))
                header.addStretch()
                
                dl_all_btn = QPushButton(f"Download All for {plat_name}")
                dl_all_btn.setStyleSheet("padding: 4px 8px; font-size: 11px;")
                dl_all_btn.clicked.connect(lambda checked, f_list=files: self.dl_fw_list(emu_name, f_list, dialog))
                header.addWidget(dl_all_btn)
                gl.addLayout(header)
                
                # Add horizontal line
                line = QFrame()
                line.setFrameShape(QFrame.HLine)
                line.setFrameShadow(QFrame.Sunken)
                line.setStyleSheet("background-color: #3d3d3d; max-height: 1px;")
                gl.addWidget(line)

                for fw in files:
                    row = QWidget()
                    row_layout = QHBoxLayout(row)
                    row_layout.setContentsMargins(5, 2, 5, 2)
                    
                    # File info
                    name_lbl = QLabel(fw.get('file_name', 'unknown'))
                    name_lbl.setStyleSheet("font-weight: bold;")
                    row_layout.addWidget(name_lbl)
                    
                    size_val = fw.get('file_size_bytes')
                    size_str = format_size(size_val) if size_val else "Unknown size"
                    size_lbl = QLabel(size_str)
                    size_lbl.setStyleSheet("color: #888;")
                    row_layout.addWidget(size_lbl)
                    
                    row_layout.addStretch()

                    # PS3 Special Handling
                    if "PS3UPDAT.PUP" in str(fw.get('file_name', '')).upper() and "PS3" in plat_name.upper():
                        ps3_btn = QPushButton("Install Firmware")
                        ps3_btn.setStyleSheet("background: #007acc; color: white; font-weight: bold;")
                        ps3_btn.clicked.connect(lambda checked, f=fw: self.dl_fw(emu_name, f, dialog))
                        row_layout.addWidget(ps3_btn)
                    else:
                        dl_btn = QPushButton("Download")
                        dl_btn.clicked.connect(lambda checked, f=fw: self.dl_fw(emu_name, f, dialog))
                        row_layout.addWidget(dl_btn)
                    
                    gl.addWidget(row)
                
                list_layout.addWidget(group)

        refresh()
        
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

            emu_id = emu_data.get("id", "").lower()
            emu_path = emu_data.get("executable_path")

            def get_bios_dest(emu_id, emu_executable_path):
                emu_dir = Path(emu_executable_path).parent if emu_executable_path else None
                appdata = Path(os.environ.get('APPDATA', ''))
                localappdata = Path(os.environ.get('LOCALAPPDATA', ''))
                
                destinations = {
                    "eden":        [emu_dir / "user" / "nand" / "system" if emu_dir else None,
                                   appdata / "eden" / "nand" / "system"],
                    "rpcs3":       [emu_dir / "dev_flash" if emu_dir else None,
                                   appdata / "rpcs3" / "dev_flash"],
                    "pcsx2":       [emu_dir / "bios" if emu_dir else None,
                                   appdata / "PCSX2" / "bios"],
                    "duckstation": [localappdata / "DuckStation" / "bios",
                                   Path.home() / "Documents" / "DuckStation" / "bios"],
                    "dolphin":     [emu_dir / "User" / "GC" if emu_dir else None,
                                   Path.home() / "Documents" / "Dolphin Emulator" / "GC"],
                    "azahar":      [appdata / "Azahar" / "nand" / "00000000000000000000000000000000" / "title"],
                    "melonds":     [appdata / "melonDS"],
                    "retroarch":   [emu_dir / "system" if emu_dir else None],
                    "cemu":        [emu_dir / "keys" if emu_dir else None,
                                   appdata / "Cemu" / "keys"],
                }
                
                candidates = destinations.get(emu_id, [])
                # Return first existing directory
                for d in candidates:
                    if d and d.exists():
                        return d
                
                # If none exist, return first non-None candidate
                for d in candidates:
                    if d: return d
                return None

            suggested = get_bios_dest(emu_id, emu_path)
            
            if not suggested:
                # Fallback: prompt user to pick a folder
                folder = QFileDialog.getExistingDirectory(self, f"Select BIOS directory for {fw.get('platform_name', 'unknown')}", str(Path.home()))
                if not folder: return False
                suggested = Path(folder)
            
            os.makedirs(suggested, exist_ok=True)
            target_path = suggested / fw['file_name']
            
            # Special PS3 logic
            is_ps3_pup = "PS3UPDAT.PUP" in fw['file_name'].upper() and "PS3" in str(fw.get('platform_name', '')).upper()
            
            self.log(f"🚀 BIOS: {fw['file_name']}...")
            fw_dl = BiosDownloader(self.client, fw, str(target_path))
            self.download_queue.add_download(f"BIOS: {fw['file_name']}", fw_dl)
            
            fw_dl.progress.connect(lambda d, t, s: self.log(f"DL BIOS: {100*d/t if t > 0 else 0:.1f}% @ {format_speed(s)}"))
            
            def on_finished(ok, p, fw_item=fw, emu_d=emu_data):
                if ok:
                    self.log(f"✨ BIOS saved to {p}")
                    if is_ps3_pup and emu_d.get("executable_path"):
                        # Offer to install
                        res = StyledMessageBox.question(self, "Install PS3 Firmware", "PS3 Firmware downloaded. Would you like to launch RPCS3 to install it now?", StyledMessageBox.Yes | StyledMessageBox.No)
                        if res == StyledMessageBox.Yes:
                            import subprocess
                            try:
                                subprocess.Popen([emu_d["executable_path"], "--installfw", p])
                            except Exception as e:
                                self.log(f"❌ Failed to launch RPCS3: {e}")
                else:
                    self.log(f"❌ BIOS failed: {p}")

            fw_dl.finished.connect(on_finished)
            fw_dl.finished.connect(lambda: self.download_queue.remove_download(fw_dl))
            fw_dl.finished.connect(lambda t=fw_dl: self.active_threads.remove(t) if t in self.active_threads else None)
            self.active_threads.append(fw_dl)
            fw_dl.start()
            return True
        except Exception as e:
            self.log(f"❌ Error starting BIOS download: {e}")
            return False

    def dl_emu(self, name):
        try:
            all_emus = emulators.load_emulators()
            emu = next((e for e in all_emus if e.get("id") == name or e.get("name") == name), None)
            if not emu:
                self.log(f"❌ Unknown emulator: {name}")
                return

            source = EMULATOR_SOURCES.get(emu["id"])
            if not source:
                self.log(f"❌ No download source configured for {name}")
                return

            source_type = source.get("type")
            url = None
            if source_type == "github":
                repo = source.get("repo")
                if repo:
                    url = f"https://github.com/{repo}/releases/latest"
            elif source_type == "direct":
                url = source.get("url")
            elif source_type == "dolphin_api":
                url = "https://dolphin-emu.org/download/"

            if not url:
                self.log(f"❌ No update page configured for {emu.get('name') or name}")
                return

            self.log(f"🌐 Opening download page for {emu.get('name') or name}...")
            QDesktopServices.openUrl(QUrl(url))

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
                    StyledMessageBox.warning(self, "Export Failed", f"Config path does not exist: {path}")
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
        try:
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
        finally:
            if self.watcher:
                self.watcher._active_conflicts.discard(str(rom_id))

    @Slot(str, str)
    def show_notification(self, title, msg):
        pass

    def open_settings(self):
        self._on_tab_changed(3)

    def ensure_watcher_running(self):
        if not self.watcher:
            self.watcher = self.watcher_class(self.client, self.config)
            self.watcher.log_signal.connect(self.log)
            self.watcher.path_detected_signal.connect(self.on_path)
            self.watcher.conflict_signal.connect(self.handle_conflict, Qt.QueuedConnection)
            self.watcher.notify_signal.connect(self.show_notification)
            if hasattr(self.watcher, 'playtime_updated_signal'):
                self.watcher.playtime_updated_signal.connect(self._on_playtime_updated, Qt.QueuedConnection)
            if hasattr(self.watcher, 'sync_cache_updated_signal'):
                try:
                    handler = getattr(self.library_tab, '_on_sync_cache_updated', None)
                except Exception:
                    handler = None
                if handler:
                    try:
                        self.watcher.sync_cache_updated_signal.connect(handler, Qt.QueuedConnection)
                    except Exception:
                        pass
            self.watcher.start()

    @Slot(int, int)
    def _on_playtime_updated(self, rom_id, total_seconds):
        try:
            # Only update if details view is currently open for this ROM
            detail = getattr(self.library_tab, 'detail_panel', None)
            if not detail:
                return
            game = getattr(detail, 'game', {})
            if not isinstance(game, dict):
                return
            if game.get('id') != rom_id:
                return
            if hasattr(detail, 'set_playtime_seconds'):
                detail.set_playtime_seconds(total_seconds)
        except Exception:
            return

    def closeEvent(self, event):
        self._shutdown_threads()
        
        settings = QSettings("Wingosy", "WingosyLauncher")
        try:
            settings.setValue("was_maximized", self.isMaximized())
        except Exception:
            pass

        try:
            settings.setValue("normal_geometry", self.normalGeometry())
        except Exception:
            pass

        if not self.isMaximized() and not self.isFullScreen():
            settings.setValue("geometry", self.saveGeometry())
        event.accept()

    def _shutdown_threads(self):
        try:
            download_registry.shutdown_all(timeout_ms=1500)
        except Exception:
            pass

        # Stop watcher thread (and its background sync threads) gracefully
        try:
            watcher = getattr(self, 'watcher', None)
        except Exception:
            watcher = None
        if watcher:
            try:
                if hasattr(watcher, 'shutdown'):
                    watcher.shutdown(watcher_timeout_ms=3000, sync_timeout_ms=2000)
                else:
                    watcher.running = False
                    watcher.quit()
                    watcher.wait(3000)
            except Exception:
                pass

        # Stop library fetch worker if running
        try:
            ft = getattr(self, '_fetch_thread', None)
        except Exception:
            ft = None
        if ft and hasattr(ft, 'isRunning') and ft.isRunning():
            try:
                if hasattr(ft, 'requestInterruption'):
                    ft.requestInterruption()
            except Exception:
                pass
            try:
                ft.quit()
            except Exception:
                pass
            try:
                ft.wait(2000)
            except Exception:
                pass
            try:
                if ft.isRunning():
                    ft.terminate()
                    ft.wait(500)
            except Exception:
                pass

        # Stop local discovery worker
        try:
            dw = getattr(self, '_discovery_worker', None)
        except Exception:
            dw = None
        if dw and hasattr(dw, 'isRunning') and dw.isRunning():
            try:
                if hasattr(dw, 'stop'):
                    dw.stop()
            except Exception:
                pass
            try:
                if hasattr(dw, 'requestInterruption'):
                    dw.requestInterruption()
            except Exception:
                pass
            try:
                dw.quit()
            except Exception:
                pass
            try:
                dw.wait(2000)
            except Exception:
                pass
            try:
                if dw.isRunning():
                    dw.terminate()
                    dw.wait(500)
            except Exception:
                pass

        # Stop any image fetchers
        try:
            fetchers = list(getattr(self, 'active_image_fetchers', []) or [])
        except Exception:
            fetchers = []
        for f in fetchers:
            if not f:
                continue
            try:
                if hasattr(f, 'requestInterruption'):
                    f.requestInterruption()
            except Exception:
                pass
            try:
                f.quit()
            except Exception:
                pass
            try:
                f.wait(1000)
            except Exception:
                pass
            try:
                if hasattr(f, 'isRunning') and f.isRunning():
                    f.terminate()
                    f.wait(500)
            except Exception:
                pass

        # Stop any other active threads we track (downloaders, extractors, etc.)
        try:
            threads = list(getattr(self, 'active_threads', []) or [])
        except Exception:
            threads = []
        for t in threads:
            if not t:
                continue
            try:
                if hasattr(t, 'cancel'):
                    t.cancel()
            except Exception:
                pass
            try:
                if hasattr(t, 'requestInterruption'):
                    t.requestInterruption()
            except Exception:
                pass
            try:
                if hasattr(t, 'quit'):
                    t.quit()
            except Exception:
                pass
            try:
                if hasattr(t, 'wait'):
                    t.wait(1500)
            except Exception:
                pass
            try:
                if hasattr(t, 'isRunning') and t.isRunning():
                    t.terminate()
                    t.wait(500)
            except Exception:
                pass

    def _apply_windows_frame(self):
        import sys
        if sys.platform != "win32":
            return
        if QApplication.activeModalWidget() is not None:
            return

        now = time.monotonic()
        if self._frame_applied and (now - self._last_frame_apply_ts) < 0.5:
            return

        try:
            import ctypes
            import ctypes.wintypes as wintypes
            
            hwnd = int(self.winId())

            try:
                DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                DWMWA_BORDER_COLOR = 34
                DWMWA_CAPTION_COLOR = 35

                _true = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, ctypes.byref(_true), ctypes.sizeof(_true))
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(_true), ctypes.sizeof(_true))

                # COLORREF is 0x00BBGGRR
                _dark = wintypes.DWORD(0x00121212)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(_dark), ctypes.sizeof(_dark))
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR, ctypes.byref(_dark), ctypes.sizeof(_dark))
            except Exception:
                pass
            
            # MARGINS struct — extend frame into client area on all sides by 1px
            # This removes title bar but keeps the resize border and snap behavior
            class MARGINS(ctypes.Structure):
                _fields_ = [
                    ("cxLeftWidth",    ctypes.c_int),
                    ("cxRightWidth",   ctypes.c_int),
                    ("cyTopHeight",    ctypes.c_int),
                    ("cyBottomHeight", ctypes.c_int),
                ]
            
            margins = MARGINS(1, 1, 1, 1)
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                hwnd, ctypes.byref(margins))
            
            # Remove WS_CAPTION but keep WS_THICKFRAME for resize
            GWL_STYLE = -16
            WS_CAPTION     = 0x00C00000
            WS_THICKFRAME  = 0x00040000
            WS_SYSMENU     = 0x00080000
            WS_MAXIMIZEBOX = 0x00010000
            WS_MINIMIZEBOX = 0x00020000
            
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
            # Remove caption, keep thick frame
            style = style & ~WS_CAPTION
            style = style | WS_THICKFRAME
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style)
            
            # Force Windows to redraw the frame
            SWP_FLAGS = (0x0020 |  # SWP_FRAMECHANGED
                         0x0002 |  # SWP_NOMOVE
                         0x0001 |  # SWP_NOSIZE
                         0x0004)   # SWP_NOZORDER
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)
            self._frame_applied = True
            self._last_frame_apply_ts = now
                
        except Exception as e:
            logging.warning(f"[Frame] Windows frame setup failed: {e}")

    def _get_drag_rect(self):
        """Returns the screen rect of the draggable center area of the title bar."""
        try:
            tb = self.title_bar
            # Drag zone is between status_text and lib_btn
            left_widget = tb.status_text
            right_widget = tb.nav_buttons[0] if tb.nav_buttons else tb.settings_btn
            
            left_x = left_widget.mapToGlobal(left_widget.rect().bottomRight()).x()
            right_x = right_widget.mapToGlobal(right_widget.rect().bottomLeft()).x()
            
            top_y = tb.mapToGlobal(tb.rect().topLeft()).y()
            bot_y = tb.mapToGlobal(tb.rect().bottomLeft()).y()
            
            return left_x, right_x, top_y, bot_y
        except Exception:
            return None

    def nativeEvent(self, eventType, message):
        import sys
        if sys.platform != "win32":
            return super().nativeEvent(eventType, message)

        import ctypes
        import ctypes.wintypes as wintypes

        if eventType not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return super().nativeEvent(eventType, message)

        msg = ctypes.wintypes.MSG.from_address(int(message))

        WM_NCCALCSIZE    = 0x0083
        WM_NCHITTEST     = 0x0084
        WM_GETMINMAXINFO = 0x0024

        if msg.message == WM_GETMINMAXINFO:
            # Constrain the maximized window to the work area so it never covers the taskbar
            try:
                hwnd = int(self.winId())
                MONITOR_DEFAULTTONEAREST = 2
                monitor = ctypes.windll.user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)

                class _RECT(ctypes.Structure):
                    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                                 ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

                class _MONITORINFO(ctypes.Structure):
                    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                                 ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]

                mi = _MONITORINFO()
                mi.cbSize = ctypes.sizeof(mi)
                ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(mi))

                max_left = mi.rcWork.left
                max_top = mi.rcWork.top
                max_right = mi.rcWork.right
                max_bottom = mi.rcWork.bottom

                if (mi.rcWork.left == mi.rcMonitor.left and mi.rcWork.top == mi.rcMonitor.top and
                        mi.rcWork.right == mi.rcMonitor.right and mi.rcWork.bottom == mi.rcMonitor.bottom):
                    try:
                        ABM_GETSTATE = 0x00000004
                        ABM_GETTASKBARPOS = 0x00000005
                        ABS_AUTOHIDE = 0x00000001

                        class _APPBARDATA(ctypes.Structure):
                            _fields_ = [
                                ("cbSize", ctypes.c_uint),
                                ("hWnd", wintypes.HWND),
                                ("uCallbackMessage", ctypes.c_uint),
                                ("uEdge", ctypes.c_uint),
                                ("rc", _RECT),
                                ("lParam", ctypes.c_int),
                            ]

                        abd = _APPBARDATA()
                        abd.cbSize = ctypes.sizeof(_APPBARDATA)

                        state = ctypes.windll.shell32.SHAppBarMessage(ABM_GETSTATE, ctypes.byref(abd))
                        if state & ABS_AUTOHIDE:
                            ctypes.windll.shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(abd))
                            ABE_LEFT = 0
                            ABE_TOP = 1
                            ABE_RIGHT = 2
                            ABE_BOTTOM = 3
                            if abd.uEdge == ABE_LEFT:
                                max_left += 1
                            elif abd.uEdge == ABE_TOP:
                                max_top += 1
                            elif abd.uEdge == ABE_RIGHT:
                                max_right -= 1
                            elif abd.uEdge == ABE_BOTTOM:
                                max_bottom -= 1
                    except Exception:
                        max_bottom -= 1

                class _POINT(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

                class _MINMAXINFO(ctypes.Structure):
                    _fields_ = [("ptReserved", _POINT), ("ptMaxSize", _POINT),
                                 ("ptMaxPosition", _POINT), ("ptMinTrackSize", _POINT),
                                 ("ptMaxTrackSize", _POINT)]

                mmi = _MINMAXINFO.from_address(msg.lParam)
                w = max_right - max_left
                h = max_bottom - max_top
                mmi.ptMaxSize.x = w
                mmi.ptMaxSize.y = h
                mmi.ptMaxPosition.x = max_left
                mmi.ptMaxPosition.y = max_top
                mmi.ptMaxTrackSize.x = w
                mmi.ptMaxTrackSize.y = h
                return True, 0
            except Exception:
                pass

        if msg.message == WM_NCCALCSIZE:
            if msg.wParam == 1:
                return True, 0
            return False, 0

        if msg.message == WM_NCHITTEST:
            # Screen coordinates from lParam
            x = ctypes.c_int16(msg.lParam & 0xFFFF).value
            y = ctypes.c_int16((msg.lParam >> 16) & 0xFFFF).value

            rect = wintypes.RECT()
            hwnd = int(self.winId())
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))

            # Use device pixels for border — fall back to Qt's ratio if Win API unavailable
            try:
                dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
                scale = dpi / 96.0
            except Exception:
                scale = self.devicePixelRatioF()

            b = max(4, int(8 * scale))

            # Clamp to window bounds first
            if (x < rect.left or x > rect.right or y < rect.top or y > rect.bottom):
                return False, 0

            dist_left   = x - rect.left
            dist_right  = rect.right  - x
            dist_top    = y - rect.top
            dist_bottom = rect.bottom - y

            try:
                is_zoomed = bool(ctypes.windll.user32.IsZoomed(hwnd))
            except Exception:
                is_zoomed = self.isMaximized()

            if not is_zoomed:
                on_left   = dist_left   <= b
                on_right  = dist_right  <= b
                on_top    = dist_top    <= b
                on_bottom = dist_bottom <= b

                HTTOPLEFT     = 13
                HTTOPRIGHT    = 14
                HTBOTTOMLEFT  = 16
                HTBOTTOMRIGHT = 17
                HTTOP         = 12
                HTBOTTOM      = 15
                HTLEFT        = 10
                HTRIGHT       = 11

                if on_top and on_left: return True, HTTOPLEFT
                if on_top and on_right: return True, HTTOPRIGHT
                if on_bottom and on_left: return True, HTBOTTOMLEFT
                if on_bottom and on_right: return True, HTBOTTOMRIGHT
                if on_top: return True, HTTOP
                if on_bottom: return True, HTBOTTOM
                if on_left: return True, HTLEFT
                if on_right: return True, HTRIGHT

            # Title bar area hit testing
            title_height = int(40 * scale)
            if dist_top <= title_height:
                drag_rect = self._get_drag_rect()
                if drag_rect:
                    left_x, right_x, top_y, bot_y = drag_rect
                    if left_x <= x <= right_x and top_y <= y <= bot_y:
                        return True, 2 # HTCAPTION
                else:
                    # Fallback
                    if (x - rect.left) < (rect.right - rect.left) * 0.4:
                        return True, 2 # HTCAPTION

                return True, 1 # HTCLIENT

            return True, 1 # HTCLIENT
        
        return super().nativeEvent(eventType, message)

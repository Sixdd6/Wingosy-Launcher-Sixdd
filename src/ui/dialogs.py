import sys
import os
import re
import webbrowser
import zipfile
import shutil
import subprocess
import logging
from pathlib import Path
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, 
                             QLabel, QLineEdit, QPushButton, QDialogButtonBox, 
                             QMessageBox, QProgressBar, QComboBox, QFileDialog, 
                             QSizePolicy, QApplication, QWidget, QSpinBox, QScrollArea)
from PySide6.QtCore import Qt, Signal, QThread, QTimer, QEventLoop
from PySide6.QtGui import QPixmap, QDesktopServices

from src.ui.threads import (UpdaterThread, SelfUpdateThread,
                             ConnectionTestThread, RomDownloader, CoreDownloadThread, ImageFetcher, ConflictResolveThread, GameDescriptionFetcher, ExtractionThread)
from src.ui.widgets import format_speed, format_size, get_resource_path
from src.platforms import RETROARCH_PLATFORMS, RETROARCH_CORES, platform_matches
from src import emulators
from src.utils import read_retroarch_cfg, write_retroarch_cfg_values

_retroarch_autosave_checked = False
_ppsspp_assets_checked = False

WINDOWS_PLATFORM_SLUGS = ["windows", "win", "pc", "pc-windows", "windows-games", "win95", "win98"]

def check_retroarch_autosave(ra_exe_path, platform_slug, parent, config=None):
    """
    Check retroarch.cfg and prompt user to enable auto save/load if needed.
    Only prompts when save mode includes states (state or both).
    PSP is always skipped.
    Fires at most once per app session.
    """
    global _retroarch_autosave_checked
    if _retroarch_autosave_checked:
        return
    _retroarch_autosave_checked = True

    # PSP always uses SAVEDATA folder sync — states not applicable
    if platform_slug in ("psp", "playstation-portable"):
        return

    # Only relevant when the user wants state-based saving
    save_mode = config.get("retroarch_save_mode", "srm") if config else "srm"
    if save_mode == "srm":
        return  # SRM-only mode doesn't need savestates enabled

    cfg_path = Path(ra_exe_path).parent / "retroarch.cfg"
    if not cfg_path.exists():
        return

    cfg = read_retroarch_cfg(str(cfg_path))
    auto_save = cfg.get("savestate_auto_save", "false")
    auto_load = cfg.get("savestate_auto_load", "false")

    if auto_save == "true" and auto_load == "true":
        return  # already good

    missing = []
    if auto_save != "true": missing.append("savestate_auto_save")
    if auto_load != "true": missing.append("savestate_auto_load")

    result = QMessageBox.question(
        parent,
        "RetroArch Auto-Save States",
        f"Your RetroArch save mode is set to '{save_mode}' but auto save/load "
        f"states are disabled in retroarch.cfg.\n\n"
        f"Disabled: {', '.join(missing)}\n\n"
        f"Would you like Wingosy to enable them automatically?\n"
        f"(Writes to: {cfg_path})",
        QMessageBox.Yes | QMessageBox.No
    )
    if result == QMessageBox.Yes:
        write_retroarch_cfg_values(str(cfg_path), {
            "savestate_auto_save": "true",
            "savestate_auto_load": "true"
        })
        QMessageBox.information(
            parent,
            "RetroArch Auto-Save States",
            "✅ Auto save/load states enabled in retroarch.cfg."
        )

def check_ppsspp_assets(ra_exe_path, parent):
    global _ppsspp_assets_checked
    if _ppsspp_assets_checked:
        return
    _ppsspp_assets_checked = True
    
    system_ppsspp = Path(ra_exe_path).parent / "system" / "PPSSPP"
    zim_path = system_ppsspp / "ppge_atlas.zim"
    if zim_path.exists():
        return
    
    result = QMessageBox.question(
        parent,
        "PPSSPP Assets Missing",
        "PPSSPP requires asset files to run correctly.\n\n"
        "ppge_atlas.zim is missing from:\n"
        f"{system_ppsspp}\n\n"
        "Would you like Wingosy to download them now?\n"
        "(~2MB from buildbot.libretro.com)",
        QMessageBox.Yes | QMessageBox.No
    )
    if result != QMessageBox.Yes:
        return
    
    progress = QMessageBox(parent)
    progress.setWindowTitle("Downloading PPSSPP Assets")
    progress.setText("Downloading PPSSPP assets...\nPlease wait.")
    progress.setStandardButtons(QMessageBox.NoButton)
    progress.show()
    QApplication.processEvents()
    
    try:
        import urllib.request, zipfile, tempfile
        url = "https://buildbot.libretro.com/assets/system/PPSSPP.zip"
        system_ppsspp.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".zip",
                                         delete=False) as tmp:
            tmp_path = tmp.name
        urllib.request.urlretrieve(url, tmp_path)
        with zipfile.ZipFile(tmp_path, 'r') as z:
            for member in z.namelist():
                relative = member
                if relative.startswith("PPSSPP/"):
                    relative = relative[len("PPSSPP/"):]
                if not relative:
                    continue
                target = system_ppsspp / relative
                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(member) as src, \
                         open(target, 'wb') as dst:
                        dst.write(src.read())
        Path(tmp_path).unlink(missing_ok=True)
        progress.close()
        QMessageBox.information(parent, "PPSSPP Assets Ready",
            "PPSSPP assets downloaded successfully. ✅")
    except Exception as e:
        progress.close()
        QMessageBox.warning(parent, "Download Failed",
            f"Could not download PPSSPP assets:\n{e}\n\n"
            f"You can manually place them in:\n{system_ppsspp}")

class WelcomeDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to Wingosy Launcher")
        self.resize(500, 350)
        layout = QVBoxLayout(self)
        
        title = QLabel("<h1>Welcome to Wingosy!</h1>")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        info = QLabel(
            "<p style='font-size: 12pt;'>Your setup is almost complete. Follow these steps to get started:</p>"
            "<ol style='font-size: 11pt;'>"
            "<li><b>Step 1:</b> Enter your RomM server URL and credentials (done!).</li>"
            "<li><b>Step 2:</b> Go to the <b>Emulators</b> tab to set your ROM and Emulator paths.</li>"
            "<li><b>Step 3:</b> Click any game in your library and hit <b>PLAY</b>. Wingosy handles the rest!</li>"
            "</ol>"
            "<p>Happy gaming!</p>"
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        
        layout.addStretch()
        btn = QPushButton("Get Started")
        btn.setStyleSheet("background: #1e88e5; color: white; font-weight: bold; padding: 10px;")
        btn.clicked.connect(self.accept)
        layout.addWidget(btn)

class ConflictDialog(QDialog):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Save Conflict: {title}")
        self.resize(450, 200)
        layout = QVBoxLayout(self)
        
        msg = QLabel(
            f"Both local and cloud saves exist for <b>{title}</b>, and they differ.<br><br>"
            "Which one would you like to use?"
        )
        msg.setWordWrap(True)
        layout.addWidget(msg)
        
        layout.addStretch()
        btn_layout = QHBoxLayout()
        
        self.result_mode = None # "cloud", "local", "both"
        
        cloud_btn = QPushButton("☁️ Use Cloud")
        cloud_btn.clicked.connect(lambda: self.finish("cloud"))
        btn_layout.addWidget(cloud_btn)
        
        local_btn = QPushButton("💾 Keep Local")
        local_btn.clicked.connect(lambda: self.finish("local"))
        btn_layout.addWidget(local_btn)
        
        both_btn = QPushButton("📁 Keep Both")
        both_btn.clicked.connect(lambda: self.finish("both"))
        btn_layout.addWidget(both_btn)
        
        layout.addLayout(btn_layout)

    def finish(self, mode):
        self.result_mode = mode
        self.accept()

class SetupDialog(QDialog):
    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Wingosy Setup")
        self.config = config_manager
        self.resize(400, 200)
        layout = QFormLayout(self)
        self.host_input = QLineEdit(self.config.get("host"))
        self.user_input = QLineEdit(self.config.get("username"))
        self.pass_input = QLineEdit("") # Do not load from config
        self.pass_input.setEchoMode(QLineEdit.Password)
        layout.addRow("RomM Host:", self.host_input)
        layout.addRow("Username:", self.user_input)
        layout.addRow("Password:", self.pass_input)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def validate_and_accept(self):
        host = self.host_input.text().strip()
        url_pattern = re.compile(r'^https?://.+')
        if not url_pattern.match(host):
            QMessageBox.warning(self, "Invalid Host", "Please enter a valid URL (starting with http:// or https://)")
            return
        self.accept()

    def get_data(self):
        return {
            "host": self.host_input.text().strip().rstrip('/'),
            "username": self.user_input.text().strip(),
            "password": self.pass_input.text()
        }

class ExePickerDialog(QDialog):
    def __init__(self, exes, game_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Choose Executable — {game_name}")
        self.setMinimumSize(500, 400)
        self.selected_exe = None
        
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Multiple executables found. Please select the one to launch:"))
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        list_layout = QVBoxLayout(container)
        list_layout.setAlignment(Qt.AlignTop)
        
        for exe_path in exes:
            btn = QPushButton()
            btn.setStyleSheet("text-align: left; padding: 10px; background: #252525; border-radius: 4px;")
            
            row_layout = QVBoxLayout(btn)
            name_label = QLabel(f"<b>{os.path.basename(exe_path)}</b>")
            path_label = QLabel(f"<small style='color: #888;'>{exe_path}</small>")
            
            size = os.path.getsize(exe_path)
            size_label = QLabel(f"<small style='color: #aaa;'>Size: {format_size(size)}</small>")
            
            row_layout.addWidget(name_label)
            row_layout.addWidget(path_label)
            row_layout.addWidget(size_label)
            
            btn.clicked.connect(lambda checked, p=exe_path: self.select_and_accept(p))
            list_layout.addWidget(btn)
            
        scroll.setWidget(container)
        layout.addWidget(scroll)
        
        buttons = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        buttons.addStretch()
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

    def select_and_accept(self, path):
        self.selected_exe = path
        self.accept()

class SettingsDialog(QDialog):
    def __init__(self, config_manager, main_window, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.config = config_manager
        self.main_window = main_window
        self.resize(400, 550)
        self.settings_layout = QVBoxLayout(self)

        host_layout = QHBoxLayout()
        host_layout.addWidget(QLabel("Server Host:"))
        self.host_input = QLineEdit()
        self.host_input.setText(self.config.get("host", ""))
        self.host_input.setPlaceholderText("http://192.168.x.x:8285")
        host_layout.addWidget(self.host_input)

        self.test_conn_btn = QPushButton("Test Connection")
        self.test_conn_btn.clicked.connect(self._test_host_connection)
        host_layout.addWidget(self.test_conn_btn)

        self.reconnect_btn = QPushButton("✅ Apply & Re-connect")
        self.reconnect_btn.setVisible(False)
        self.reconnect_btn.setStyleSheet(
            "background: #2e7d32; color: white; padding: 4px 10px;")
        self.reconnect_btn.clicked.connect(self._apply_and_restart)
        host_layout.addWidget(self.reconnect_btn)

        self.settings_layout.addLayout(host_layout)
        
        self.settings_layout.addWidget(QLabel(f"<b>User:</b> {self.config.get('username')}"))
        self.settings_layout.addWidget(QLabel(f"<b>Version:</b> {self.main_window.version}"))
        
        self.auto_pull_btn = QPushButton("Auto Pull Saves: ON" if self.config.get("auto_pull_saves", True) else "Auto Pull Saves: OFF")
        self.auto_pull_btn.setCheckable(True)
        self.auto_pull_btn.setChecked(self.config.get("auto_pull_saves", True))
        self.auto_pull_btn.toggled.connect(self.toggle_auto_pull)
        self.settings_layout.addWidget(self.auto_pull_btn)
        
        # Cards per row setting
        cards_row_layout = QHBoxLayout()
        cards_row_layout.addWidget(QLabel("Cards per row:"))
        self.cards_per_row_spin = QSpinBox()
        self.cards_per_row_spin.setMinimum(1)
        self.cards_per_row_spin.setMaximum(12)
        self.cards_per_row_spin.setValue(self.config.get("cards_per_row", 6))
        self.cards_per_row_spin.valueChanged.connect(self.set_cards_per_row)
        cards_row_layout.addWidget(self.cards_per_row_spin)
        cards_row_layout.addStretch()
        self.settings_layout.addLayout(cards_row_layout)
        
        # RetroArch save mode
        self.settings_layout.addWidget(QLabel("<b>RetroArch Save Mode:</b>"))
        self.ra_save_mode_combo = QComboBox()
        self.ra_save_mode_combo.addItems(["SRM only", "States only", "Both"])
        mode_map = {"srm": "SRM only", "state": "States only", "both": "Both"}
        current_mode = self.config.get("retroarch_save_mode", "srm")
        self.ra_save_mode_combo.setCurrentText(mode_map.get(current_mode, "SRM only"))
        self.ra_save_mode_combo.currentTextChanged.connect(self.set_ra_save_mode)
        self.settings_layout.addWidget(self.ra_save_mode_combo)

        # Windows Games Folder
        self.settings_layout.addWidget(QLabel("<b>Windows Games Folder:</b>"))
        win_folder_layout = QHBoxLayout()
        self.win_folder_input = QLineEdit(self.config.get("windows_games_dir", ""))
        win_folder_layout.addWidget(self.win_folder_input)
        browse_win_btn = QPushButton("Browse")
        browse_win_btn.clicked.connect(self.browse_windows_folder)
        win_folder_layout.addWidget(browse_win_btn)
        self.settings_layout.addLayout(win_folder_layout)

        # Log level setting
        log_level_layout = QHBoxLayout()
        log_level_layout.addWidget(QLabel("<b>Log Level:</b>"))
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        current_level = self.config.get("log_level", "INFO").upper()
        self.log_level_combo.setCurrentText(current_level)
        self.log_level_combo.currentTextChanged.connect(self.set_log_level)
        log_level_layout.addWidget(self.log_level_combo)
        log_level_layout.addStretch()
        self.settings_layout.addLayout(log_level_layout)
        
        self.settings_layout.addSpacing(10)
        self.update_btn = QPushButton("Check for Updates")
        self.update_btn.clicked.connect(self.check_updates)
        self.settings_layout.addWidget(self.update_btn)
        
        self.upgrade_btn = QPushButton("Upgrade Available!")
        self.upgrade_btn.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold;")
        self.upgrade_btn.setVisible(False)
        self.settings_layout.addWidget(self.upgrade_btn)

        self.update_pbar = QProgressBar()
        self.update_pbar.setVisible(False)
        self.settings_layout.addWidget(self.update_pbar)
        
        self.settings_layout.addStretch()
        
        self.about_btn = QPushButton("ℹ️ About Wingosy")
        self.about_btn.clicked.connect(self.show_about)
        self.settings_layout.addWidget(self.about_btn)
        
        self.logout_btn = QPushButton("Log Out")
        self.logout_btn.setStyleSheet("background-color: #c62828; color: white; padding: 8px;")
        self.logout_btn.clicked.connect(self.do_logout)
        self.settings_layout.addWidget(self.logout_btn)
        
        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.reject)
        self.settings_layout.addWidget(buttons)

        self.latest_version_url = ""

    def browse_windows_folder(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Windows Games Folder")
        if directory:
            self.win_folder_input.setText(directory)
            self.config.set("windows_games_dir", directory)

    def _test_host_connection(self):
        host = self.host_input.text().strip()
        if not host:
            QMessageBox.warning(self, "No Host", "Please enter a host URL.")
            return
        self.test_conn_btn.setText("Testing...")
        self.test_conn_btn.setEnabled(False)
        
        # Use the unified test_connection method from the client with retry feedback
        success, message = self.main_window.client.test_connection(
            host_override=host,
            retry_callback=lambda: self.test_conn_btn.setText("Retrying (slow server)...")
        )
        
        self.test_conn_btn.setText("Test Connection")
        self.test_conn_btn.setEnabled(True)
        if success:
            QMessageBox.information(self, "Success",
                f"{message} Click 'Apply & Reconnect' to use this host.")
            self.reconnect_btn.setVisible(True)
        else:
            QMessageBox.warning(self, "Failed", message)
            self.reconnect_btn.setVisible(False)

    def _apply_and_restart(self):
        import logging
        import time
        new_host = self.host_input.text().strip()
        logging.info("[Restart] _apply_and_restart called")
        logging.info(f"[Restart] new host={new_host}")
        
        # Save config first
        self.config.set("host", new_host)
        
        # Small delay to ensure config is flushed to disk
        time.sleep(0.3)
        
        QMessageBox.information(self, "Restarting",
            "Host saved. The app will now restart.")
        
        logging.info("[Restart] config saved, calling _do_restart")
        self._do_restart()

    def _do_restart(self):
        import logging
        import subprocess
        import sys
        import os
        
        logging.info("[Restart] _do_restart called")
        logging.info(f"[Restart] frozen="
                     f"{getattr(sys, 'frozen', False)}")
        logging.info(f"[Restart] sys.executable={sys.executable}")
        logging.info(f"[Restart] sys.argv={sys.argv}")
        
        exe = sys.executable  # Always the correct exe, 
                               # frozen or not
        
        try:
            logging.info(f"[Restart] about to Popen: {exe}")
            
            if sys.platform == "win32":
                # Windows: detached process so it survives
                # parent exit
                DETACHED_PROCESS = 0x00000008
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                subprocess.Popen(
                    [exe],
                    close_fds=True,
                    creationflags=(
                        DETACHED_PROCESS | 
                        CREATE_NEW_PROCESS_GROUP),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    [exe],
                    close_fds=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            
            logging.info("[Restart] Popen complete")
        except Exception as e:
            logging.exception(f"[Restart] Popen failed: {e}")
            return
        
        logging.info("[Restart] calling sys.exit(0)")
        sys.exit(0)

    def show_about(self):
        QMessageBox.about(self, "About Wingosy",
            f"<b>Wingosy Launcher</b> v{self.main_window.version}<br><br>"
            "A lightweight Windows game launcher for RomM.<br>"
            "Licensed under GNU GPL v3.0.<br><br>"
            "<a href='https://github.com/abduznik/Wingosy-Launcher'>GitHub Repository</a>"
        )

    def toggle_auto_pull(self, checked):
        self.config.set("auto_pull_saves", checked)
        self.auto_pull_btn.setText("Auto Pull Saves: ON" if checked else "Auto Pull Saves: OFF")

    def set_cards_per_row(self, value):
        self.config.set("cards_per_row", value)
        lib = self.main_window.library_tab
        lib._resize_all_cards()

    def set_log_level(self, text):
        self.config.set("log_level", text)
        level = getattr(logging, text.upper(), logging.INFO)
        logging.getLogger().setLevel(level)
        logging.info(f"Log level changed to {text}")

    def set_ra_save_mode(self, text):
        mode_map = {"SRM only": "srm", "States only": "state", "Both": "both"}
        self.config.set("retroarch_save_mode", mode_map.get(text, "srm"))

    def check_updates(self):
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Checking...")
        self.updater = UpdaterThread(self.main_window.version)
        self.updater.finished.connect(self.on_update_result)
        self.updater.start()

    def on_update_result(self, available, version, url):
        self.update_btn.setEnabled(True)
        self.update_btn.setText("Check for Updates")
        if available:
            self.latest_version_url = url
            self.upgrade_btn.setText(f"Upgrade to v{version}")
            self.upgrade_btn.setVisible(True)
            try: self.upgrade_btn.clicked.disconnect()
            except Exception: pass
            
            if getattr(sys, 'frozen', False):
                self.upgrade_btn.clicked.connect(self.start_self_update)
            else:
                self.upgrade_btn.clicked.connect(lambda: webbrowser.open(url))
        else:
            QMessageBox.information(self, "No Updates", "You are running the latest version.")

    def start_self_update(self):
        self.upgrade_btn.setEnabled(False)
        self.upgrade_btn.setText("Downloading update...")
        self.update_pbar.setVisible(True)
        self.update_pbar.setValue(0)
        
        current_exe = Path(sys.executable).resolve()
        self.updater_thread = SelfUpdateThread(self.latest_version_url, current_exe)
        self.updater_thread.progress.connect(self.update_pbar.setValue)
        self.updater_thread.finished.connect(self.on_self_update_finished)
        self.updater_thread.start()

    def on_self_update_finished(self, success, message):
        if success:
            QMessageBox.information(self, "Update Complete", "Update downloaded! Click OK to restart Wingosy.")
            current_exe = Path(sys.executable).resolve()
            pid = os.getpid()
            bat_path = current_exe.parent / "_wingosy_restart.bat"
            bat_content = (
                f'@echo off\n'
                f':wait\n'
                f'tasklist /FI "PID eq {pid}" 2>NUL | find /I "{pid}" >NUL\n'
                f'if not errorlevel 1 (\n'
                f'    timeout /t 1 /nobreak >NUL\n'
                f'    goto wait\n'
                f')\n'
                f'start "" "{current_exe}"\n'
                f'del "%~f0"\n'
            )
            bat_path.write_text(bat_content)
            subprocess.Popen(
                ['cmd.exe', '/c', str(bat_path)],
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            QApplication.instance().quit()
        else:
            QMessageBox.critical(self, "Update Failed", f"Could not replace the current file. Please download manually.\nError: {message}")
            self.upgrade_btn.setEnabled(True)
            self.upgrade_btn.setText("Retry Update")
            webbrowser.open(self.latest_version_url)

    def do_logout(self):
        reply = QMessageBox.question(self, "Log Out", "Are you sure you want to log out? You will need to enter your credentials again.", QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
            
        self.main_window.client.logout()
        self.config.set("password", None)
        QMessageBox.information(self, "Logged Out", "You have been logged out. Restart to log in.")
        QApplication.instance().quit()

class GameDetailDialog(QDialog):
    def __init__(self, game, client, config, main_window, parent=None):
        super().__init__(parent)
        self.game, self.client, self.config, self.main_window = game, client, config, main_window
        self.setWindowTitle(game.get("name"))
        self.setFixedSize(800, 550)
        self.dl_thread = None
        self.extract_thread = None
        self._conflict_shown = False
        self._is_windows = game.get("platform_slug") in WINDOWS_PLATFORM_SLUGS
        self._local_rom_path = self._get_local_rom_path()
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        
        # Title (Top)
        title_label = QLabel(game.get('name'))
        title_label.setStyleSheet("font-size: 20pt; font-weight: bold; color: #1e88e5;")
        title_label.setWordWrap(True)
        main_layout.addWidget(title_label)
        
        # Content layout (Cover | Metadata)
        content_layout = QHBoxLayout()
        content_layout.setSpacing(25)
        
        # Left: Cover Image (300px wide)
        self.img_label = QLabel()
        self.img_label.setFixedWidth(300)
        self.img_label.setAlignment(Qt.AlignCenter)
        self.img_label.setStyleSheet("background: #1a1a1a; border-radius: 6px;")
        content_layout.addWidget(self.img_label)
        
        # Right Column: Metadata + Description + Action Buttons
        right_column = QVBoxLayout()
        right_column.setSpacing(0) # Remove default spacing
        
        platform_label = QLabel(f"<b>Platform:</b> {game.get('platform_display_name')}")
        platform_label.setStyleSheet("font-size: 12pt; margin-bottom: 2px;")
        right_column.addWidget(platform_label)
        
        # Size
        total_bytes = 0
        for f in game.get('files', []):
            total_bytes += f.get('file_size_bytes', 0)
        size_str = format_size(total_bytes)
        size_label = QLabel(f"<b>Size:</b> {size_str}")
        size_label.setStyleSheet("font-size: 12pt; margin-bottom: 8px;")
        right_column.addWidget(size_label)
        
        # Description scroll area
        self.desc_scroll = QScrollArea()
        self.desc_scroll.setWidgetResizable(True)
        self.desc_scroll.setStyleSheet("background: transparent; border: none;")
        self.desc_label = QLabel("Loading description...")
        self.desc_label.setWordWrap(True)
        self.desc_label.setAlignment(Qt.AlignTop)
        self.desc_label.setStyleSheet("color: #ccc; font-size: 11pt; line-height: 1.4;")
        self.desc_scroll.setWidget(self.desc_label)
        right_column.addWidget(self.desc_scroll, 1) # Give it stretch factor 1
        
        # Progress area (for downloads)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        right_column.addWidget(self.progress_bar)
        self.speed_label = QLabel()
        self.speed_label.setAlignment(Qt.AlignCenter)
        right_column.addWidget(self.speed_label)
        
        # Action Buttons Container (Minimized spacing)
        actions_layout = QVBoxLayout()
        actions_layout.setContentsMargins(0, 5, 0, 0) # Tight to description
        actions_layout.setSpacing(4) # Very small margin between buttons
        
        self.play_btn = QPushButton("▶ PLAY")
        self.play_btn.setStyleSheet("background: #2e7d32; color: white; font-weight: bold; padding: 10px; font-size: 13pt;")
        self.play_btn.clicked.connect(self.play_game)
        
        self.uninstall_btn = QPushButton("🗑 Uninstall")
        self.uninstall_btn.setStyleSheet("background: #8e0000; color: white; padding: 6px; font-size: 11pt;")
        self.uninstall_btn.clicked.connect(self.uninstall_game)
        
        self.dl_btn = QPushButton("⬇ DOWNLOAD")
        self.dl_btn.setStyleSheet("background: #1565c0; color: white; font-weight: bold; padding: 10px; font-size: 13pt;")
        self.dl_btn.clicked.connect(self._on_download_clicked)
        
        self.cancel_btn = QPushButton("Cancel Download")
        self.cancel_btn.setStyleSheet("background: #c62828; color: white;")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.cancel_dl)
        
        actions_layout.addWidget(self.play_btn)
        actions_layout.addWidget(self.uninstall_btn)
        actions_layout.addWidget(self.dl_btn)
        actions_layout.addWidget(self.cancel_btn)
        
        right_column.addLayout(actions_layout)
        
        content_layout.addLayout(right_column, 1)
        main_layout.addLayout(content_layout)
        
        # Close button (Bottom, spans full width)
        close_btn = QPushButton("Close")
        close_btn.setStyleSheet("background: #333; color: #ccc; padding: 8px; font-size: 12pt;")
        close_btn.clicked.connect(self.reject)
        main_layout.addWidget(close_btn)
        
        # Fetch data
        self._update_button_states()
        self._start_image_fetch()
        self._start_desc_fetch()

    def _get_local_rom_path(self):
        platform = self.game.get('platform_slug')
        
        if self._is_windows:
            win_dir = self.config.get("windows_games_dir")
            if not win_dir: return None
            rom_name = self.game.get('fs_name')
            if not rom_name: return None
            # Path to the folder named after the archive (no extension)
            folder_name = Path(rom_name).stem
            return Path(win_dir) / folder_name

        base_rom = self.config.get("base_rom_path")
        rom_name = self.game.get('fs_name')
        if not rom_name: return None
        return Path(base_rom) / platform / rom_name

    def _update_button_states(self):
        exists = False
        if self._is_windows:
            # Check for extracted folder containing at least one .exe
            folder = self._local_rom_path
            if folder and folder.exists() and folder.is_dir():
                # Any .exe recursively
                exists = any(folder.rglob("*.exe"))
        else:
            exists = self._local_rom_path and self._local_rom_path.exists()
            # If not in the platform subfolder, check root
            if not exists:
                base_rom = self.config.get("base_rom_path")
                rom_name = self.game.get('fs_name')
                if rom_name:
                    root_path = Path(base_rom) / rom_name
                    if root_path.exists():
                        self._local_rom_path = root_path
                        exists = True
        
        self.play_btn.setVisible(exists)
        self.uninstall_btn.setVisible(exists)
        self.dl_btn.setVisible(not exists)

    def _start_image_fetch(self):
        url = self.client.get_cover_url(self.game)
        if url:
            self.img_fetch_thread = ImageFetcher(self.game['id'], url)
            self.img_fetch_thread.finished.connect(self._on_image_loaded)
            self.img_fetch_thread.finished.connect(lambda t=self.img_fetch_thread: self.main_window.active_threads.remove(t) if t in self.main_window.active_threads else None)
            self.main_window.active_threads.append(self.img_fetch_thread)
            self.img_fetch_thread.start()

    def _on_image_loaded(self, gid, pixmap):
        self.img_label.setPixmap(pixmap.scaled(300, 420, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _start_desc_fetch(self):
        self.desc_thread = GameDescriptionFetcher(self.client, self.game['id'])
        self.desc_thread.finished.connect(self.desc_label.setText)
        self.desc_thread.finished.connect(lambda t=self.desc_thread: self.main_window.active_threads.remove(t) if t in self.main_window.active_threads else None)
        self.main_window.active_threads.append(self.desc_thread)
        self.desc_thread.start()

    def uninstall_game(self):
        reply = QMessageBox.question(self, "Uninstall", 
            f"Are you sure you want to delete {self.game.get('name')} from your device?\n\nCloud saves are not affected.",
            QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            try:
                path = self._local_rom_path
                if path.exists():
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                    QMessageBox.information(self, "Success", "Game uninstalled.")
                    self._update_button_states()
                    # Refresh library tab indicators
                    self.main_window.library_tab.apply_filters()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to delete: {e}")

    def _on_download_clicked(self):
        if self._is_windows:
            win_dir = self.config.get("windows_games_dir")
            if not win_dir:
                QMessageBox.warning(self, "Windows Games Folder Not Set", 
                    "Please set your Windows Games folder in Settings before downloading Windows games.")
                directory = QFileDialog.getExistingDirectory(self, "Select Windows Games Folder")
                if directory:
                    self.config.set("windows_games_dir", directory)
                    win_dir = directory
                else:
                    return

        files = self.game.get('files', [])
        if files:
            self.download_rom(files[0])

    def _do_blocking_pull(self, save_info, is_retroarch):
        """
        Pull cloud saves synchronously before launch.
        Shows conflict dialog if needed and waits for resolution.
        Returns True if launch should proceed, False to cancel.
        """
        watcher = self.main_window.watcher
        rom_id = self.game['id']
        title = self.game['name']
        self._conflict_shown = False
        
        if is_retroarch and isinstance(save_info, dict):
            srm_path = save_info.get('srm')
            state_path = save_info.get('state')
            psp_folder = save_info.get('psp_folder')
            
            if psp_folder:
                # PSP: pull zip save to psp_folder
                latest_save = watcher.client.get_latest_save(rom_id)
                if latest_save:
                    result = self._apply_save_blocking(
                        rom_id, title, latest_save,
                        str(psp_folder), file_type="save",
                        is_folder=True)
                    if result is False:
                        return False
                
                # PSP state (same as other RA cores)
                state_path = save_info.get('state')
                if state_path:
                    latest_state = watcher.client.get_latest_state(rom_id)
                    if latest_state:
                        result = self._apply_save_blocking(
                            rom_id, title, latest_state,
                            state_path, file_type="state")
                        if result is False:
                            return False
            else:
                # Normal RetroArch: SRM + state
                if srm_path:
                    latest_save = watcher.client.get_latest_save(rom_id)
                    if latest_save:
                        result = self._apply_save_blocking(
                            rom_id, title, latest_save, 
                            srm_path, file_type="save")
                        if result is False:
                            return False

                if state_path:
                    latest_state = watcher.client.get_latest_state(rom_id)
                    if latest_state:
                        result = self._apply_save_blocking(
                            rom_id, title, latest_state,
                            state_path, file_type="state")
                        if result is False:
                            return False
        else:
            # Non-RetroArch
            local_path = save_info if isinstance(save_info, str) else None
            if local_path:
                latest_save = watcher.client.get_latest_save(rom_id)
                if latest_save:
                    result = self._apply_save_blocking(
                        rom_id, title, latest_save,
                        local_path, file_type="save")
                    if result is False:
                        return False
        return True

    def _apply_save_blocking(self, rom_id, title, cloud_obj, 
                              local_path, file_type="save", is_folder=False):
        """
        Download and apply a single save/state file synchronously.
        Shows conflict dialog if local file exists and differs.
        Returns False only if user explicitly cancels.
        """
        import os, tempfile, zipfile, re
        from pathlib import Path
        
        watcher = self.main_window.watcher
        
        server_updated_at = cloud_obj.get('updated_at', '')
        local_exists = os.path.isdir(local_path) if is_folder else os.path.exists(local_path)
        
        # Check cache — skip if already up to date
        cache_entry = watcher.sync_cache.get(str(rom_id), {})
        if isinstance(cache_entry, dict):
            cached_ts = cache_entry.get(f'{file_type}_updated_at','')
        else:
            cached_ts = cache_entry if file_type=='save' else ''
        
        if cached_ts == server_updated_at and local_exists:
            print(f"[Launch] {file_type} already up to date, skipping")
            return True
        
        # Download
        tmp = tempfile.mktemp(suffix=f".{file_type}")
        if file_type == "state":
            ok = watcher.client.download_state(cloud_obj, tmp)
        else:
            ok = watcher.client.download_save(cloud_obj, tmp)
        
        if not ok:
            return True  # download failed, proceed anyway
        
        # Clean filename and determine dest path
        orig_name = cloud_obj.get('file_name', '')
        clean_name = re.sub(
            r'\s*\[[^\]]*\d{4}-\d{2}-\d{2}[^\]]*\]', '', orig_name)
        
        # Conflict check — if local exists and cache has entry
        rid_str = str(rom_id)
        if (local_exists 
                and (rid_str in watcher.sync_cache)
                and not self._conflict_shown):
            
            self._conflict_shown = True
            from PySide6.QtWidgets import QMessageBox
            msg = QMessageBox(self)
            msg.setWindowTitle(f"Save Conflict — {title}")
            msg.setText(
                f"Your local {file_type} differs from the cloud.\n\n"
                f"Cloud: {server_updated_at[:19]}\n"
                f"Which do you want to use?")
            keep_local = msg.addButton(
                "Keep Local", QMessageBox.RejectRole)
            use_cloud = msg.addButton(
                "Use Cloud", QMessageBox.AcceptRole)
            msg.exec()
            if msg.clickedButton() == keep_local:
                if os.path.exists(tmp): os.remove(tmp)
                return True  # keep local, proceed with launch
        
        # Apply cloud file
        dest = Path(local_path)
        if is_folder:
            dest.parent.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
        
        # Backup existing
        if dest.exists():
            bak = Path(str(dest) + ".bak")
            try:
                import shutil
                if is_folder: shutil.copytree(str(dest), str(bak), dirs_exist_ok=True)
                else: shutil.copy2(str(dest), str(bak))
            except Exception:
                pass
        
        # Write file
        import shutil
        try:
            if is_folder or (zipfile.is_zipfile(tmp) and not local_path.endswith(('.srm', '.state'))):
                # Zip handling
                if os.path.exists(local_path) and is_folder:
                    # For PSP folders, we might want to merge or clear. 
                    pass
                os.makedirs(local_path, exist_ok=True)
                with zipfile.ZipFile(tmp, 'r') as z:
                    z.extractall(local_path)
                print(f"[Launch] Extracted {file_type} to {local_path}")
            else:
                shutil.copy2(tmp, str(dest))
            
                # For states: ensure .auto suffix
                if (file_type == "state" 
                        and dest.suffix == '.state'
                        and not dest.name.endswith('.state.auto')):
                    auto_path = dest.with_name(dest.name + '.auto')
                    if auto_path.exists():
                        if auto_path.is_dir(): shutil.rmtree(auto_path)
                        else: auto_path.unlink()
                    dest.rename(auto_path)
                    print(f"[Launch] State written to {auto_path}")
                else:
                    print(f"[Launch] {file_type} written to {dest}")
            
            # Update cache
            if not isinstance(watcher.sync_cache.get(str(rom_id)), dict):
                watcher.sync_cache[str(rom_id)] = {}
            watcher.sync_cache[str(rom_id)][
                f'{file_type}_updated_at'] = server_updated_at
            watcher.save_cache()
        except Exception as e:
            print(f"[Launch] Failed to apply {file_type}: {e}")
        finally:
            if os.path.exists(tmp): os.remove(tmp)
        
        return True

    def play_game(self):
        platform = self.game.get('platform_slug')
        
        if self._is_windows:
            folder = self._local_rom_path
            if not folder or not folder.exists():
                QMessageBox.warning(self, "Game Folder Not Found", "The extracted game folder was not found.")
                self._update_button_states()
                return
            
            # Scan for EXEs
            exes = []
            exclude = ["unins000.exe", "uninstall.exe", "setup.exe", "redist", "vcredist", "directx", "dxsetup.exe", "crashpad_handler.exe", "notification_helper.exe"]
            for p in folder.rglob("*.exe"):
                name = p.name.lower()
                if not any(ex in name for ex in exclude):
                    exes.append(str(p))
            
            if not exes:
                QMessageBox.warning(self, "No Executables Found", "Could not find any game executables in the folder.")
                return
            
            exe_to_launch = None
            if len(exes) == 1:
                exe_to_launch = exes[0]
            else:
                picker = ExePickerDialog(exes, self.game.get("name"), self)
                if picker.exec() == QDialog.Accepted:
                    exe_to_launch = picker.selected_exe
                else:
                    return
            
            if exe_to_launch:
                try:
                    self.main_window.log(f"🚀 Launching Windows Game: {os.path.basename(exe_to_launch)}")
                    subprocess.Popen([exe_to_launch], cwd=os.path.dirname(exe_to_launch))
                    self.accept()
                except Exception as e:
                    QMessageBox.critical(self, "Launch Error", f"Failed to launch game: {e}")
            return

        base_rom = self.config.get("base_rom_path")
        rom_name = self.game.get('fs_name')
        
        local_rom = self._local_rom_path
        if not local_rom or not local_rom.exists():
            QMessageBox.warning(self, "ROM Not Found", f"Could not find {rom_name} in {base_rom}.\nPlease download it first.")
            return

        emu_data = None
        emu_display_name = None
        all_emus = emulators.load_emulators()
        
        # 1. Check platform_assignments FIRST (new feature)
        assigned_id = self.config.get("platform_assignments", {}).get(platform)
        if assigned_id:
            emu_data = next((e for e in all_emus if e["id"] == assigned_id), None)
            if emu_data and emu_data.get("executable_path") and os.path.exists(emu_data["executable_path"]):
                emu_display_name = emu_data["name"]
            else:
                emu_data = None # Assigned emu is invalid or missing

        # 2. Fallback: find first emulator that supports this platform (schema order)
        if not emu_data:
            emu_data = emulators.get_emulator_for_platform(platform)
            if emu_data and emu_data.get("executable_path") and os.path.exists(emu_data["executable_path"]):
                emu_display_name = emu_data["name"]
            else:
                emu_data = None

        # 3. Last Fallback: RetroArch
        if not emu_data:
            emu_data = next((e for e in all_emus if e["id"] == "retroarch"), None)
            if emu_data and emu_data.get("executable_path") and os.path.exists(emu_data["executable_path"]):
                emu_display_name = emu_data["name"]
                if platform in RETROARCH_PLATFORMS:
                    self.main_window.log(f"🎮 No dedicated emulator for {platform}, falling back to RetroArch")
            else:
                emu_data = None
        
        if not emu_data:
            QMessageBox.warning(self, "Emulator Not Set", 
                f"No emulator path set for {platform}.\n\n"
                "If you use RetroArch for this platform, make sure its path is set in the Emulators tab.")
            return

        self.main_window.log(f"🎮 Preparing {self.game.get('name')}...")
        self.main_window.ensure_watcher_running()
        
        try:
            # Build launch arguments
            args = []
            is_retroarch = emu_data["id"] == "retroarch"
            emu_path = emu_data["executable_path"]
            
            if is_retroarch:
                # Once-per-session auto-save prompt
                check_retroarch_autosave(emu_path, platform, self, self.config)

                core_name = RETROARCH_CORES.get(platform)

                # PSP asset check
                if platform == "psp" or core_name == "ppsspp_libretro.dll":
                    check_ppsspp_assets(emu_path, self)

                if core_name:
                    # Look for the core relative to the RetroArch exe location
                    emu_dir_path = Path(emu_path).parent
                    core_path = emu_dir_path / "cores" / core_name
                    if core_path.exists():
                        args = [emu_path, "-L", str(core_path), str(local_rom)]
                        self.main_window.log(f"🎮 Using core: {core_name}")
                    else:
                        # Core missing — offer to download
                        reply = QMessageBox.question(self, "Core Not Found",
                            f"The core '{core_name}' is not installed for {platform}.\n\n"
                            "Would you like Wingosy to download it automatically now?\n\n"
                            "(This uses RetroArch's buildbot — same source as Online Updater)",
                            QMessageBox.Yes | QMessageBox.No)
                        
                        if reply == QMessageBox.Yes:
                            self.start_core_download(core_name, emu_dir_path, platform)
                        return
                else:
                    # No known core for this platform — launch without -L and let RetroArch show its menu
                    args = [emu_path, str(local_rom)]
                    self.main_window.log(f"⚠️ No known RetroArch core for {platform}, launching without core")
            else:
                # Use custom launch args from schema
                raw_args = emu_data.get("launch_args", ["{rom_path}"])
                args = [emu_path]
                for arg in raw_args:
                    # Only append if it's not the executable itself (first item handled above)
                    processed = arg.replace("{rom_path}", str(local_rom))
                    if processed != emu_path:
                        args.append(processed)

            # Pre-launch sync
            watcher = self.main_window.watcher
            rom_id = self.game['id']
            title = self.game['name']
            
            if is_retroarch:
                save_info = watcher.get_retroarch_save_path(self.game, {"path": emu_path})
            else:
                full_cmd = f"\"{emu_path}\" \"{local_rom}\""
                res = watcher.resolve_save_path(emu_display_name, title, full_cmd, emu_path, platform)
                save_info = res[0] if res else None

            if self.main_window.config.get("auto_pull_saves", True):
                self.main_window.log(f"☁️ Checking cloud for {title}...")
                pull_completed = self._do_blocking_pull(save_info, is_retroarch)
                if pull_completed is False:
                    return  # user cancelled or error

            # Create a clean environment for the emulator
            clean_env = os.environ.copy()
            for key in ["QT_QPA_PLATFORM_PLUGIN_PATH", "QT_PLUGIN_PATH", "QT_QPA_FONTDIR", "QT_QPA_PLATFORM", "QT_STYLE_OVERRIDE"]:
                clean_env.pop(key, None)
            
            if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
                mei_path = str(Path(sys._MEIPASS)).lower()
                path_val = clean_env.get("PATH", "")
                path_parts = path_val.split(os.pathsep)
                new_path_parts = [p for p in path_parts if mei_path not in str(Path(p)).lower()]
                clean_env["PATH"] = os.pathsep.join(new_path_parts)

                conflicting_dlls = ['vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll', 'msvcp140_1.dll', 'concrt140.dll']
                for dll in conflicting_dlls:
                    dll_path = Path(sys._MEIPASS) / dll
                    if dll_path.exists():
                        try: dll_path.rename(dll_path.with_suffix('.dll.bak'))
                        except Exception: pass

            emu_dir_cwd = str(Path(emu_path).parent)
            proc = subprocess.Popen(args, env=clean_env, cwd=emu_dir_cwd)
            self.main_window.log(f"🚀 Launched {emu_display_name} (PID: {proc.pid})")
            
            if self.main_window.watcher:
                QTimer.singleShot(0, lambda: self.main_window.watcher.track_session(
                    proc, emu_display_name, self.game, str(local_rom), emu_path, skip_pull=True
                ))
            
            self.accept()
        except Exception as e:
            self.main_window.log(f"❌ Launch Error: {e}")
            QMessageBox.critical(self, "Launch Error", str(e))

    def download_rom(self, file_data):
        if self._is_windows:
            target_dir = Path(self.config.get("windows_games_dir"))
            target_path = target_dir / file_data['file_name']
        else:
            suggested = Path(self.config.get("base_rom_path")) / self.game.get('platform_slug', 'unknown')
            os.makedirs(suggested, exist_ok=True)
            target_path, _ = QFileDialog.getSaveFileName(self, "Save ROM", str(suggested / file_data['file_name']))
            if not target_path: return
            target_path = Path(target_path)

        self.dl_btn.setVisible(False)
        self.cancel_btn.setVisible(True)
        self.progress_bar.setVisible(True)
        self.speed_label.setText("Downloading...")
        
        thread = RomDownloader(self.client, self.game['id'], file_data['file_name'], str(target_path))
        self.main_window.active_threads.append(thread)
        self.main_window.download_queue.add_download(self.game.get('name'), thread)
        
        thread.progress.connect(lambda p, s: (self.progress_bar.setValue(p), self.speed_label.setText(f"Speed: {format_speed(s)}")))
        thread.finished.connect(self.on_download_complete)
        thread.finished.connect(lambda: self.main_window.download_queue.remove_download(thread))
        thread.finished.connect(lambda t=thread: self.main_window.active_threads.remove(t) if t in self.main_window.active_threads else None)
        self.dl_thread = thread
        thread.start()

    def cancel_dl(self):
        if self.dl_thread:
            self.dl_thread.requestInterruption()
            self.on_download_complete(False, "Cancelled")

    def on_download_complete(self, ok, path):
        if not ok:
            self.cancel_btn.setVisible(False)
            self.progress_bar.setVisible(False)
            self.speed_label.setText("")
            self._update_button_states()
            if path != "Cancelled":
                QMessageBox.critical(self, "Error", f"Download failed: {path}")
            return

        if self._is_windows:
            self.speed_label.setText("Extracting...")
            target_dir = self._local_rom_path # The stem folder
            self.extract_thread = ExtractionThread(path, target_dir)
            self.main_window.active_threads.append(self.extract_thread)
            self.extract_thread.progress.connect(self.speed_label.setText)
            self.extract_thread.finished.connect(self.on_extraction_complete)
            self.extract_thread.start()
        else:
            self._local_rom_path = Path(path)
            self.cancel_btn.setVisible(False)
            self.progress_bar.setVisible(False)
            self.speed_label.setText("")
            self._update_button_states()
            QMessageBox.information(self, "Success", f"Downloaded to {path}")
            self.main_window.fetch_library_and_populate()

    def on_extraction_complete(self, ok, msg):
        self.cancel_btn.setVisible(False)
        self.progress_bar.setVisible(False)
        self.speed_label.setText("Ready to play!" if ok else "")
        self._update_button_states()
        if ok:
            QMessageBox.information(self, "Success", "Game extracted and ready to play!")
            self.main_window.fetch_library_and_populate()
        else:
            QMessageBox.warning(self, "Extraction Finished", msg)

    def start_core_download(self, core_name, emu_dir, platform):
        progress_dlg = QDialog(self)
        progress_dlg.setWindowTitle(f"Downloading {core_name}...")
        progress_dlg.setFixedSize(350, 100)
        dlg_layout = QVBoxLayout(progress_dlg)
        status_label = QLabel(f"Downloading core for {platform}...")
        pbar = QProgressBar()
        dlg_layout.addWidget(status_label)
        dlg_layout.addWidget(pbar)
        progress_dlg.setWindowModality(Qt.ApplicationModal)
        
        thread = CoreDownloadThread(core_name, emu_dir / "cores")
        thread.progress.connect(lambda val, speed: (pbar.setValue(val), status_label.setText(f"Speed: {format_speed(speed)}")))
        
        def on_finished(success, msg):
            progress_dlg.close()
            if success:
                self.main_window.log(f"✨ Core {core_name} installed successfully.")
                self.play_game() # Relaunch!
            else:
                QMessageBox.critical(self, "Download Failed",
                    f"Could not download core: {msg}\n\n"
                    "Please try installing it manually via RetroArch's Online Updater.")
        
        thread.finished.connect(on_finished)
        thread.start()
        progress_dlg.exec()

class SettingsDialog(QDialog):
    def __init__(self, config_manager, main_window, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.config = config_manager
        self.main_window = main_window
        self.resize(400, 550)
        self.settings_layout = QVBoxLayout(self)

        host_layout = QHBoxLayout()
        host_layout.addWidget(QLabel("Server Host:"))
        self.host_input = QLineEdit()
        self.host_input.setText(self.config.get("host", ""))
        self.host_input.setPlaceholderText("http://192.168.x.x:8285")
        host_layout.addWidget(self.host_input)

        self.test_conn_btn = QPushButton("Test Connection")
        self.test_conn_btn.clicked.connect(self._test_host_connection)
        host_layout.addWidget(self.test_conn_btn)

        self.reconnect_btn = QPushButton("✅ Apply & Re-connect")
        self.reconnect_btn.setVisible(False)
        self.reconnect_btn.setStyleSheet(
            "background: #2e7d32; color: white; padding: 4px 10px;")
        self.reconnect_btn.clicked.connect(self._apply_and_restart)
        host_layout.addWidget(self.reconnect_btn)

        self.settings_layout.addLayout(host_layout)
        
        self.settings_layout.addWidget(QLabel(f"<b>User:</b> {self.config.get('username')}"))
        self.settings_layout.addWidget(QLabel(f"<b>Version:</b> {self.main_window.version}"))
        
        self.auto_pull_btn = QPushButton("Auto Pull Saves: ON" if self.config.get("auto_pull_saves", True) else "Auto Pull Saves: OFF")
        self.auto_pull_btn.setCheckable(True)
        self.auto_pull_btn.setChecked(self.config.get("auto_pull_saves", True))
        self.auto_pull_btn.toggled.connect(self.toggle_auto_pull)
        self.settings_layout.addWidget(self.auto_pull_btn)
        
        # Cards per row setting
        cards_row_layout = QHBoxLayout()
        cards_row_layout.addWidget(QLabel("Cards per row:"))
        self.cards_per_row_spin = QSpinBox()
        self.cards_per_row_spin.setMinimum(1)
        self.cards_per_row_spin.setMaximum(12)
        self.cards_per_row_spin.setValue(self.config.get("cards_per_row", 6))
        self.cards_per_row_spin.valueChanged.connect(self.set_cards_per_row)
        cards_row_layout.addWidget(self.cards_per_row_spin)
        cards_row_layout.addStretch()
        self.settings_layout.addLayout(cards_row_layout)
        
        # RetroArch save mode
        self.settings_layout.addWidget(QLabel("<b>RetroArch Save Mode:</b>"))
        self.ra_save_mode_combo = QComboBox()
        self.ra_save_mode_combo.addItems(["SRM only", "States only", "Both"])
        mode_map = {"srm": "SRM only", "state": "States only", "both": "Both"}
        current_mode = self.config.get("retroarch_save_mode", "srm")
        self.ra_save_mode_combo.setCurrentText(mode_map.get(current_mode, "SRM only"))
        self.ra_save_mode_combo.currentTextChanged.connect(self.set_ra_save_mode)
        self.settings_layout.addWidget(self.ra_save_mode_combo)

        # Windows Games Folder
        self.settings_layout.addWidget(QLabel("<b>Windows Games Folder:</b>"))
        win_folder_layout = QHBoxLayout()
        self.win_folder_input = QLineEdit(self.config.get("windows_games_dir", ""))
        win_folder_layout.addWidget(self.win_folder_input)
        browse_win_btn = QPushButton("Browse")
        browse_win_btn.clicked.connect(self.browse_windows_folder)
        win_folder_layout.addWidget(browse_win_btn)
        self.settings_layout.addLayout(win_folder_layout)

        # Log level setting
        log_level_layout = QHBoxLayout()
        log_level_layout.addWidget(QLabel("<b>Log Level:</b>"))
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        current_level = self.config.get("log_level", "INFO").upper()
        self.log_level_combo.setCurrentText(current_level)
        self.log_level_combo.currentTextChanged.connect(self.set_log_level)
        log_level_layout.addWidget(self.log_level_combo)
        log_level_layout.addStretch()
        self.settings_layout.addLayout(log_level_layout)
        
        self.settings_layout.addSpacing(10)
        self.update_btn = QPushButton("Check for Updates")
        self.update_btn.clicked.connect(self.check_updates)
        self.settings_layout.addWidget(self.update_btn)
        
        self.upgrade_btn = QPushButton("Upgrade Available!")
        self.upgrade_btn.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold;")
        self.upgrade_btn.setVisible(False)
        self.settings_layout.addWidget(self.upgrade_btn)

        self.update_pbar = QProgressBar()
        self.update_pbar.setVisible(False)
        self.settings_layout.addWidget(self.update_pbar)
        
        self.settings_layout.addStretch()
        
        self.about_btn = QPushButton("ℹ️ About Wingosy")
        self.about_btn.clicked.connect(self.show_about)
        self.settings_layout.addWidget(self.about_btn)
        
        self.logout_btn = QPushButton("Log Out")
        self.logout_btn.setStyleSheet("background-color: #c62828; color: white; padding: 8px;")
        self.logout_btn.clicked.connect(self.do_logout)
        self.settings_layout.addWidget(self.logout_btn)
        
        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.reject)
        self.settings_layout.addWidget(buttons)

    def browse_windows_folder(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Windows Games Folder")
        if directory:
            self.win_folder_input.setText(directory)
            self.config.set("windows_games_dir", directory)

    def _test_host_connection(self):
        host = self.host_input.text().strip()
        if not host:
            QMessageBox.warning(self, "No Host", "Please enter a host URL.")
            return
        self.test_conn_btn.setText("Testing...")
        self.test_conn_btn.setEnabled(False)
        
        # Use the unified test_connection method from the client with retry feedback
        success, message = self.main_window.client.test_connection(
            host_override=host,
            retry_callback=lambda: self.test_conn_btn.setText("Retrying (slow server)...")
        )
        
        self.test_conn_btn.setText("Test Connection")
        self.test_conn_btn.setEnabled(True)
        if success:
            QMessageBox.information(self, "Success",
                f"{message} Click 'Apply & Reconnect' to use this host.")
            self.reconnect_btn.setVisible(True)
        else:
            QMessageBox.warning(self, "Failed", message)
            self.reconnect_btn.setVisible(False)

    def _apply_and_restart(self):
        import logging
        import time
        new_host = self.host_input.text().strip()
        logging.info("[Restart] _apply_and_restart called")
        logging.info(f"[Restart] new host={new_host}")
        
        # Save config first
        self.config.set("host", new_host)
        
        # Small delay to ensure config is flushed to disk
        time.sleep(0.3)
        
        QMessageBox.information(self, "Restarting",
            "Host saved. The app will now restart.")
        
        logging.info("[Restart] config saved, calling _do_restart")
        self._do_restart()

    def _do_restart(self):
        import logging
        import subprocess
        import sys
        import os
        
        logging.info("[Restart] _do_restart called")
        logging.info(f"[Restart] frozen="
                     f"{getattr(sys, 'frozen', False)}")
        logging.info(f"[Restart] sys.executable={sys.executable}")
        logging.info(f"[Restart] sys.argv={sys.argv}")
        
        exe = sys.executable  # Always the correct exe, 
                               # frozen or not
        
        try:
            logging.info(f"[Restart] about to Popen: {exe}")
            
            if sys.platform == "win32":
                # Windows: detached process so it survives
                # parent exit
                DETACHED_PROCESS = 0x00000008
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                subprocess.Popen(
                    [exe],
                    close_fds=True,
                    creationflags=(
                        DETACHED_PROCESS | 
                        CREATE_NEW_PROCESS_GROUP),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    [exe],
                    close_fds=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            
            logging.info("[Restart] Popen complete")
        except Exception as e:
            logging.exception(f"[Restart] Popen failed: {e}")
            return
        
        logging.info("[Restart] calling sys.exit(0)")
        sys.exit(0)

    def show_about(self):
        QMessageBox.about(self, "About Wingosy",
            f"<b>Wingosy Launcher</b> v{self.main_window.version}<br><br>"
            "A lightweight Windows game launcher for RomM.<br>"
            "Licensed under GNU GPL v3.0.<br><br>"
            "<a href='https://github.com/abduznik/Wingosy-Launcher'>GitHub Repository</a>"
        )

    def toggle_auto_pull(self, checked):
        self.config.set("auto_pull_saves", checked)
        self.auto_pull_btn.setText("Auto Pull Saves: ON" if checked else "Auto Pull Saves: OFF")

    def set_cards_per_row(self, value):
        self.config.set("cards_per_row", value)
        lib = self.main_window.library_tab
        lib._resize_all_cards()

    def set_log_level(self, text):
        self.config.set("log_level", text)
        level = getattr(logging, text.upper(), logging.INFO)
        logging.getLogger().setLevel(level)
        logging.info(f"Log level changed to {text}")

    def set_ra_save_mode(self, text):
        mode_map = {"SRM only": "srm", "States only": "state", "Both": "both"}
        self.config.set("retroarch_save_mode", mode_map.get(text, "srm"))

    def check_updates(self):
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Checking...")
        self.updater = UpdaterThread(self.main_window.version)
        self.updater.finished.connect(self.on_update_result)
        self.updater.start()

    def on_update_result(self, available, version, url):
        self.update_btn.setEnabled(True)
        self.update_btn.setText("Check for Updates")
        if available:
            self.latest_version_url = url
            self.upgrade_btn.setText(f"Upgrade to v{version}")
            self.upgrade_btn.setVisible(True)
            try: self.upgrade_btn.clicked.disconnect()
            except Exception: pass
            
            if getattr(sys, 'frozen', False):
                self.upgrade_btn.clicked.connect(self.start_self_update)
            else:
                self.upgrade_btn.clicked.connect(lambda: webbrowser.open(url))
        else:
            QMessageBox.information(self, "No Updates", "You are running the latest version.")

    def start_self_update(self):
        self.upgrade_btn.setEnabled(False)
        self.upgrade_btn.setText("Downloading update...")
        self.update_pbar.setVisible(True)
        self.update_pbar.setValue(0)
        
        current_exe = Path(sys.executable).resolve()
        self.updater_thread = SelfUpdateThread(self.latest_version_url, current_exe)
        self.updater_thread.progress.connect(self.update_pbar.setValue)
        self.updater_thread.finished.connect(self.on_self_update_finished)
        self.updater_thread.start()

    def on_self_update_finished(self, success, message):
        if success:
            QMessageBox.information(self, "Update Complete", "Update downloaded! Click OK to restart Wingosy.")
            current_exe = Path(sys.executable).resolve()
            pid = os.getpid()
            bat_path = current_exe.parent / "_wingosy_restart.bat"
            bat_content = (
                f'@echo off\n'
                f':wait\n'
                f'tasklist /FI "PID eq {pid}" 2>NUL | find /I "{pid}" >NUL\n'
                f'if not errorlevel 1 (\n'
                f'    timeout /t 1 /nobreak >NUL\n'
                f'    goto wait\n'
                f')\n'
                f'start "" "{current_exe}"\n'
                f'del "%~f0"\n'
            )
            bat_path.write_text(bat_content)
            subprocess.Popen(
                ['cmd.exe', '/c', str(bat_path)],
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            QApplication.instance().quit()
        else:
            QMessageBox.critical(self, "Update Failed", f"Could not replace the current file. Please download manually.\nError: {message}")
            self.upgrade_btn.setEnabled(True)
            self.upgrade_btn.setText("Retry Update")
            webbrowser.open(self.latest_version_url)

    def do_logout(self):
        reply = QMessageBox.question(self, "Log Out", "Are you sure you want to log out? You will need to enter your credentials again.", QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
            
        self.main_window.client.logout()
        self.config.set("password", None)
        QMessageBox.information(self, "Logged Out", "You have been logged out. Restart to log in.")
        QApplication.instance().quit()

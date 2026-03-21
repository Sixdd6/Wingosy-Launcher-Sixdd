import os
import shutil
import subprocess
import logging
import zipfile
import time
from pathlib import Path
import json
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar, QScrollArea, QFileDialog, QApplication, QDialog, QSizePolicy, QFrame)
from PySide6.QtCore import Qt, QTimer, Signal, QThread, QPoint, QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QPixmap, QColor, QMovie, QPainter
from PySide6.QtSvg import QSvgRenderer

from src.ui.dialogs.styled_messagebox import StyledMessageBox

from src.ui.threads import (RomDownloader, ImageFetcher, ConflictResolveThread, GameDescriptionFetcher, RomDetailsFetcher, ExtractionThread)
from src.ui.widgets import format_size, get_resource_path, format_speed
from src.platforms import RETROARCH_CORES
from src import emulators, windows_saves, download_registry
from src.save_strategies import get_strategy
from src.utils import read_retroarch_cfg, write_retroarch_cfg_values, extract_strip_root, resolve_local_rom_path

_retroarch_autosave_checked = False
_ppsspp_assets_checked = False

WINDOWS_PLATFORM_SLUGS = ["windows", "win", "pc", "pc-windows", "windows-games", "win95", "win98", "win9x", "windows9x"]
EXCLUDED_EXES = [
    "unins000.exe", "uninstall.exe", "setup.exe",
    "crashpad_handler.exe", "notification_helper.exe", "UnityCrashHandler",
    "installscript", "redist", "socialclub",
    "epicportal", "launcher", "activation",
    "touchup", "cleanup", "webhelper"
]


class UninstallConfirmDialog(QDialog):
    def __init__(self, title_text, message, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title_text)
        self.setFixedSize(520, 230)

        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._drag_pos = None

        self.setStyleSheet("""
            #UninstallRoot {
                background-color: #1a1a1a;
                color: #ffffff;
                border: 1px solid #2f2f2f;
                border-radius: 10px;
            }
            QLabel {
                color: #ffffff;
                font-size: 12px;
            }
            QLabel#DlgTitle {
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton {
                border-radius: 8px;
                padding: 10px 12px;
                min-height: 36px;
                background: #2b2b2b;
                border: 1px solid #3a3a3a;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #353535;
                border-color: #4a4a4a;
            }
            QPushButton:pressed {
                background: #242424;
            }
            QPushButton:focus {
                border-color: #6b6b6b;
            }
            QPushButton#DangerBtn {
                background: #8e0000;
                border-color: #a40000;
                color: #ffffff;
            }
            QPushButton#DangerBtn:hover {
                background: #a40000;
            }
            QPushButton#CloseBtn {
                min-height: 28px;
                padding: 4px 10px;
                border-radius: 6px;
                background: transparent;
                border: 1px solid transparent;
                color: #cfcfcf;
                font-weight: 700;
            }
            QPushButton#CloseBtn:hover {
                background: #2b2b2b;
                border-color: #3a3a3a;
                color: #ffffff;
            }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)

        root = QFrame(self)
        root.setObjectName("UninstallRoot")
        outer.addWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)

        title_lbl = QLabel(title_text)
        title_lbl.setObjectName("DlgTitle")
        title_row.addWidget(title_lbl, 1)

        close_btn = QPushButton("✕")
        close_btn.setObjectName("CloseBtn")
        close_btn.setFixedWidth(36)
        close_btn.clicked.connect(self.reject)
        title_row.addWidget(close_btn)

        layout.addLayout(title_row)

        msg_lbl = QLabel(message)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet("color: #d9d9d9;")
        layout.addWidget(msg_lbl)
        layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        uninstall_btn = QPushButton("Uninstall")
        uninstall_btn.setObjectName("DangerBtn")
        uninstall_btn.clicked.connect(self.accept)
        btn_row.addWidget(uninstall_btn)

        layout.addLayout(btn_row)

        cancel_btn.setDefault(True)
        cancel_btn.setAutoDefault(True)

        QTimer.singleShot(0, self._apply_dark_frame)
        QTimer.singleShot(50, self._center_on_parent)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and (event.buttons() & Qt.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def _apply_dark_frame(self):
        import sys, ctypes
        if sys.platform == "win32":
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()),
                    20,
                    ctypes.byref(ctypes.c_int(1)),
                    4,
                )
            except Exception:
                pass

    def _center_on_parent(self):
        p = self.parent()
        if not p:
            return
        pg = p.geometry()
        x = pg.x() + (pg.width() - self.width()) // 2
        y = pg.y() + (pg.height() - self.height()) // 2
        self.move(x, y)


def check_retroarch_autosave(ra_exe_path, platform_slug, parent, config=None):
    global _retroarch_autosave_checked
    if _retroarch_autosave_checked:
        return
    _retroarch_autosave_checked = True
    
    if platform_slug in ("psp", "playstation-portable"):
        return
        
    save_mode = config.get("retroarch_save_mode", "srm") if config else "srm"
    if save_mode == "srm":
        return
        
    cfg_path = Path(ra_exe_path).parent / "retroarch.cfg"
    if not cfg_path.exists():
        return
        
    cfg = read_retroarch_cfg(str(cfg_path))
    auto_save = cfg.get("savestate_auto_save", "false")
    auto_load = cfg.get("savestate_auto_load", "false")
    
    if auto_save == "true" and auto_load == "true":
        return
        
    missing = []
    if auto_save != "true": missing.append("savestate_auto_save")
    if auto_load != "true": missing.append("savestate_auto_load")
    
    result = StyledMessageBox.question(
        parent, 
        "RetroArch Auto-Save States — Wingosy", 
        f"Enable auto save/load states in retroarch.cfg?\n\nMissing: {', '.join(missing)}", 
        StyledMessageBox.Yes | StyledMessageBox.No
    )
    
    if result == StyledMessageBox.Yes:
        write_retroarch_cfg_values(str(cfg_path), {"savestate_auto_save": "true", "savestate_auto_load": "true"})
        StyledMessageBox.information(parent, "RetroArch Auto-Save States — Wingosy", "✅ Auto save/load states enabled.")

def check_ppsspp_assets(ra_exe_path, parent):
    global _ppsspp_assets_checked
    if _ppsspp_assets_checked:
        return
    _ppsspp_assets_checked = True
    
    system_ppsspp = Path(ra_exe_path).parent / "system" / "PPSSPP"
    if (system_ppsspp / "ppge_atlas.zim").exists():
        return
        
    result = StyledMessageBox.question(
        parent, 
        "PPSSPP Assets Missing — Wingosy", 
        "Download missing PPSSPP assets now?", 
        StyledMessageBox.Yes | StyledMessageBox.No
    )
    
    if result != StyledMessageBox.Yes:
        return
        
    progress = StyledMessageBox(
        parent,
        "Downloading PPSSPP Assets — Wingosy",
        "Downloading...",
        buttons=0,
    )
    progress.show()
    QApplication.processEvents()
    
    try:
        import urllib.request, tempfile
        url = "https://buildbot.libretro.com/assets/system/PPSSPP.zip"
        system_ppsspp.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name
        
        urllib.request.urlretrieve(url, tmp_path)
        with zipfile.ZipFile(tmp_path, 'r') as z:
            for member in z.namelist():
                rel = member[len("PPSSPP/"):] if member.startswith("PPSSPP/") else member
                if not rel: continue
                target = system_ppsspp / rel
                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(member) as src, open(target, 'wb') as dst:
                        dst.write(src.read())
        
        Path(tmp_path).unlink(missing_ok=True)
        progress.close()
        StyledMessageBox.information(parent, "PPSSPP Assets Ready — Wingosy", "✅ Done.")
    except Exception as e:
        progress.close()
        StyledMessageBox.warning(parent, "Download Failed — Wingosy", str(e))

class GameDetailPanel(QWidget):
    def __init__(self, game, client, config, main_window, on_close=None, parent=None):
        super().__init__(parent)
        self._on_close = on_close
        self.game = game
        self.client = client
        self.config = config
        self.main_window = main_window
        self._uninstall_dialog_open = False
        self._pending_registry_update = None
        self._smoothed_speed = 0.0
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(100)
        self._flush_timer.timeout.connect(self._flush_pending_registry_update)
        
        self.dl_thread = None
        self.extract_thread = None
        self._is_windows = game.get("platform_slug") in WINDOWS_PLATFORM_SLUGS
        self._local_rom_path = self._get_local_rom_path()

        self.setStyleSheet("""
            QWidget {
                background-color: #1a1a1a;
                color: #ffffff;
            }
            QLabel {
                color: #ffffff;
            }
            QPushButton {
                border-radius: 4px;
            }
            QScrollArea {
                background: transparent;
                border: none;
            }
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Header
        main_layout.addWidget(self._build_header(game.get('name', '')))

        # Content area
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 20, 24, 20)
        content_layout.setSpacing(10)

        sub_layout = QHBoxLayout()
        sub_layout.setSpacing(25)

        left_col_widget = QWidget()
        left_col_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        left_col_layout = QVBoxLayout(left_col_widget)
        left_col_layout.setContentsMargins(0, 0, 0, 0)
        left_col_layout.setSpacing(8)
        self._left_col_layout = left_col_layout

        self.badges_row = self._build_badges_row()
        left_col_layout.addWidget(self.badges_row, 0, Qt.AlignLeft)

        self.img_label = QLabel()
        self.img_label.setMaximumWidth(900)
        self.img_label.setMinimumWidth(280)
        self.img_label.setMinimumHeight(400)
        self.img_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.img_label.setAlignment(Qt.AlignCenter)
        self.img_label.setStyleSheet("background: #111; border-radius: 6px;")
        left_col_layout.addWidget(self.img_label, 1)

        sub_layout.addWidget(left_col_widget)

        right_col_widget = QWidget()
        right_col_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.right_column = QVBoxLayout(right_col_widget)
        self.right_column.setContentsMargins(0, 0, 0, 0)
        self.right_column.setSpacing(0)

        try:
            badge_h = int(self.badges_row.sizeHint().height())
        except Exception:
            badge_h = 0
        if badge_h > 0:
            self.right_column.addSpacing(badge_h + left_col_layout.spacing())

        self.title_label = QLabel(game.get('name'))
        self.title_label.setStyleSheet("font-size: 20pt; font-weight: bold; color: #1e88e5; background: transparent; margin-bottom: 8px;")
        self.title_label.setWordWrap(True)
        self.right_column.addWidget(self.title_label)

        self.right_column.addWidget(QLabel(f"<b>Platform:</b> {game.get('platform_display_name')}", styleSheet="font-size: 12pt; margin-bottom: 2px; background: transparent;"))

        total_bytes = sum(f.get('file_size_bytes', 0) for f in game.get('files', []))
        self.right_column.addWidget(QLabel(f"<b>Size:</b> {format_size(total_bytes)}", styleSheet="font-size: 12pt; margin-bottom: 8px; background: transparent;"))

        self.release_label = QLabel("<b>Release:</b> Loading...")
        self.release_label.setStyleSheet("font-size: 12pt; margin-bottom: 2px; background: transparent;")
        self.right_column.addWidget(self.release_label)

        self.genres_label = QLabel("<b>Genres:</b> Loading...")
        self.genres_label.setWordWrap(True)
        self.genres_label.setStyleSheet("font-size: 12pt; margin-bottom: 2px; background: transparent;")
        self.right_column.addWidget(self.genres_label)

        self.rating_label = QLabel("<b>Rating:</b> Loading...")
        self.rating_label.setStyleSheet("font-size: 12pt; margin-bottom: 2px; background: transparent;")
        self.right_column.addWidget(self.rating_label)

        self.companies_label = QLabel("<b>Developer/Publisher:</b> Loading...")
        self.companies_label.setWordWrap(True)
        self.companies_label.setStyleSheet("font-size: 12pt; margin-bottom: 2px; background: transparent;")
        self.right_column.addWidget(self.companies_label)

        self.players_label = QLabel("<b>Max Players:</b> Loading...")
        self.players_label.setStyleSheet("font-size: 12pt; margin-bottom: 2px; background: transparent;")
        self.right_column.addWidget(self.players_label)

        self.playtime_label = QLabel("<b>Playtime:</b> Loading...")
        self.playtime_label.setStyleSheet("font-size: 12pt; margin-bottom: 8px; background: transparent;")
        self.right_column.addWidget(self.playtime_label)

        self.desc_scroll = QScrollArea()
        self.desc_scroll.setWidgetResizable(True)
        self.desc_scroll.setStyleSheet("background: transparent; border: none;")

        self.desc_label = QLabel("Loading description...")
        self.desc_label.setWordWrap(True)
        self.desc_label.setAlignment(Qt.AlignTop)
        self.desc_label.setStyleSheet("color: #ccc; font-size: 11pt; line-height: 1.4; background: transparent;")
        self.desc_scroll.setWidget(self.desc_label)
        self.right_column.addWidget(self.desc_scroll, 1)

        self._screenshot_threads = []
        self._screenshot_items = []
        self.screenshots_scroll = QScrollArea()
        self.screenshots_scroll.setWidgetResizable(True)
        self.screenshots_scroll.setStyleSheet("background: transparent; border: none;")
        self.screenshots_scroll.setFixedWidth(260)

        self.screenshots_container = QWidget()
        self.screenshots_layout = QVBoxLayout(self.screenshots_container)
        self.screenshots_layout.setContentsMargins(0, 0, 0, 0)
        self.screenshots_layout.setSpacing(10)
        self.screenshots_scroll.setWidget(self.screenshots_container)
        self.screenshots_scroll.setVisible(True)

        self.screenshots_empty_label = QLabel("No screenshots")
        self.screenshots_empty_label.setAlignment(Qt.AlignCenter)
        self.screenshots_empty_label.setStyleSheet("color: #666; font-size: 11pt; background: transparent;")
        self.screenshots_layout.addWidget(self.screenshots_empty_label)

        self.pbar = QProgressBar()
        self.pbar.setVisible(False)
        self.pbar.setStyleSheet("""
            QProgressBar {
                border: none;
                border-radius: 3px;
                background: #2d2d2d;
                height: 8px;
            }
            QProgressBar::chunk {
                border-radius: 3px;
                background: #0d6efd;
            }
        """)
        self.right_column.addWidget(self.pbar)

        self.speed_label = QLabel()
        self.speed_label.setAlignment(Qt.AlignCenter)
        self.speed_label.setStyleSheet("background: transparent;")
        self.right_column.addWidget(self.speed_label)

        self.actions_layout = QVBoxLayout()
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(4)

        self.play_btn = QPushButton("▶ PLAY")
        self.play_btn.setStyleSheet("background: #2e7d32; color: white; font-weight: bold; padding: 12px; font-size: 16pt;")
        self.play_btn.clicked.connect(self.play_game)

        self.gs_btn = QPushButton("⚙ Game Settings")
        self.gs_btn.setStyleSheet("background: #455a64; color: white; padding: 8px; font-size: 11pt;")
        self.gs_btn.clicked.connect(self.open_game_settings)

        self.un_btn = QPushButton("🗑 Uninstall")
        self.un_btn.setStyleSheet("background: #8e0000; color: white; padding: 8px; font-size: 13pt;")
        self.un_btn.clicked.connect(self.uninstall_game)

        self.cloud_btn = QPushButton("☁️ Cloud Saves")
        self.cloud_btn.setStyleSheet("background: #0d47a1; color: white; padding: 8px; font-size: 11pt;")
        self.cloud_btn.clicked.connect(self.open_cloud_manager)

        self.dl_btn = QPushButton("⬇ DOWNLOAD")
        self.dl_btn.setStyleSheet("background: #1565c0; color: white; font-weight: bold; padding: 12px; font-size: 16pt;")
        self.dl_btn.clicked.connect(self._on_download_clicked)

        self.can_btn = QPushButton("Cancel Download")
        self.can_btn.setStyleSheet("background: #c62828; color: white; font-size: 12pt;")
        self.can_btn.setVisible(False)
        self.can_btn.clicked.connect(self.cancel_dl)

        self.actions_layout.addWidget(self.play_btn)
        self.actions_layout.addWidget(self.gs_btn)
        self.actions_layout.addWidget(self.un_btn)
        self.actions_layout.addWidget(self.dl_btn)
        self.actions_layout.addWidget(self.can_btn)

        self.right_column.addLayout(self.actions_layout)
        sub_layout.addWidget(right_col_widget)

        sub_layout.addWidget(self.screenshots_scroll, 0)

        # Fixed ratio between left cover column and right metadata column
        # (screenshots column remains fixed-width)
        sub_layout.setStretch(0, 4)
        sub_layout.setStretch(1, 6)
        content_layout.addLayout(sub_layout)

        main_layout.addWidget(content, 1)

        # After building the UI, check registry
        self._reconnect_active_download()
        self.destroyed.connect(self._cleanup)
            
        self._cover_full_pixmap = None
        self._cover_movie = None
        self._cover_movie_buffer = None
        self._start_image_fetch()
        self._start_metadata_fetch()

    def _render_svg_icon(self, relative_svg_path, w, h):
        try:
            svg_path = get_resource_path(relative_svg_path)
            renderer = QSvgRenderer(svg_path)
            if not renderer.isValid():
                return QPixmap()
            pm = QPixmap(max(1, int(w)), max(1, int(h)))
            pm.fill(Qt.transparent)
            painter = QPainter(pm)
            try:
                renderer.render(painter)
            finally:
                painter.end()
            return pm
        except Exception:
            return QPixmap()

    def _build_badges_row(self):
        row = QWidget()
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row.setMinimumHeight(28)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        rom_id = str(self.game.get('id', ''))
        installed = bool(self.game.get('_local_exists'))
        if not installed:
            try:
                p = self._get_local_rom_path()
                if self._is_windows and p and p.is_dir():
                    installed = any(p.rglob("*.exe")) or any(p.rglob("*.bat")) or any(p.rglob("*.cmd"))
                else:
                    installed = bool(p and p.exists())
            except Exception:
                installed = bool(self.game.get('_local_exists'))

        has_cloud_save = False
        try:
            watcher = getattr(self.main_window, 'watcher', None)
            sync_cache = watcher.sync_cache if watcher and isinstance(watcher.sync_cache, dict) else {}
            entry = sync_cache.get(rom_id, {}) if isinstance(sync_cache, dict) else {}
            if isinstance(entry, dict):
                has_cloud_save = bool(
                    entry.get('save_updated_at') or entry.get('save_mtime') or
                    entry.get('state_updated_at') or entry.get('state_mtime')
                )
        except Exception:
            has_cloud_save = False

        def _badge_pill(bg, svg_path, text):
            w = QWidget()
            w.setStyleSheet(f"background: {bg}; border-radius: 10px;")
            hl = QHBoxLayout(w)
            hl.setContentsMargins(8, 4, 8, 4)
            hl.setSpacing(6)

            icon = QLabel()
            pm = self._render_svg_icon(svg_path, 14, 14)
            if not pm.isNull():
                icon.setPixmap(pm)
            icon.setStyleSheet("background: transparent;")
            hl.addWidget(icon)

            lbl = QLabel(text)
            lbl.setStyleSheet("background: transparent; color: white; font-weight: bold;")
            hl.addWidget(lbl)
            return w

        if installed:
            layout.addWidget(_badge_pill("#4caf50", "assets/library-svgrepo-com.svg", "Installed"))

        if has_cloud_save:
            layout.addWidget(_badge_pill("#1565c0", "assets/save-floppy-svgrepo-com.svg", "Cloud"))

        layout.addStretch(1)
        return row

    def refresh_badges_row(self):
        try:
            new_row = self._build_badges_row()
            old_row = getattr(self, 'badges_row', None)
            layout = getattr(self, '_left_col_layout', None)
            if not layout:
                return

            if old_row is not None:
                try:
                    layout.replaceWidget(old_row, new_row)
                except Exception:
                    try:
                        layout.removeWidget(old_row)
                        layout.insertWidget(0, new_row, 0, Qt.AlignLeft)
                    except Exception:
                        return
                try:
                    old_row.setParent(None)
                    old_row.deleteLater()
                except Exception:
                    pass
            else:
                try:
                    layout.insertWidget(0, new_row, 0, Qt.AlignLeft)
                except Exception:
                    return

            self.badges_row = new_row
            try:
                self.updateGeometry()
            except Exception:
                pass
        except Exception:
            return

    def _cleanup(self):
        rom_id = str(self.game["id"])
        if hasattr(self, '_progress_listener'):
            download_registry.remove_listener(rom_id, self._progress_listener)
        if hasattr(self, '_flush_timer') and self._flush_timer.isActive():
            self._flush_timer.stop()
        try:
            mv = getattr(self, "_cover_movie", None)
            if mv is not None:
                try:
                    mv.stop()
                except Exception:
                    pass
            self._cover_movie = None
            self._cover_movie_buffer = None
        except Exception:
            self._cover_movie = None
            self._cover_movie_buffer = None

    def _build_header(self, game_name):
        header = QWidget()
        header.setFixedHeight(44)
        header.setStyleSheet("background: #111; border-bottom: 1px solid #222;")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 0, 12, 0)
        
        back_btn = QPushButton("← Back to Library")
        back_btn.setFixedWidth(180)
        back_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #888;
                border: none;
                font-size: 14px;
                padding: 6px 10px;
                text-align: left;
            }
            QPushButton:hover {
                color: #ffffff;
            }
        """)
        back_btn.clicked.connect(self._close)
        hl.addWidget(back_btn)
        hl.addStretch()
        return header

    def _close(self):
        if self._on_close:
            self._on_close()

    def _reconnect_active_download(self):
        rom_id = str(self.game["id"])
        entry = download_registry.get(rom_id)
        
        if not entry:
            self._update_button_states()
            return
        
        # Active download or extraction found!
        row_type = entry["type"]
        current, total = entry["progress"]
        
        self.play_btn.hide()
        self.dl_btn.hide()
        self.un_btn.hide()
        
        self.pbar.setVisible(True)
        if total > 0:
            self.pbar.setRange(0, 100)
            self.pbar.setValue(int(current / total * 100))
        else:
            self.pbar.setRange(0, 0)
        
        if row_type == "download":
            self.speed_label.setText("Downloading...")
        else:
            self.speed_label.setText("Extracting...")
        
        self.can_btn.show()
        
        self._progress_listener = self._on_registry_progress
        download_registry.add_listener(rom_id, self._progress_listener)

    def _on_registry_progress(self, rom_id, rtype, current, total, speed=0):
        if rtype in ("done", "cancelled"):
            if self._flush_timer.isActive():
                self._flush_timer.stop()
            self._pending_registry_update = None
            self._apply_registry_progress(rom_id, rtype, current, total, speed)
            return

        self._pending_registry_update = (rom_id, rtype, current, total, speed)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_pending_registry_update(self):
        if not self._pending_registry_update:
            self._flush_timer.stop()
            return
        rom_id, rtype, current, total, speed = self._pending_registry_update
        self._pending_registry_update = None
        self._apply_registry_progress(rom_id, rtype, current, total, speed)

    def _apply_registry_progress(self, rom_id, rtype, current, total, speed=0):
        if rtype == "done" or rtype == "cancelled":
            download_registry.remove_listener(rom_id, self._progress_listener)
            self.pbar.setVisible(False)
            self.can_btn.hide()
            self.speed_label.setText("")
            self._update_button_states()
            try:
                self.refresh_badges_row()
            except Exception:
                pass
            return

        if total > 0:
            self.pbar.setRange(0, 100)
            self.pbar.setValue(int(current / total * 100))
        else:
            self.pbar.setRange(0, 0)

        if speed > 0 and rtype == "download":
            alpha = 0.2
            if self._smoothed_speed <= 0:
                self._smoothed_speed = float(speed)
            else:
                self._smoothed_speed = (alpha * float(speed)) + ((1.0 - alpha) * self._smoothed_speed)

        if rtype == "download":
            speed_txt = f" @ {format_speed(self._smoothed_speed)}" if self._smoothed_speed > 0 else ""
            self.speed_label.setText(f"Downloading... {format_size(current)} / {format_size(total)}{speed_txt}")
        elif rtype == "extraction":
            if total > 0:
                self.speed_label.setText(f"Extracting... {current}/{total} files")
            else:
                self.speed_label.setText("Extracting...")

    def download_rom(self, file_obj):
        if not file_obj: return
        
        # Determine target path
        if self._is_windows:
            target_dir = Path(self.config.get("windows_games_dir"))
            target_path = target_dir / file_obj['file_name']
        else:
            target_dir = Path(self.config.get("base_rom_path")) / self.game.get('platform_slug')
            target_path = target_dir / file_obj['file_name']
            
        os.makedirs(target_dir, exist_ok=True)
        
        self.dl_thread = RomDownloader(self.client, self.game['id'], file_obj['file_name'], str(target_path))
        download_registry.register_download(self.game['id'], self.game['name'], self.dl_thread)
        
        self.dl_thread.progress.connect(lambda d, t, s: download_registry.update_progress(self.game['id'], d, t, s))
        self.dl_thread.finished.connect(lambda ok, p: self._on_download_finished(ok, p))
        
        self.main_window.download_queue.add_download(self.game['name'], self.dl_thread, "download", self.game['id'])
        self.dl_thread.start()
        self._reconnect_active_download()

    def _on_download_finished(self, ok, path):
        if not ok:
            download_registry.unregister(self.game['id'])
            return
            
        # If it's an archive and we are on Windows, or just need extraction
        if path.endswith(('.zip', '.7z', '.iso')):
            # Pre-fetch 7z.exe in background so extraction starts immediately
            from src.sevenzip import get_7zip_exe
            from PySide6.QtCore import QThread
            
            class SevenZipFetcher(QThread):
                ready = Signal(str)
                def run(self):
                    exe = get_7zip_exe()
                    self.ready.emit(exe or "")
            
            self.speed_label.setText("Preparing extractor...")
            self._sz_fetcher = SevenZipFetcher()
            self._sz_fetcher.ready.connect(lambda exe: self._start_extraction(path))
            self._sz_fetcher.start()
        else:
            download_registry.unregister(self.game['id'])
            # Direct file download complete — mark local exists
            self.game['_local_exists'] = True
            self._update_button_states()
            try:
                self.refresh_badges_row()
            except Exception:
                pass
            self.main_window.library_tab.apply_filters()

    def _on_extraction_finished(self, path):
        download_registry.unregister(self.game['id'])
        # Mark local exists directly on the game dict so _update_button_states works
        self.game['_local_exists'] = True
        self._update_button_states()
        try:
            self.refresh_badges_row()
        except Exception:
            pass
        # Refresh library in background to update card in grid
        # Use apply_filters instead of full re-fetch to avoid closing the detail panel
        self.main_window.library_tab.apply_filters()

    def cancel_dl(self):
        rom_id = str(self.game["id"])
        entry = download_registry.get(rom_id)
        if not entry or not entry.get("thread"):
            return

        rom_name = self.game.get('name', 'this game')

        # Immediately cancel without prompting
        entry["thread"].cancel()

        self.game['_local_exists'] = False # Reset state immediately
        # 2. Update status in registry (thread will handle unregistering)
        download_registry.update_status(rom_id, "cancelled")
        
        self.can_btn.hide()
        self.pbar.hide()
        self._update_button_states()

    def _get_local_rom_path(self):
        return resolve_local_rom_path(self.game, self.config.data)
        
    def _update_button_states(self):
        self._local_rom_path = self._get_local_rom_path()
        p = self._local_rom_path
        
        # If explicitly marked as not installed (e.g. after cancel), trust that
        if self.game.get('_local_exists') is False:
            exists = False
        elif self._is_windows and p and p.is_dir():
            exists = any(p.rglob("*.exe")) or any(p.rglob("*.bat")) or any(p.rglob("*.cmd"))
        else:
            exists = p and p.exists() if p else False
                
        self.play_btn.setVisible(bool(exists))
        self.gs_btn.setVisible(bool(exists) and self._is_windows)
        self.un_btn.setVisible(bool(exists))
        self.dl_btn.setVisible(not bool(exists))
        self.dl_btn.setText("⬇ DOWNLOAD")
        self.dl_btn.setStyleSheet("background: #1565c0; color: white; font-weight: bold; padding: 10px; font-size: 13pt;")
        
    def open_game_settings(self):
        from src.ui.dialogs.windows_settings import WindowsGameSettingsDialog
        dlg = WindowsGameSettingsDialog(self.game, self.config, self.main_window, self)
        dlg.show()
        # Keep reference
        self._child_dlg = dlg

    def open_cloud_manager(self):
        from src.ui.dialogs.save_sync import CloudSaveManagerDialog
        dlg = CloudSaveManagerDialog(self.game, self.client, self.config, self.main_window, self)
        dlg.show()
        # Keep reference
        self._child_dlg = dlg
            
    def _start_image_fetch(self):
        url = self.client.get_cover_url(self.game)
        if url:
            self.it = ImageFetcher(self.game['id'], url)
            def _safe_set_pixmap(g, img, _raw=b"", _fmt="", _is_animated=False):
                try:
                    if _is_animated and _raw:
                        self._set_cover_movie(_raw, _fmt)
                        return

                    pixmap = None
                    try:
                        if img and (not img.isNull()):
                            pixmap = QPixmap.fromImage(img)
                    except Exception:
                        pixmap = None

                    if pixmap and not pixmap.isNull():
                        try:
                            mv = getattr(self, "_cover_movie", None)
                            if mv is not None:
                                mv.stop()
                        except Exception:
                            pass
                        self._cover_movie = None
                        self._cover_movie_buffer = None
                        self._cover_full_pixmap = pixmap
                        self._update_cover_pixmap()
                    else:
                        self._render_placeholder()
                except RuntimeError:
                    pass  # Panel was closed before image loaded
            self.it.finished.connect(_safe_set_pixmap)
            self.it.finished.connect(lambda t=self.it: self.main_window.active_threads.remove(t) if t in self.main_window.active_threads else None)
            self.main_window.active_threads.append(self.it)
            self.it.start()
        else:
            self._render_placeholder()

    def _set_cover_movie(self, raw, fmt=""):
        try:
            try:
                if self._cover_movie is not None:
                    self._cover_movie.stop()
            except Exception:
                pass

            qba = QByteArray(raw)
            buf = QBuffer()
            buf.setData(qba)
            buf.open(QIODevice.ReadOnly)

            mv = QMovie(buf)
            if fmt:
                try:
                    mv.setFormat(str(fmt).lower().encode("ascii", errors="ignore"))
                except Exception:
                    pass

            try:
                mv.setCacheMode(QMovie.CacheNone)
            except Exception:
                pass

            try:
                mv.setScaledSize(self.img_label.size())
            except Exception:
                pass

            self._cover_movie = mv
            self._cover_movie_buffer = buf
            self._cover_full_pixmap = None
            self.img_label.setMovie(mv)
            mv.start()
        except Exception:
            self._render_placeholder()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_cover_pixmap()
        self._update_screenshot_pixmaps()
        try:
            mv = getattr(self, "_cover_movie", None)
            if mv is not None:
                mv.setScaledSize(self.img_label.size())
        except Exception:
            pass

    def _update_cover_pixmap(self):
        try:
            if getattr(self, "_cover_movie", None) is not None:
                return
            if not self._cover_full_pixmap or self._cover_full_pixmap.isNull():
                return
            w = self.img_label.width()
            h = self.img_label.height()
            if w <= 0 or h <= 0:
                return
            self.img_label.setPixmap(
                self._cover_full_pixmap.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        except RuntimeError:
            return

    def _render_placeholder(self):
        from PySide6.QtGui import QPainter, QFont
        from PySide6.QtCore import QRect
        w, h = 280, 400
        pixmap = QPixmap(w, h)
        pixmap.fill(QColor("#2a2a3e"))
        painter = QPainter(pixmap)
        painter.setPen(QColor("#8888aa"))
        painter.setFont(QFont("Arial", 16, QFont.Bold))
        painter.drawText(QRect(0, 0, w, h), Qt.AlignCenter, "No Cover")
        painter.end()
        self._cover_full_pixmap = pixmap
        self._update_cover_pixmap()
            
    def _start_desc_fetch(self):
        self.dt = GameDescriptionFetcher(self.client, self.game['id'])
        def _safe_set_text(text):
            try:
                self.desc_label.setText(text)
            except RuntimeError:
                pass
        self.dt.finished.connect(_safe_set_text)
        self.dt.finished.connect(lambda t=self.dt: self.main_window.active_threads.remove(t) if t in self.main_window.active_threads else None)
        self.main_window.active_threads.append(self.dt)
        self.dt.start()

    def _start_metadata_fetch(self):
        self._rom_details_thread = RomDetailsFetcher(self.client, self.game['id'])

        def _safe_apply(rom):
            try:
                self._apply_resolved_metadata(rom or {})
            except RuntimeError:
                pass

        self._rom_details_thread.finished.connect(_safe_apply)
        self._rom_details_thread.finished.connect(
            lambda t=self._rom_details_thread: self.main_window.active_threads.remove(t)
            if t in self.main_window.active_threads else None
        )
        self.main_window.active_threads.append(self._rom_details_thread)
        self._rom_details_thread.start()

    def _resolve_rom_metadata(self, rom):
        md = {}
        try:
            igdb = rom.get("igdb_metadata") or {}
            moby = rom.get("moby_metadata") or {}
            ss = rom.get("ss_metadata") or {}
            lb = (
                rom.get("launchbox_metadata") or rom.get("launchbox") or
                rom.get("lb_metadata") or rom.get("lb") or
                rom.get("launchbox_game") or {}
            )
            if isinstance(igdb, dict):
                md.update(igdb)
            if isinstance(moby, dict):
                md.update(moby)
            if isinstance(ss, dict):
                md.update(ss)
            # LaunchBox should be lowest-priority fallback
            if isinstance(lb, dict):
                for k, v in lb.items():
                    if k not in md or md.get(k) in (None, "", [], {}):
                        md[k] = v
        except Exception:
            md = {}

        title = rom.get("name") or rom.get("title") or md.get("name") or md.get("title")
        summary = (
            rom.get("summary") or rom.get("description") or
            md.get("summary") or md.get("description")
        )

        release = (
            rom.get("release_date") or rom.get("released") or rom.get("first_release_date") or
            md.get("release_date") or md.get("released") or md.get("first_release_date")
        )
        genres_val = (
            rom.get("genres") or md.get("genres") or
            rom.get("genre") or md.get("genre")
        )
        # LaunchBox sometimes includes genres like "Adventure;"
        try:
            if isinstance(genres_val, (list, tuple)):
                cleaned = []
                for g in genres_val:
                    if isinstance(g, str):
                        s = g.strip().rstrip(";").strip()
                        if s:
                            cleaned.append(s)
                    else:
                        cleaned.append(g)
                genres_val = cleaned
            elif isinstance(genres_val, str):
                genres_val = genres_val.strip().rstrip(";").strip()
        except Exception:
            pass

        rating_val = (
            rom.get("total_rating") or md.get("total_rating") or
            rom.get("rating") or md.get("rating") or
            rom.get("aggregated_rating") or md.get("aggregated_rating")
        )
        if rating_val in (None, "", 0, 0.0):
            rating_val = rom.get("community_rating") or md.get("community_rating")

        dev_val = (
            rom.get("developer") or rom.get("developers") or
            md.get("developer") or md.get("developers") or
            rom.get("companies") or md.get("companies")
        )
        pub_val = (
            rom.get("publisher") or rom.get("publishers") or
            md.get("publisher") or md.get("publishers")
        )

        players_val = (
            rom.get("players") or rom.get("num_players") or rom.get("max_players") or rom.get("player_count") or
            md.get("players") or md.get("num_players") or md.get("max_players") or md.get("player_count")
        )
        if not players_val:
            players_val = rom.get("max_players") or md.get("max_players")
        playtime_val = (
            rom.get("playtime_seconds") or rom.get("playtime") or rom.get("play_time") or
            md.get("playtime_seconds") or md.get("playtime") or md.get("play_time")
        )
        if not playtime_val:
            playtime_val = self._get_cached_playtime_seconds(self.game.get("id"))

        # Some backends return "0"/"0.0" strings for missing ratings.
        try:
            if rating_val is not None and str(rating_val).strip() in ("0", "0.0", "0.00"):
                rating_val = None
        except Exception:
            pass

        # Cover URL fallback: LaunchBox provides a list of images
        cover_url = None
        screenshot_urls = []
        try:
            imgs = md.get("images") or rom.get("images")
            if isinstance(imgs, list):
                for i in imgs:
                    if not isinstance(i, dict):
                        continue
                    t = str(i.get("type") or "")
                    u = i.get("url")
                    if not u:
                        continue
                    if "screenshot" in t.lower():
                        screenshot_urls.append(u)

                preferred_types = [
                    "Box - Front",
                    "Box - 3D",
                    "Banner",
                    "Poster",
                    "Clear Logo",
                    "Screenshot - Gameplay",
                ]
                best = None
                for pref in preferred_types:
                    best = next((i for i in imgs if isinstance(i, dict) and (i.get("type") == pref) and i.get("url")), None)
                    if best:
                        break
                if not best:
                    best = next((i for i in imgs if isinstance(i, dict) and i.get("url")), None)
                if best:
                    cover_url = best.get("url")
        except Exception:
            cover_url = None

        try:
            seen = set()
            out = []
            for u in screenshot_urls:
                if not isinstance(u, str):
                    continue
                s = u.strip()
                if not s:
                    continue
                if s in seen:
                    continue
                seen.add(s)
                out.append(s)
                if len(out) >= 20:
                    break
            screenshot_urls = out
        except Exception:
            screenshot_urls = []

        return {
            "title": title,
            "summary": summary,
            "release": release,
            "genres": genres_val,
            "rating": rating_val,
            "developer": dev_val,
            "publisher": pub_val,
            "players": players_val,
            "playtime": playtime_val,
            "cover_url": cover_url,
            "screenshot_urls": screenshot_urls,
        }

    def _apply_resolved_metadata(self, rom):
        resolved = self._resolve_rom_metadata(rom or {})

        try:
            title = resolved.get("title")
            if title and isinstance(title, str) and title.strip():
                self.title_label.setText(title)
                self.game["name"] = title
        except Exception:
            pass

        release_text = self._format_release_date(resolved.get("release"))
        genres_text = self._format_listish(resolved.get("genres"))
        rating_text = self._format_rating_stars(resolved.get("rating"))
        companies_text = self._format_listish(resolved.get("developer"))
        players_text = self._format_players(resolved.get("players"))
        playtime_text = self._format_playtime(resolved.get("playtime"))

        self.release_label.setText(f"<b>Release:</b> {release_text}")
        self.genres_label.setText(f"<b>Genres:</b> {genres_text}")
        self.rating_label.setText(f"<b>Rating:</b> {rating_text}")
        self.companies_label.setText(f"<b>Developer/Publisher:</b> {companies_text}")
        self.players_label.setText(f"<b>Max Players:</b> {players_text}")
        self.playtime_label.setText(f"<b>Playtime:</b> {playtime_text}")

        try:
            self._set_screenshots(resolved.get("screenshot_urls") or [])
        except Exception:
            pass

        try:
            summary = resolved.get("summary")
            if summary and isinstance(summary, str) and summary.strip():
                self.desc_label.setText(summary)
            elif self.desc_label.text().strip() in ("", "Loading description..."):
                self.desc_label.setText("No description available.")
        except Exception:
            pass

        try:
            updated = False
            for k in ("path_cover_large", "path_cover_small", "url_cover"):
                v = rom.get(k)
                if v and not self.game.get(k):
                    self.game[k] = v
                    updated = True
            # LaunchBox cover fallback (only if server didn't provide a RomM cover)
            if not self.game.get("url_cover"):
                lb_cover = resolved.get("cover_url")
                if lb_cover and isinstance(lb_cover, str) and lb_cover.strip():
                    self.game["url_cover"] = lb_cover.strip()
                    updated = True
            if updated and (not self._cover_full_pixmap or self._cover_full_pixmap.isNull()):
                self._start_image_fetch()
        except Exception:
            pass

    def _clear_screenshots(self):
        try:
            for t in getattr(self, "_screenshot_threads", []):
                try:
                    t.requestInterruption()
                except Exception:
                    pass
        except Exception:
            pass

        self._screenshot_threads = []
        self._screenshot_items = []

        try:
            for i in reversed(range(self.screenshots_layout.count())):
                item = self.screenshots_layout.takeAt(i)
                if not item:
                    continue
                w = item.widget()
                if w is not None:
                    try:
                        if w is self.screenshots_empty_label:
                            w.hide()
                            w.setParent(self.screenshots_container)
                            continue
                        w.setParent(None)
                        w.deleteLater()
                    except Exception:
                        pass
        except Exception:
            pass

    def _set_screenshots(self, urls):
        self._clear_screenshots()

        cleaned = []
        try:
            for u in (urls or []):
                if isinstance(u, str) and u.strip():
                    cleaned.append(u.strip())
        except Exception:
            cleaned = []

        if not cleaned:
            try:
                self.screenshots_layout.addWidget(self.screenshots_empty_label)
                self.screenshots_empty_label.show()
            except Exception:
                pass
            return

        try:
            self.screenshots_empty_label.hide()
        except Exception:
            pass

        for u in cleaned:
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setMinimumHeight(120)
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            lbl.setStyleSheet("background: #111; border-radius: 6px;")
            self.screenshots_layout.addWidget(lbl)

            self._screenshot_items.append({"label": lbl, "pixmap": None})

            it = ImageFetcher(self.game['id'], u)

            def _apply_img(_gid, img, _raw=b"", _fmt="", _is_animated=False, target=lbl, url=u):
                try:
                    if not img or img.isNull():
                        return
                    pm = QPixmap.fromImage(img)
                    if pm.isNull():
                        return
                    for entry in self._screenshot_items:
                        if entry.get("label") is target:
                            entry["pixmap"] = pm
                            break
                    self._update_screenshot_pixmaps()
                except Exception:
                    pass


            it.finished.connect(_apply_img)
            it.finished.connect(lambda _g, _img, _raw, _fmt, _is_animated, t=it: self.main_window.active_threads.remove(t) if t in self.main_window.active_threads else None)
            self.main_window.active_threads.append(it)
            self._screenshot_threads.append(it)
            it.start()

        self.screenshots_layout.addStretch(1)

    def _update_screenshot_pixmaps(self):
        try:
            w = max(1, self.screenshots_scroll.viewport().width() - 8)
            for entry in getattr(self, "_screenshot_items", []):
                lbl = entry.get("label")
                pm = entry.get("pixmap")
                if (not lbl) or (not pm) or pm.isNull():
                    continue

                h = max(1, int(w * 9 / 16))
                lbl.setFixedHeight(h)
                lbl.setPixmap(pm.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except RuntimeError:
            return

    def _get_cached_playtime_seconds(self, rom_id):
        if rom_id is None:
            return None
        try:
            cache_path = Path.home() / ".wingosy" / "sync_cache.json"
            if not cache_path.exists():
                return None
            with open(cache_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            entry = data.get(str(rom_id))
            if not isinstance(entry, dict):
                return None
            val = entry.get("playtime_seconds")
            return val
        except Exception:
            return None

    def set_playtime_seconds(self, seconds):
        try:
            self._cached_playtime_seconds = int(seconds)
        except Exception:
            self._cached_playtime_seconds = None

        try:
            txt = self._format_playtime(self._cached_playtime_seconds)
            self.playtime_label.setText(f"<b>Playtime:</b> {txt}")
        except Exception:
            return

    def _format_release_date(self, v):
        if not v:
            return "Unknown"
        try:
            if isinstance(v, (int, float)):
                # Many APIs use unix epoch seconds or ms; disambiguate
                ts = float(v)
                if ts > 10_000_000_000:
                    ts = ts / 1000.0
                import datetime
                return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    return "Unknown"
                # Keep it simple: show ISO date if present
                if "T" in s:
                    s = s.split("T", 1)[0]
                # IGDB sometimes returns epoch as string
                if s.isdigit():
                    return self._format_release_date(int(s))
                return s
            return str(v)
        except Exception:
            return "Unknown"

    def _format_listish(self, v):
        if not v:
            return "Unknown"
        if isinstance(v, str):
            s = v.strip()
            return s if s else "Unknown"
        if isinstance(v, (list, tuple, set)):
            parts = []
            for it in v:
                if isinstance(it, dict):
                    name = it.get("name") or it.get("title")
                    if name:
                        parts.append(str(name))
                elif it:
                    parts.append(str(it))
            return ", ".join(parts) if parts else "Unknown"
        if isinstance(v, dict):
            name = v.get("name") or v.get("title")
            return str(name) if name else "Unknown"
        return str(v)

    def _format_rating_stars(self, v):
        if v is None or v == "":
            return "☆☆☆☆☆"
        try:
            val = float(v)
            if val <= 0:
                return "☆☆☆☆☆"
            # Normalize various scales to 0..5
            if val <= 5:
                stars = val
            elif val <= 10:
                stars = val / 2.0
            elif val <= 100:
                stars = val / 20.0
            else:
                stars = 5.0
            stars = max(0.0, min(5.0, stars))
            full = int(round(stars))
            return "".join(["★" if i < full else "☆" for i in range(5)])
        except Exception:
            return "☆☆☆☆☆"

    def _format_players(self, v):
        if not v:
            return "Unknown"
        try:
            if isinstance(v, (int, float)):
                n = int(v)
                if n <= 0:
                    return "Unknown"
                if n == 1:
                    return "Single-Player"
                return str(n)
            if isinstance(v, str):
                s = v.strip()
                if s == "1":
                    return "Single-Player"
                return s if s else "Unknown"
            if isinstance(v, dict):
                mn = v.get("min") or v.get("min_players")
                mx = v.get("max") or v.get("max_players")
                if mn and mx:
                    try:
                        if int(mn) == 1 and int(mx) == 1:
                            return "Single-Player"
                    except Exception:
                        pass
                    return f"{mn}-{mx}"
                if mx:
                    try:
                        if int(mx) == 1:
                            return "Single-Player"
                    except Exception:
                        pass
                    return str(mx)
            return str(v)
        except Exception:
            return "Unknown"

    def _format_playtime(self, v):
        if not v:
            return "Not Yet Played"
        try:
            secs = None
            if isinstance(v, (int, float)):
                secs = float(v)
                if secs <= 0:
                    return "Not Yet Played"
                # If it's too small, it might actually be minutes
                if 0 < secs < 1000:
                    # Heuristic: treat as minutes if not an obvious seconds counter
                    # (we'll still show it sensibly)
                    pass
            elif isinstance(v, str):
                s = v.strip()
                if not s:
                    return "Not Yet Played"
                if s.isdigit():
                    secs = float(int(s))
                    if secs <= 0:
                        return "Not Yet Played"
                else:
                    return s
            if secs is None:
                return str(v)

            # If looks like minutes (common in some APIs) convert
            if secs < 10_000 and secs % 60 != 0 and secs < 600:
                # leave as seconds
                pass

            total_seconds = int(secs)
            if total_seconds < 60:
                return f"{total_seconds}s"
            mins = total_seconds // 60
            if mins < 60:
                return f"{mins}m"
            hrs = mins // 60
            rem_m = mins % 60
            if rem_m == 0:
                return f"{hrs}h"
            return f"{hrs}h {rem_m}m"
        except Exception:
            return "Not Yet Played"
        
    def uninstall_game(self):
        if self._uninstall_dialog_open:
            return

        self._uninstall_dialog_open = True
        self.un_btn.setEnabled(False)

        msg = f"Are you sure you want to delete {self.game.get('name')}?"
        if self._is_windows:
            msg = f"Permanently delete ALL files in:\n{self._local_rom_path}?"
            
        try:
            dlg = UninstallConfirmDialog(
                "Uninstall — Wingosy",
                msg,
                parent=self,
            )
            if dlg.exec() == QDialog.Accepted:
                try:
                    p = self._local_rom_path
                    if p.exists():
                        if p.is_dir():
                            shutil.rmtree(p)
                        else:
                            os.remove(p)
                        self.main_window.log(f"🗑 {self.game.get('name')} uninstalled")
                        self.game['_local_exists'] = False
                        self._update_button_states()
                        try:
                            self.refresh_badges_row()
                        except Exception:
                            pass
                        try:
                            if hasattr(self.main_window, 'library_tab') and self.main_window.library_tab:
                                self.main_window.library_tab.update_game_local_status(self.game.get('id'), False)
                        except Exception:
                            pass
                        self.main_window.library_tab.apply_filters()
                except Exception as e:
                    StyledMessageBox.critical(self, "Error — Wingosy", str(e))
        finally:
            self._uninstall_dialog_open = False
            try:
                self.un_btn.setEnabled(True)
            except RuntimeError:
                pass
                
    def _on_download_clicked(self):
        # Prevent duplicate downloads
        rom_id = str(self.game["id"])
        existing = download_registry.get(rom_id)
        if existing and existing.get("status") in ("downloading", "extracting"):
            return  # Already in progress, ignore click

        windows_dir = self.config.get("windows_games_dir", "")
        if self._is_windows and not windows_dir:
            directory = QFileDialog.getExistingDirectory(self, "Select Windows Games Folder")
            if directory:
                self.config.set("windows_games_dir", directory)
                windows_dir = directory
            else:
                return

        files = self.game.get('files', [])
        if not files:
            return

        file_obj = files[0]
        rom_name = file_obj.get("file_name", "")

        # Windows-specific pre-download checks
        if self._is_windows and windows_dir:
            archive_path = Path(windows_dir) / rom_name
            extracted_dir = Path(windows_dir) / Path(rom_name).stem

            # 1. Check if already installed
            if extracted_dir.exists() and any(extracted_dir.rglob("*.exe")):
                StyledMessageBox.information(
                    self, "Already Installed — Wingosy",
                    f"{self.game['name']} appears to already be installed at:\n{extracted_dir}\n\nUse the Play button to launch it."
                )
                self._update_button_states()
                return

            # 2. Check if archive exists
            if archive_path.exists():
                reply = StyledMessageBox.question(
                    self, "Archive Already Downloaded — Wingosy",
                    f"{rom_name} already exists in your Windows Games folder.\n\nWould you like to extract it now instead of downloading again?",
                    StyledMessageBox.Yes | StyledMessageBox.No | StyledMessageBox.Cancel,
                    StyledMessageBox.Yes
                )
                if reply == StyledMessageBox.Cancel:
                    return
                if reply == StyledMessageBox.Yes:
                    self._start_extraction(str(archive_path))
                    return

        self.download_rom(file_obj)

    def _start_extraction(self, path):
        target_dir = Path(path).parent
        if self._is_windows:
            target_dir = target_dir / Path(path).stem

        rom_id = str(self.game['id'])
        self.extract_thread = ExtractionThread(path, str(target_dir), rom_id=rom_id)
        download_registry.register_extraction(self.game['id'], self.game['name'], self.extract_thread)

        self.extract_thread.progress.connect(lambda d, t: download_registry.update_progress(self.game['id'], d, t))
        self.extract_thread.finished.connect(self._on_extraction_finished)
        self.extract_thread.error.connect(lambda msg: StyledMessageBox.critical(self, "Extraction Error", msg))

        self.main_window.download_queue.add_download(self.game['name'], self.extract_thread, "extraction", self.game['id'])
        self.extract_thread.start()
        self._reconnect_active_download()

    def _do_blocking_pull(self, rom, emulator):
        """Pull latest save from RomM before launching. Returns False to abort launch."""
        try:
            if not self.config.get("auto_pull_saves", True):
                return True

            strategy = get_strategy(self.config, emulator)
            strategy.set_session_context(start_time=time.time(), rom_path=str(self._local_rom_path) if self._local_rom_path else "")
            save_dir = strategy.get_save_dir(rom)

            latest = self.client.get_latest_save(rom['id'])
            if not latest:
                return True

            is_folder = (strategy.mode_id in ["folder", "windows"])
            local_path = str(save_dir) if save_dir else (
                str(strategy.get_save_files(rom)[0]) if strategy.get_save_files(rom) else None
            )

            if not local_path:
                return True

            # Check if a conflict will be triggered: local exists + hashes differ
            import zipfile
            from src.utils import calculate_zip_content_hash, calculate_file_hash, calculate_folder_hash
            import tempfile

            watcher = self.main_window.watcher
            server_updated_at = latest.get('updated_at', '')
            cached_val = watcher.sync_cache.get(str(rom['id']), {})
            cached_ts = cached_val.get('save_updated_at', '') if isinstance(cached_val, dict) else ""

            # If cache matches server, no conflict possible
            if cached_ts == server_updated_at and os.path.exists(local_path):
                return True

            local_exists = os.path.isdir(local_path) if is_folder else os.path.exists(local_path)
            if not local_exists:
                # No local save — safe to pull normally
                return self._apply_save_blocking(
                    rom['id'], rom['name'], latest, local_path, "save", is_folder
                ) is not False

            # Local exists — download cloud save to temp and compare hashes
            tmp = tempfile.mktemp(suffix=".save")
            if not watcher.client.download_save(latest, tmp):
                return True

            try:
                remote_h = calculate_zip_content_hash(tmp) if zipfile.is_zipfile(tmp) else calculate_file_hash(tmp)
                local_h = calculate_folder_hash(local_path) if is_folder else calculate_file_hash(local_path)

                if remote_h == local_h:
                    # Identical — update cache and proceed
                    watcher.sync_cache[str(rom['id'])] = {"save_updated_at": server_updated_at}
                    watcher.save_cache()
                    return True

                # Hashes differ — show conflict dialog and BLOCK launch until resolved
                from src.ui.dialogs.save_sync import ConflictDialog
                from PySide6.QtCore import QEventLoop

                result = {"choice": None}
                loop = QEventLoop()

                def on_conflict_resolved(choice):
                    result["choice"] = choice
                    loop.quit()

                dlg = ConflictDialog(rom['name'], self)
                dlg.choice_made.connect(on_conflict_resolved)
                dlg.show()
                loop.exec()  # Block here until user picks

                choice = result["choice"]
                if choice == "cloud":
                    return self._apply_save_blocking(
                        rom['id'], rom['name'], latest, local_path, "save", is_folder
                    ) is not False
                elif choice == "local":
                    return True  # Keep local, proceed to launch
                elif choice == "both":
                    # Backup cloud to .cloud_backup then proceed with local
                    cloud_bak = str(local_path) + ".cloud_backup"
                    if os.path.exists(cloud_bak):
                        if os.path.isdir(cloud_bak): shutil.rmtree(cloud_bak, ignore_errors=True)
                        else: os.remove(cloud_bak)
                    shutil.copy2(tmp, cloud_bak) if not os.path.isdir(tmp) else shutil.copytree(tmp, cloud_bak)
                    self.main_window.log(f"📁 Cloud save backed up to: {cloud_bak}")
                    return True
                else:
                    return False  # User closed dialog — abort launch
            finally:
                if os.path.exists(tmp):
                    try: os.remove(tmp) if not os.path.isdir(tmp) else shutil.rmtree(tmp, ignore_errors=True)
                    except: pass

        except Exception as e:
            logging.warning(f"[Sync] Pull failed: {e}")
            return True

    def _apply_save_blocking(self, rom_id, title, obj, local_path, file_type, is_folder=False):
        import tempfile
        watcher = self.main_window.watcher
        server_updated_at = obj.get('updated_at', '')
        local_exists = os.path.isdir(local_path) if is_folder else os.path.exists(local_path)
        
        cached_entry = watcher.sync_cache.get(str(rom_id), {})
        if isinstance(cached_entry, dict):
            cached_ts = cached_entry.get(f'{file_type}_updated_at', '')
        else:
            cached_ts = cached_entry if file_type == 'save' else ''
            
        if cached_ts == server_updated_at and local_exists:
            return True
            
        tmp = tempfile.mktemp(suffix=f".{file_type}")
        success = watcher.client.download_state(obj, tmp) if file_type == "state" else watcher.client.download_save(obj, tmp)
        if not success:
            return True
            
        dest = Path(local_path)
        if is_folder:
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            
        if dest.exists():
            bak = Path(str(dest) + ".bak")
            try:
                if is_folder:
                    shutil.copytree(str(dest), str(bak), dirs_exist_ok=True)
                else:
                    shutil.copy2(str(dest), str(bak))
            except:
                pass
                
        try:
            if is_folder or (zipfile.is_zipfile(tmp) and not local_path.endswith(('.srm', '.state'))):
                extract_strip_root(tmp, local_path)
            else:
                shutil.copy2(tmp, str(dest))
                if file_type == "state" and dest.suffix == '.state' and not dest.name.endswith('.state.auto'):
                    auto_path = dest.with_name(dest.name + '.auto')
                    if auto_path.exists():
                        if auto_path.is_dir(): shutil.rmtree(auto_path)
                        else: auto_path.unlink()
                    dest.rename(auto_path)
                    
            if not isinstance(watcher.sync_cache.get(str(rom_id)), dict):
                watcher.sync_cache[str(rom_id)] = {}
            watcher.sync_cache[str(rom_id)][f'{file_type}_updated_at'] = server_updated_at
            watcher.save_cache()
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return True

    def play_game(self):
        local_rom = self._get_local_rom_path()
        if not local_rom or not local_rom.exists():
            StyledMessageBox.warning(self, "Error — Wingosy", "Could not find the local ROM file. Please download it first.")
            return
            
        emu_data = None
        platform = self.game.get('platform_slug')
        all_emus = emulators.load_emulators()
        
        assigned_id = self.config.get("platform_assignments", {}).get(platform)
        if assigned_id:
            emu_data = next((e for e in all_emus if e["id"] == assigned_id), None)
            
        if not emu_data:
            emu_data = emulators.get_emulator_for_platform(platform)
            
        if not emu_data:
            emu_data = next((e for e in all_emus if e["id"] == "retroarch"), None)

        # Warn if multiple emulators support this platform and none is explicitly assigned
        if emu_data and not self.config.get("platform_assignments", {}).get(platform):
            all_matching = [
                e for e in all_emus
                if platform in e.get("platform_slugs", [])
                and e.get("executable_path")
                and os.path.exists(e.get("executable_path", ""))
            ]
            if len(all_matching) > 1:
                names = ", ".join(e["name"] for e in all_matching)
                self.main_window.log(
                    f"⚠️ Multiple emulators support {platform.upper()}: {names}. "
                    f"Using {emu_data['name']}. Set a preferred emulator in Settings → Platform Assignments."
                )

        if not emu_data or (not emu_data.get("is_native") and (not emu_data.get("executable_path") or not os.path.exists(emu_data["executable_path"]))):
            StyledMessageBox.warning(self, "Error — Wingosy", "No valid emulator configured.")
            return
            
        self.main_window.log(f"🎮 Preparing {self.game.get('name')}...")
        self.main_window.ensure_watcher_running()
        
        # 1. Sync Before Play
        if self.config.get("auto_pull_saves", True):
            if not self._do_blocking_pull(self.game, emu_data):
                return

        # 2. Launch
        try:
            exe_path = os.path.normpath(emu_data.get("executable_path") or "")
            
            if emu_data.get("is_native"):
                saved = windows_saves.get_windows_save(self.game['id'])
                exe_to_launch = saved.get("default_exe") if saved else None
                if not exe_to_launch:
                    # Fallback to auto-detect logic
                    rom = self.game.get('fs_name')
                    win_dir = self.config.get("windows_games_dir")
                    if rom and win_dir:
                        folder = Path(win_dir) / Path(rom).stem
                        if folder.exists():
                            exes = []
                            for pattern in ("*.exe", "*.bat", "*.cmd"):
                                exes.extend(
                                    str(p) for p in folder.rglob(pattern)
                                    if not any(ex_name.lower() in str(p).lower() for ex_name in EXCLUDED_EXES)
                                )
                            if len(exes) == 1:
                                exe_to_launch = exes[0]
                            elif len(exes) > 1:
                                from src.ui.dialogs.emulator_editor import ExePickerDialog
                                picker = ExePickerDialog(exes, self.game.get("name"), self)
                                picker.exe_selected.connect(self._launch_windows_exe)
                                picker.show()
                                # Keep reference
                                self._child_dlg = picker
                                return # Launching happens after picking
                
                if not exe_to_launch:
                    StyledMessageBox.warning(self, "Error — Wingosy", "No game executable found.")
                    return

                self._launch_windows_exe(exe_to_launch)
                return

            if emu_data["id"] == "retroarch":
                check_retroarch_autosave(exe_path, platform, self, self.config)
                from src.platforms import RETROARCH_CORES
                core_name = RETROARCH_CORES.get(platform)
                if core_name:
                    core_path = Path(exe_path).parent / "cores" / core_name
                    if core_path.exists():
                        args = [exe_path, "-L", str(core_path), str(local_rom)]
                    else:
                        if StyledMessageBox.question(self, "Error — Wingosy", f"Core {core_name} missing. Download?") == StyledMessageBox.Yes:
                            self.start_core_download(core_name, Path(exe_path).parent, platform)
                        return
                else:
                    args = [exe_path, str(local_rom)]
            else:
                raw_args = emu_data.get("launch_args", ["{rom_path}"])
                args = [exe_path]
                for a in raw_args:
                    if a.replace("{rom_path}", str(local_rom)) != exe_path:
                        args.append(a.replace("{rom_path}", str(local_rom)))
            
            clean_env = os.environ.copy()
            for k in ["QT_QPA_PLATFORM_PLUGIN_PATH", "QT_PLUGIN_PATH", "QT_QPA_FONTDIR", "QT_QPA_PLATFORM", "QT_STYLE_OVERRIDE"]:
                clean_env.pop(k, None)
                
            proc = subprocess.Popen(args, env=clean_env, cwd=str(Path(exe_path).parent))
            self.main_window.log(f"🚀 Launched {emu_data['name']} (PID: {proc.pid})")
            if self.main_window.watcher:
                QTimer.singleShot(0, lambda: self.main_window.watcher.track_session(proc, emu_data["name"], self.game, str(local_rom), exe_path, skip_pull=True))
        except Exception as e:
            StyledMessageBox.critical(self, "Error — Wingosy", str(e))

    def _launch_windows_exe(self, exe_path):
        self.main_window.log(f"🚀 Launching Windows Game: {os.path.basename(exe_path)}")
        ext = os.path.splitext(exe_path)[1].lower()
        if ext in (".bat", ".cmd"):
            proc = subprocess.Popen(["cmd.exe", "/c", exe_path], cwd=os.path.dirname(exe_path))
        else:
            proc = subprocess.Popen([exe_path], cwd=os.path.dirname(exe_path))
        if self.main_window.watcher:
            self.main_window.watcher.track_session(proc, "Windows", self.game, exe_path, exe_path, skip_pull=True)

            
    def start_core_download(self, core_name, emu_dir, platform):
        from src.ui.threads import CoreDownloadThread
        dlg = QDialog(self) # Still modal for core DL
        dlg.setWindowTitle(f"Downloading {core_name} — Wingosy")
        dlg.setFixedSize(350, 100)
        l = QVBoxLayout(dlg)
        status = QLabel(f"Downloading for {platform}...")
        pb = QProgressBar()
        l.addWidget(status)
        l.addWidget(pb)
        dlg.setWindowModality(Qt.ApplicationModal)
        
        t = CoreDownloadThread(core_name, emu_dir / "cores")
        t.progress.connect(lambda v, s: (pb.setValue(v), status.setText(f"Speed: {format_speed(s)}")))
        t.finished.connect(lambda success, msg: (dlg.close(), self.play_game() if success else StyledMessageBox.critical(self, "Error — Wingosy", msg)))
        t.start()
        dlg.exec()

GameDetailDialog = GameDetailPanel

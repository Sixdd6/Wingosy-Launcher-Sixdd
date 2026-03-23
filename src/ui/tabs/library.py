import os
import re
import logging
import ctypes
import sys
import time
from pathlib import Path
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,   
                             QPushButton, QLineEdit, QScrollArea, QGridLayout,
                             QComboBox, QSizePolicy, QAbstractItemView, QGraphicsDropShadowEffect, QStackedWidget)
from PySide6.QtCore import Qt, Signal, QTimer, QEvent, QPropertyAnimation, QEasingCurve, QThread, Slot
from PySide6.QtGui import QPixmap, QImage, QColor, QFontMetrics, QPainter
from PySide6.QtSvg import QSvgRenderer

from src.ui.threads import ImageFetcher
from src.ui.widgets import format_speed, get_resource_path
from src.platforms import RETROARCH_PLATFORMS, platform_matches
from src import emulators, download_registry

CLOUD_BADGE_PROBE_TTL_SECONDS = 300
CLOUD_BADGE_PROBE_FAILURE_TTL_SECONDS = 60
CLOUD_BADGE_PROBE_MAX_IDS_PER_PASS = 80
CLOUD_BADGE_PROBE_STATUS_TEXT = "Checking for cloud save updates..."


def _entry_has_cloud_save(entry):
    if not isinstance(entry, dict):
        return False
    explicit = entry.get("has_cloud_save")
    if explicit is not None:
        return bool(explicit)
    return bool(
        entry.get('save_updated_at') or entry.get('save_mtime') or
        entry.get('state_updated_at') or entry.get('state_mtime')
    )

CONTROLLER_MAPS = {
    "xinput": {
        "confirm":  0x1000,  # A button
        "back":     0x2000,  # B button
        "up":       0x0001,  # DPAD UP
        "down":     0x0002,  # DPAD DOWN
        "left":     0x0004,  # DPAD LEFT
        "right":    0x0008,  # DPAD RIGHT
        "stick_deadzone": 8000,
    },
    "ps4": {
        "confirm":  0x1000,  # ✕ (Cross)
        "back":     0x2000,  # ○ (Circle)
        "up":       0x0001,
        "down":     0x0002,
        "left":     0x0004,
        "right":    0x0008,
        "stick_deadzone": 8000,
    },
    "ps5": {
        "confirm":  0x1000,  # ✕ (Cross)
        "back":     0x2000,  # ○ (Circle)
        "up":       0x0001,
        "down":     0x0002,
        "left":     0x0004,
        "right":    0x0008,
        "stick_deadzone": 8000,
    },
    "switch": {
        "confirm":  0x1000,  # A (right)
        "back":     0x2000,  # B (bottom)
        "up":       0x0001,
        "down":     0x0002,
        "left":     0x0004,
        "right":    0x0008,
        "stick_deadzone": 8000,
    },
    "generic": {
        "confirm":  0x1000,
        "back":     0x2000,
        "up":       0x0001,
        "down":     0x0002,
        "left":     0x0004,
        "right":    0x0008,
        "stick_deadzone": 10000,
    },
}

class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]

class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_ulong),
        ("Gamepad", XINPUT_GAMEPAD),
    ]

class SmoothScrollArea(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scroll_animation = QPropertyAnimation(self.verticalScrollBar(), b"value")
        self._scroll_animation.setDuration(180)
        self._scroll_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._target_value = 0

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        step = 120  # pixels per scroll tick
        self._target_value = max(
            self.verticalScrollBar().minimum(),
            min(
                self.verticalScrollBar().maximum(),
                (self._target_value if self._scroll_animation.state() == QPropertyAnimation.Running else self.verticalScrollBar().value()) - (delta / 120 * step)
            )
        )
        self._scroll_animation.stop()
        self._scroll_animation.setStartValue(self.verticalScrollBar().value())
        self._scroll_animation.setEndValue(int(self._target_value))
        self._scroll_animation.start()

class CloudSaveProbeThread(QThread):
    probed = Signal(int, object)

    def __init__(self, client, rom_ids):
        super().__init__()
        self.client = client
        self.rom_ids = list(rom_ids or [])

    def run(self):
        for rid in self.rom_ids:
            if self.isInterruptionRequested():
                return
            try:
                latest_save = None
                latest_state = None
                try:
                    latest_save = self.client.get_latest_save(rid)
                except Exception:
                    latest_save = None
                try:
                    latest_state = self.client.get_latest_state(rid)
                except Exception:
                    latest_state = None

                payload = {
                    "has_cloud_save": bool(
                        isinstance(latest_save, dict) or isinstance(latest_state, dict)
                    ),
                    "save_updated_at": latest_save.get('updated_at') if isinstance(latest_save, dict) else None,
                    "state_updated_at": latest_state.get('updated_at') if isinstance(latest_state, dict) else None,
                }
                self.probed.emit(int(rid), payload)
                time.sleep(0.02)
            except Exception:
                try:
                    self.probed.emit(int(rid), None)
                except Exception:
                    pass

class GameCard(QWidget):
    clicked = Signal(object)
    def __init__(self, game, client, config, sync_cache):
        super().__init__()
        self.game, self.client, self.config, self.sync_cache = game, client, config, sync_cache
        self._selected = False
        self._badge_render_state = {}
        self._badge_style_radius = None
        
        self.update_style()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(4)

        self._cover_title_spacing = 12
        self._badge_layout_force = False
        self._badge_layout_timer = QTimer(self)
        self._badge_layout_timer.setSingleShot(True)
        self._badge_layout_timer.setInterval(0)
        self._badge_layout_timer.timeout.connect(self._flush_badge_layout)

        self.img_label = QLabel()
        self.img_label.setAlignment(Qt.AlignCenter)
        self.img_label.installEventFilter(self)
        layout.addWidget(self.img_label)
        layout.addSpacing(self._cover_title_spacing)

        self._apply_pixmap_retries = 0

        # State Indicators
        rom_exists = game.get('_local_exists', False)

        if rom_exists:
            self.local_indicator = QLabel(self)
            self.local_indicator.setStyleSheet("background-color: #4caf50; border-radius: 7px;")
            self.local_indicator.setAlignment(Qt.AlignCenter)
            self.local_indicator.setProperty("_badge_svg", "assets/library-svgrepo-com.svg")
            self.local_indicator.setToolTip("Installed")
            self.local_indicator.show()

        self.refresh_cloud_indicator(sync_cache)
        self._update_badge_layout(force=True)

        self.title_label = QLabel()
        self.title_label.setFixedHeight(30)
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setStyleSheet("color: white; font-weight: bold; border: none;")
        self.title_label.setWordWrap(False)
        self.title_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        
        display_name = game.get('name', 'Unknown')
        fs_name = game.get('fs_name', '')
        disc_match = re.search(r'\((disc|disk|cd)\s*(\d+)\)', fs_name, re.IGNORECASE)
        if disc_match:
            disc_num = disc_match.group(2)
            display_name = f"[D{disc_num}] {display_name}"
            
        self._full_title = display_name
        self.title_label.setText(display_name)
        self.title_label.setToolTip(display_name)
        layout.addWidget(self.title_label)
        self.disc_label = None
        self.fetcher = None
        self._full_image = None
        self._full_pixmap = None
        self._scaled_pixmap_cache = {}

        # Listen to registry for downloads/extractions
        download_registry.add_listener(str(self.game['id']), self.on_registry_update)

    def refresh_cloud_indicator(self, sync_cache):
        try:
            rom_id = str(self.game.get('id', ''))
            entry = sync_cache.get(rom_id, {}) if isinstance(sync_cache, dict) else {}
            has_cloud_save = _entry_has_cloud_save(entry)

            if has_cloud_save:
                if not hasattr(self, 'cloud_indicator'):
                    self.cloud_indicator = QLabel(self)
                    self.cloud_indicator.setStyleSheet("background-color: #1565c0; border-radius: 7px;")
                    self.cloud_indicator.setAlignment(Qt.AlignCenter)
                    self.cloud_indicator.setProperty("_badge_svg", "assets/save-floppy-svgrepo-com.svg")
                    self.cloud_indicator.setToolTip("Cloud save available")
                self.cloud_indicator.show()
            elif hasattr(self, 'cloud_indicator'):
                self.cloud_indicator.hide()
            self._update_badge_layout()
        except Exception:
            return

    def update_title_width(self, available_width):
        try:
            max_w = max(1, int(available_width))
            self.title_label.setMaximumWidth(max_w)
            self.title_label.setFixedWidth(max_w)
            fm = QFontMetrics(self.title_label.font())
            full = getattr(self, "_full_title", "")
            self.title_label.setText(fm.elidedText(full, Qt.ElideRight, max_w))
        except Exception:
            return

    def on_registry_update(self, rom_id, rtype, current, total, speed=0):
        if rtype == "done":
            download_registry.remove_listener(str(self.game['id']), self.on_registry_update)
            self.set_local_exists(True)
            QTimer.singleShot(0, self._refresh_parent_tab)
        elif rtype == "cancelled":
            download_registry.remove_listener(str(self.game['id']), self.on_registry_update)
            self.set_local_exists(False)
            QTimer.singleShot(0, self._refresh_parent_tab)

    def _refresh_parent_tab(self):
        # Traverse up to find LibraryTab
        p = self.parent()
        while p and not isinstance(p, LibraryTab):
            p = p.parent()
        if p:
            p.apply_filters()

    def set_local_exists(self, exists):
        """Dynamically add or remove the local ROM checkmark."""
        self.game['_local_exists'] = exists
        if exists:
            if not hasattr(self, 'local_indicator'):
                self.local_indicator = QLabel(self)
                self.local_indicator.setStyleSheet("background-color: #4caf50; border-radius: 7px;")
                self.local_indicator.setAlignment(Qt.AlignCenter)
                self.local_indicator.setProperty("_badge_svg", "assets/library-svgrepo-com.svg")
                self.local_indicator.setToolTip("Installed")
            self.local_indicator.show()
        elif hasattr(self, 'local_indicator'):
            self.local_indicator.hide()
        self._update_badge_layout()

    def resizeEvent(self, event):
        try:
            self._schedule_badge_layout()
        except Exception:
            pass
        super().resizeEvent(event)

    def showEvent(self, event):
        try:
            self._schedule_badge_layout(force=True)
        except Exception:
            pass
        super().showEvent(event)

    def eventFilter(self, obj, event):
        try:
            if obj is self.img_label and event.type() in (
                QEvent.Type.Resize,
                QEvent.Type.Move,
                QEvent.Type.Show,
            ):
                self._schedule_badge_layout()
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def _schedule_badge_layout(self, force=False):
        try:
            self._badge_layout_force = bool(self._badge_layout_force or force)
            if not self._badge_layout_timer.isActive():
                self._badge_layout_timer.start()
        except Exception:
            try:
                self._update_badge_layout(force=force)
            except Exception:
                pass

    def _flush_badge_layout(self):
        try:
            force = bool(self._badge_layout_force)
            self._badge_layout_force = False
            self._update_badge_layout(force=force)
        except Exception:
            return

    def _compute_badge_metrics(self):
        try:
            base = int(min(self.width(), self.img_label.width() or self.width()) * 0.11)
        except Exception:
            base = 16

        badge_px = max(16, min(24, base))
        icon_px = max(12, badge_px - 6)
        pad_px = max(3, min(6, badge_px // 4))
        gap_px = max(3, min(8, pad_px + 2))
        return badge_px, icon_px, pad_px, gap_px

    def _update_badge_layout(self, force=False):
        badge_px, icon_px, pad_px, gap_px = self._compute_badge_metrics()
        x = pad_px
        y = pad_px

        try:
            label_x = int(self.img_label.x())
            label_y = int(self.img_label.y())
            label_w = int(self.img_label.width() or 0)
            label_h = int(self.img_label.height() or 0)

            pix = self.img_label.pixmap()
            if pix and (not pix.isNull()) and label_w > 0 and label_h > 0:
                try:
                    dpr = float(pix.devicePixelRatio()) or 1.0
                except Exception:
                    dpr = 1.0
                pix_w = max(1, int(round(pix.width() / dpr)))
                pix_h = max(1, int(round(pix.height() / dpr)))
                draw_w = min(label_w, pix_w)
                draw_h = min(label_h, pix_h)
                x = label_x + max(0, (label_w - draw_w) // 2) + pad_px
                y = label_y + max(0, (label_h - draw_h) // 2) + pad_px
            else:
                x = label_x + pad_px
                y = label_y + pad_px
        except Exception:
            x = pad_px
            y = pad_px

        # Installed badge
        if hasattr(self, 'local_indicator'):
            try:
                if self.local_indicator.isVisible():
                    self._apply_badge_geometry(self.local_indicator, x, y, badge_px, force=force)
                    self._apply_badge_icon(self.local_indicator, icon_px, force=force)
                    x += badge_px + gap_px
            except Exception:
                pass

        # Cloud badge
        if hasattr(self, 'cloud_indicator'):
            try:
                if self.cloud_indicator.isVisible():
                    self._apply_badge_geometry(self.cloud_indicator, x, y, badge_px, force=force)
                    self._apply_badge_icon(self.cloud_indicator, icon_px, force=force)
            except Exception:
                pass

        # Update rounded corners to match computed size
        try:
            r = max(6, badge_px // 2)
            if force or (self._badge_style_radius != r):
                if hasattr(self, 'local_indicator'):
                    self.local_indicator.setStyleSheet(f"background-color: #4caf50; border-radius: {r}px;")
                if hasattr(self, 'cloud_indicator'):
                    self.cloud_indicator.setStyleSheet(f"background-color: #1565c0; border-radius: {r}px;")
                self._badge_style_radius = r
        except Exception:
            pass

    def _apply_badge_geometry(self, w, x, y, size, force=False):
        try:
            xi = int(x)
            yi = int(y)
            si = int(size)
            target = (xi, yi, si)
            if (not force) and w.property("_badge_geo") == target:
                return
            if w.width() != si or w.height() != si:
                w.setFixedSize(si, si)
            if w.x() != xi or w.y() != yi:
                w.move(xi, yi)
            w.setProperty("_badge_geo", target)
        except Exception:
            return

    def _apply_badge_icon(self, badge_label, icon_px, force=False):
        try:
            rel_svg = badge_label.property("_badge_svg")
            if not rel_svg:
                return
            dpr = 1.0
            try:
                dpr = float(self.devicePixelRatioF())
            except Exception:
                pass

            key = (rel_svg, int(icon_px), float(dpr))
            if (not force) and self._badge_render_state.get(badge_label) == key:
                return

            pm = self._render_svg_badge(rel_svg, int(icon_px), int(icon_px), dpr=dpr)
            if not pm.isNull():
                badge_label.setPixmap(pm)
                self._badge_render_state[badge_label] = key
        except Exception:
            return

    def _render_svg_badge(self, relative_svg_path, w, h, dpr=1.0):
        try:
            svg_path = get_resource_path(relative_svg_path)
            renderer = QSvgRenderer(svg_path)
            if not renderer.isValid():
                return QPixmap()
            try:
                dpr = float(dpr)
            except Exception:
                dpr = 1.0
            pw = max(1, int(round(float(w) * dpr)))
            ph = max(1, int(round(float(h) * dpr)))
            pm = QPixmap(pw, ph)
            pm.fill(Qt.transparent)
            try:
                pm.setDevicePixelRatio(dpr)
            except Exception:
                pass
            painter = QPainter(pm)
            try:
                renderer.render(painter)
            finally:
                painter.end()
            return pm
        except Exception:
            return QPixmap()

    def set_selected(self, selected):
        self._selected = selected
        if selected:
            shadow = QGraphicsDropShadowEffect(self)
            shadow.setBlurRadius(20)
            shadow.setColor(QColor(13, 110, 253, 150))
            shadow.setOffset(0, 0)
            self.setGraphicsEffect(shadow)
        else:
            self.setGraphicsEffect(None)
        self.update_style()

    def update_style(self):
        border = "2px solid #0d6efd" if self._selected else "none"
        bg = "#2c2c2c" if self._selected else "#1e1e1e"
        self.setStyleSheet(f"""
            GameCard {{ 
                background: {bg}; 
                border-radius: 8px; 
                border: {border};
            }}
            GameCard:hover {{ background: #2c2c2c; border: 2px solid #1565c0; }}
        """)

    def start_image_fetch(self, main_window, generation):
        url = self.client.get_cover_url(self.game)
        if not url:
            w = self.img_label.width() or 150
            h = self.img_label.height() or 200
            pixmap = QPixmap(w, h)
            pixmap.fill(QColor("#2a2a3e"))
            self._full_pixmap = pixmap
            try:
                self._scaled_pixmap_cache.clear()
            except Exception:
                pass
            self.img_label.setPixmap(pixmap)
            try:
                self._schedule_badge_layout(force=True)
            except Exception:
                pass
            return None
        self.fetcher = ImageFetcher(self.game['id'], url)
        fetcher = self.fetcher
        self.fetcher.finished.connect(self.set_image)
        self.fetcher.finished.connect(lambda _gid, _img, _raw, _fmt, _is_animated, f=fetcher: main_window._on_image_fetched(f, generation))
        self.fetcher.start()
        return self.fetcher

    def set_image(self, game_id, image, _raw=b"", _fmt="", _is_animated=False):
        try:
            if (not image) or image.isNull():
                w = self.img_label.width() or 150
                h = self.img_label.height() or 200
                ph = QPixmap(w, h)
                ph.fill(QColor("#2a2a3e"))
                self._full_image = None
                self._full_pixmap = ph
                self._scaled_pixmap_cache.clear()
                self.img_label.setPixmap(ph)
                try:
                    self._schedule_badge_layout(force=True)
                except Exception:
                    pass
            else:
                self._full_image = image
                self._full_pixmap = None
                self._scaled_pixmap_cache.clear()
                self._apply_pixmap_retries = 0
                p = self.parent()
                while p and not isinstance(p, LibraryTab):
                    p = p.parent()
                if p:
                    p._enqueue_cover_apply(self)
                else:
                    self._apply_full_pixmap_to_label()
        except Exception:
            return

    def _apply_full_pixmap_to_label(self):
        try:
            pm = getattr(self, "_full_pixmap", None)
            if (not pm) or pm.isNull():
                img = getattr(self, "_full_image", None)
                if img and (not img.isNull()):
                    try:
                        pm = QPixmap.fromImage(img)
                        self._full_pixmap = pm
                    except Exception:
                        return
                else:
                    return

            w = self.img_label.width()
            h = self.img_label.height()
            if w <= 0 or h <= 0:
                self._apply_pixmap_retries = getattr(self, "_apply_pixmap_retries", 0) + 1
                if self._apply_pixmap_retries <= 5:
                    QTimer.singleShot(0, self._apply_full_pixmap_to_label)
                return

            key = (w, h)
            cached = self._scaled_pixmap_cache.get(key)
            if cached is None:
                if pm.width() == w and pm.height() == h:
                    cached = pm
                else:
                    cached = pm.scaled(
                        w, h,
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation
                    )
                self._scaled_pixmap_cache[key] = cached
            self.img_label.setPixmap(cached)
            try:
                self._schedule_badge_layout(force=True)
            except Exception:
                pass
        except Exception:
            return

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.game)

    def closeEvent(self, event):
        download_registry.remove_listener(str(self.game['id']), self.on_registry_update)
        super().closeEvent(event)

class LibraryTab(QWidget):
    def __init__(self, main_window):
        # ... (rest of __init__ is unchanged)
        super().__init__()
        self.main_window = main_window
        self.client = main_window.client
        self.config = main_window.config
        self._platform_selection = "All Platforms"
        self._all_cards = []       # all GameCard widgets currently in grid 
        self._render_generation = 0  # incremented to cancel in-flight renders
        self._filter_generation = 0  # incremented on every apply_filters call
        self._loading_label = None
        self._pending_games = []    # games not yet rendered
        self._load_more_label = None  # "Load more..." indicator at bottom  
        self.LOAD_BATCH = 100
        self._is_loading_batch = False  # guard against concurrent loads    
        self._total_server_games = 0
        self._loaded_count = 0
        self._selected_index = -1

        self._cover_apply_queue = []
        self._cover_apply_pending = set()
        self._cover_apply_timer = QTimer(self)
        self._cover_apply_timer.setSingleShot(True)
        self._cover_apply_timer.setInterval(0)
        self._cover_apply_timer.timeout.connect(self._process_cover_apply_queue)

        self._cloud_probe_thread = None
        self._cloud_probe_pending = set()
        self._cloud_probe_backlog = []
        self._cloud_probe_activity_visible = False

        self._scroll_debounce = QTimer()
        self._scroll_debounce.setSingleShot(True)
        self._scroll_debounce.setInterval(150)  # ms cooldown
        self._scroll_debounce.timeout.connect(self._do_load_batch)

        # BUG FIX: Debounce library updates to prevent lag spikes on 'All Platforms'
        self._library_update_timer = QTimer(self)
        self._library_update_timer.setSingleShot(True)
        self._library_update_timer.setInterval(300)  # 300ms debounce
        self._library_update_timer.timeout.connect(self._do_library_refresh)
        self._library_refresh_pending = False

        try:
            watcher = getattr(self.main_window, 'watcher', None)
        except Exception:
            watcher = None
        if watcher and hasattr(watcher, 'sync_cache_updated_signal'):
            try:
                watcher.sync_cache_updated_signal.connect(self._on_sync_cache_updated, Qt.QueuedConnection)
            except Exception:
                pass

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # Filter controls (Header)
        self.filter_widget = QWidget()
        filter_layout = QHBoxLayout(self.filter_widget)
        filter_layout.setContentsMargins(10, 10, 10, 10)
        filter_layout.addWidget(QLabel("Search:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter games (Ctrl+F)...")    
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        self.search_input.returnPressed.connect(self.apply_filters)
        filter_layout.addWidget(self.search_input)

        filter_layout.addWidget(QLabel("Platform:"))
        self.platform_filter = QComboBox()
        self.platform_filter.addItem("All Platforms")
        self.platform_filter.currentTextChanged.connect(self._on_platform_changed)
        filter_layout.addWidget(self.platform_filter)

        self.refresh_btn = QPushButton("🔄 Refresh")
        self.refresh_btn.clicked.connect(lambda: self.main_window.fetch_library_and_populate(force_refresh=True))
        filter_layout.addWidget(self.refresh_btn)

        self.main_layout.addWidget(self.filter_widget)

        # Installation Toggle Bar
        self.install_filter_widget = QWidget()
        self.install_filter_widget.setStyleSheet("background: #111; border-bottom: 1px solid #222;")
        install_layout = QHBoxLayout(self.install_filter_widget)
        install_layout.setContentsMargins(10, 5, 10, 5)
        install_layout.setSpacing(5)

        self.install_filter_group = []
        self.current_install_filter = "all" # "all", "installed", "not_installed"

        for label, filter_id in [("All", "all"), ("Installed", "installed"), ("Not Installed", "not_installed")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            if filter_id == "all": btn.setChecked(True)
            
            btn.setStyleSheet("""
                QPushButton {
                    background: #222;
                    color: #888;
                    border: none;
                    padding: 6px 15px;
                    border-radius: 4px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background: #333;
                    color: #fff;
                }
                QPushButton:checked {
                    background: #0d6efd;
                    color: #fff;
                }
            """)
            btn.clicked.connect(lambda checked, fid=filter_id: self._set_install_filter(fid))
            install_layout.addWidget(btn)
            self.install_filter_group.append(btn)
        
        install_layout.addStretch()
        self.main_layout.addWidget(self.install_filter_widget)

        # Stack area
        self.stack = QStackedWidget()
        
        # Page 0: Grid
        self.grid_page = QWidget()
        grid_page_layout = QVBoxLayout(self.grid_page)
        grid_page_layout.setContentsMargins(0, 0, 0, 0)
        
        # Grid area inside scroll
        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self.scroll_area = SmoothScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self.grid_widget)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll)

        self.scroll_area.viewport().installEventFilter(self)

        self._resize_debounce = QTimer()
        self._resize_debounce.setSingleShot(True)
        self._resize_debounce.setInterval(80)
        self._resize_debounce.timeout.connect(self._resize_all_cards)       

        grid_page_layout.addWidget(self.scroll_area)

        # Status label
        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #bbb; padding: 5px; background: #222; border-top: 1px solid #333;")
        self.status_label.setVisible(False)
        grid_page_layout.addWidget(self.status_label)
        
        self.stack.addWidget(self.grid_page)
        
        # Page 1: Detail Panel (placeholder)
        self.detail_panel = None
        
        self.main_layout.addWidget(self.stack, 1)
        
        # Gamepad support via XInput polling
        self._last_buttons = 0
        self._last_axis = [0.0, 0.0]
        self._controller_lost_count = 0
        self.gamepad_timer = QTimer(self)
        self.gamepad_timer.timeout.connect(self._poll_gamepad)
        if sys.platform == 'win32':
            try:
                self.xinput = ctypes.windll.xinput1_4
                self.gamepad_timer.start(100) # 100ms polling
            except Exception:
                try:
                    self.xinput = ctypes.windll.xinput1_3
                    self.gamepad_timer.start(100)
                except Exception:
                    logging.debug("[Library] XInput unavailable — gamepad support disabled")
        else:
            logging.debug("[Library] Gamepad support currently only available on Windows via XInput")

    def _on_sync_cache_updated(self, rom_id):
        try:
            sync_cache = (self.main_window.watcher.sync_cache
                          if self.main_window.watcher else {})
            rom_id_str = str(rom_id)

            try:
                detail = getattr(self, 'detail_panel', None)
            except Exception:
                detail = None
            if detail:
                try:
                    game = getattr(detail, 'game', {})
                    if isinstance(game, dict) and str(game.get('id')) == rom_id_str:
                        if hasattr(detail, 'refresh_badges_row'):
                            QTimer.singleShot(0, detail.refresh_badges_row)
                except (RuntimeError, AttributeError):
                    pass
            for card in list(getattr(self, '_all_cards', []) or []):
                try:
                    game = getattr(card, 'game', {})
                    if not isinstance(game, dict):
                        continue
                    if str(game.get('id')) != rom_id_str:
                        continue
                    if hasattr(card, 'refresh_cloud_indicator'):
                        card.refresh_cloud_indicator(sync_cache)
                except (RuntimeError, AttributeError):
                    continue
        except Exception:
            return

    def _request_cloud_badge_prime(self, games):
        try:
            watcher = getattr(self.main_window, 'watcher', None)
        except Exception:
            watcher = None
        if not watcher:
            return

        try:
            sync_cache = watcher.sync_cache if isinstance(watcher.sync_cache, dict) else {}
        except Exception:
            sync_cache = {}

        ids = []
        now_ts = time.time()
        for g in (games or []):
            try:
                if not isinstance(g, dict):
                    continue
                rid = g.get('id')
                if rid is None:
                    continue
                rid_str = str(rid)
                entry = sync_cache.get(rid_str)
                checked_recently = False
                if isinstance(entry, dict):
                    try:
                        checked_at = float(entry.get('cloud_probe_checked_at') or 0)
                        ttl = CLOUD_BADGE_PROBE_FAILURE_TTL_SECONDS if entry.get('cloud_probe_failed') else CLOUD_BADGE_PROBE_TTL_SECONDS
                        checked_recently = (now_ts - checked_at) < ttl
                    except Exception:
                        checked_recently = False
                if checked_recently:
                    continue
                if rid_str in self._cloud_probe_pending:
                    continue
                if len(ids) >= CLOUD_BADGE_PROBE_MAX_IDS_PER_PASS:
                    continue
                self._cloud_probe_pending.add(rid_str)
                ids.append(rid)
            except Exception:
                continue

        if not ids:
            return

        try:
            t = getattr(self, '_cloud_probe_thread', None)
            if t and t.isRunning():
                for rid in ids:
                    try:
                        rid_int = int(rid)
                    except Exception:
                        continue
                    if rid_int not in self._cloud_probe_backlog:
                        self._cloud_probe_backlog.append(rid_int)
                return
        except Exception:
            pass

        self._start_cloud_probe_thread(ids)

    def _set_cloud_probe_activity(self, active):
        try:
            active = bool(active)
            if active:
                if self._cloud_probe_activity_visible:
                    return
                self._cloud_probe_activity_visible = True
                try:
                    if hasattr(self.main_window, 'title_bar'):
                        self.main_window.title_bar.set_activity(CLOUD_BADGE_PROBE_STATUS_TEXT)
                except Exception:
                    pass
                return

            if not self._cloud_probe_activity_visible:
                return
            self._cloud_probe_activity_visible = False
            try:
                if hasattr(self.main_window, 'title_bar'):
                    self.main_window.title_bar.clear_activity()
            except Exception:
                pass
        except Exception:
            return

    def _start_cloud_probe_thread(self, ids):
        if not ids:
            return
        try:
            self._set_cloud_probe_activity(True)
            self._cloud_probe_thread = CloudSaveProbeThread(self.client, ids)
            self._cloud_probe_thread.probed.connect(self._on_cloud_probe_result, Qt.QueuedConnection)
            self._cloud_probe_thread.finished.connect(self._on_cloud_probe_thread_finished, Qt.QueuedConnection)
            self._cloud_probe_thread.start()
        except Exception:
            self._set_cloud_probe_activity(False)
            return

    @Slot()
    def _on_cloud_probe_thread_finished(self):
        try:
            self._cloud_probe_thread = None
        except Exception:
            pass

        backlog_ids = []
        try:
            backlog_ids = list(self._cloud_probe_backlog)
            self._cloud_probe_backlog = []
        except Exception:
            backlog_ids = []

        if backlog_ids:
            self._start_cloud_probe_thread(backlog_ids)
            return

        self._set_cloud_probe_activity(False)

    @Slot(int, object)
    def _on_cloud_probe_result(self, rom_id, probe_payload):
        try:
            rid_str = str(rom_id)
            try:
                self._cloud_probe_pending.discard(rid_str)
            except Exception:
                pass

            watcher = getattr(self.main_window, 'watcher', None)
            if not watcher:
                return

            entry = watcher.sync_cache.get(rid_str)
            if not isinstance(entry, dict):
                entry = {}

            if not isinstance(probe_payload, dict):
                entry['cloud_probe_checked_at'] = time.time()
                entry['cloud_probe_failed'] = True
                watcher.sync_cache[rid_str] = entry
                try:
                    watcher.save_cache()
                except Exception:
                    pass
                return

            has_cloud_save = bool(probe_payload.get("has_cloud_save"))
            entry['has_cloud_save'] = has_cloud_save
            entry['cloud_probe_checked_at'] = time.time()
            entry.pop('cloud_probe_failed', None)

            save_updated_at = probe_payload.get("save_updated_at")
            state_updated_at = probe_payload.get("state_updated_at")

            if has_cloud_save:
                if save_updated_at:
                    entry['save_updated_at'] = save_updated_at
                if state_updated_at:
                    entry['state_updated_at'] = state_updated_at
            else:
                entry.pop('save_updated_at', None)
                entry.pop('state_updated_at', None)

            watcher.sync_cache[rid_str] = entry
            try:
                watcher.save_cache()
            except Exception:
                pass

            try:
                if hasattr(watcher, 'sync_cache_updated_signal'):
                    watcher.sync_cache_updated_signal.emit(int(rom_id))
                else:
                    self._on_sync_cache_updated(int(rom_id))
            except Exception:
                return
        except Exception:
            return

    def _poll_gamepad(self):
        if not hasattr(self, 'xinput'):
            return

        controller_type = self.config.get("controller_type", "xinput")
        mapping = CONTROLLER_MAPS.get(controller_type, CONTROLLER_MAPS["xinput"])
        
        state = XINPUT_STATE()
        res = self.xinput.XInputGetState(0, ctypes.byref(state))
        
        if res == 0:
            # Controller connected
            self._controller_lost_count = 0
            if hasattr(self.main_window, 'title_bar'):
                self.main_window.title_bar.gamepad_indicator.setVisible(True)
        else:
            # Controller disconnected
            self._controller_lost_count += 1
            if self._controller_lost_count >= 3:
                if hasattr(self.main_window, 'title_bar'):
                    self.main_window.title_bar.gamepad_indicator.setVisible(False)
            return

        buttons = state.Gamepad.wButtons
        lx = state.Gamepad.sThumbLX
        ly = state.Gamepad.sThumbLY
        dz = mapping["stick_deadzone"]
        
        # Detect new presses only (not holds)
        prev = getattr(self, '_prev_buttons', 0)
        prev_lx = getattr(self, '_prev_lx', 0)
        prev_ly = getattr(self, '_prev_ly', 0)
        
        def pressed(mask):
            return (buttons & mask) and not (prev & mask)

        if pressed(mapping["up"]):    self._gamepad_up()
        if pressed(mapping["down"]):  self._gamepad_down()
        if pressed(mapping["left"]):  self._gamepad_left()
        if pressed(mapping["right"]): self._gamepad_right()
        if pressed(mapping["confirm"]): self._gamepad_confirm()
        if pressed(mapping["back"]):    self._gamepad_back()

        # Left stick — only trigger on crossing deadzone threshold
        stick_up    = ly >  dz and prev_ly <= dz
        stick_down  = ly < -dz and prev_ly >= -dz
        stick_left  = lx < -dz and prev_lx >= -dz
        stick_right = lx >  dz and prev_lx <= dz
        
        if stick_up:    self._gamepad_up()
        if stick_down:  self._gamepad_down()
        if stick_left:  self._gamepad_left()
        if stick_right: self._gamepad_right()

        self._prev_buttons = buttons
        self._prev_lx = lx
        self._prev_ly = ly

    def _gamepad_up(self):      self._on_nav_key(Qt.Key_Up)
    def _gamepad_down(self):    self._on_nav_key(Qt.Key_Down)
    def _gamepad_left(self):    self._on_nav_key(Qt.Key_Left)
    def _gamepad_right(self):   self._on_nav_key(Qt.Key_Right)
    def _gamepad_confirm(self): self._on_nav_key(Qt.Key_Return)
    def _gamepad_back(self):    self._on_nav_key(Qt.Key_Escape)

    def keyPressEvent(self, event):
        if self._on_nav_key(event.key()):
            event.accept()
        else:
            super().keyPressEvent(event)

    def _on_nav_key(self, key):
        if not self._all_cards: return False
        
        visible_cards = [c for c in self._all_cards if c.isVisible()]
        if not visible_cards: return False
        
        if self._selected_index == -1:
            if key in [Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down]:
                self._select_card(0, visible_cards)
                return True
            return False

        _, cols = self._get_card_size()
        idx = self._selected_index
        
        if key == Qt.Key_Right:
            idx = min(len(visible_cards) - 1, idx + 1)
        elif key == Qt.Key_Left:
            idx = max(0, idx - 1)
        elif key == Qt.Key_Down:
            idx = min(len(visible_cards) - 1, idx + cols)
        elif key == Qt.Key_Up:
            idx = max(0, idx - cols)
        elif key == Qt.Key_Return:
            self.open_detail(visible_cards[idx].game)
            return True
        elif key == Qt.Key_Escape:
            if self.stack.currentWidget() == self.detail_panel:
                self._close_detail()
            else:
                self._select_card(-1, visible_cards)
            return True
        else:
            return False
            
        self._select_card(idx, visible_cards)
        return True

    def _select_card(self, index, visible_cards):
        if self._selected_index != -1 and self._selected_index < len(visible_cards):
            visible_cards[self._selected_index].set_selected(False)
            
        self._selected_index = index
        if index != -1:
            card = visible_cards[index]
            card.set_selected(True)
            self.scroll_area.ensureWidgetVisible(card)

    def set_status(self, text, color=None):
        if not text:
            self.status_label.setVisible(False)
            return
        self.status_label.setText(text)
        if color:
            self.status_label.setStyleSheet(f"color: {color}; padding: 5px; background: #222; border-top: 1px solid #333;")
        else:
            self.status_label.setStyleSheet("color: #bbb; padding: 5px; background: #222; border-top: 1px solid #333;")
        self.status_label.setVisible(True)

    def append_batch(self, games):
        """Called when new games arrive from parallel server fetch.
        Instead of appending to the end (which breaks alphabetical order),
        trigger a full apply_filters re-render so all games are sorted correctly.
        
        BUG FIX: Throttle this so filters update during parsing without rebuilding
        for every single page.
        """
        # Games are already added to main_window.all_games by the caller.
        self._queue_library_refresh(immediate=True)

    def _queue_library_refresh(self, immediate=False):
        """Schedule a throttled library refresh.

        immediate=True triggers a leading-edge refresh (first update right away),
        then keeps the debounce window active for follow-up updates.
        """
        self._library_refresh_pending = True

        if immediate and not self._library_update_timer.isActive():
            self._do_library_refresh()
            self._library_update_timer.start()
            return

        if not self._library_update_timer.isActive():
            self._library_update_timer.start()

    def _do_library_refresh(self):
        """Actual logic for debounced library refresh."""
        if not self._library_refresh_pending:
            return

        self._library_refresh_pending = False
        self.apply_filters()

        if self._library_refresh_pending:
            self._library_update_timer.start()

    def _get_card_size(self):
        """Compute card width/height based on viewport width and cols setting."""
        cols = max(1, int(self.config.get("cards_per_row", 6)))
        spacing = self.grid_layout.horizontalSpacing() * (cols - 1) + 20    
        available = self.scroll_area.viewport().width() - spacing
        w = max(100, available // cols)
        return w, cols

    def _resize_all_cards(self):
        """Resize every rendered card to match current viewport width."""   
        if not self._all_cards:
            return
        w, cols = self._get_card_size()
        self.grid_widget.setUpdatesEnabled(False)
        try:
            for card in self._all_cards:
                img_w = max(1, w - 10)
                title_h = 30
                gap_h = int(getattr(card, "_cover_title_spacing", 5))
                margins_h = 10

                if card._full_pixmap and not card._full_pixmap.isNull() and card._full_pixmap.width() > 0:
                    ratio = card._full_pixmap.height() / card._full_pixmap.width()
                    img_h = max(1, int(img_w * ratio))
                else:
                    img_h = max(1, int(img_w * 1.5))

                card.setFixedSize(w, img_h + title_h + gap_h + margins_h)
                card.img_label.setFixedSize(img_w, img_h)
                if card._full_pixmap and not card._full_pixmap.isNull():
                    key = (img_w, img_h)
                    cached = getattr(card, "_scaled_pixmap_cache", {}).get(key)
                    if cached is None:
                        cached = card._full_pixmap.scaled(
                            img_w, img_h,
                            Qt.KeepAspectRatio,
                            Qt.SmoothTransformation
                        )
                        try:
                            card._scaled_pixmap_cache[key] = cached
                        except Exception:
                            pass
                    card.img_label.setPixmap(cached)
                card.update_title_width(w - 10)
        finally:
            self.grid_widget.setUpdatesEnabled(True)

        try:
            self._request_cloud_badge_prime([
                getattr(c, 'game', None)
                for c in (self._all_cards or [])
                if c and c.isVisible()
            ])
        except Exception:
            pass

    def eventFilter(self, obj, event):
        try:
            if (obj is self.scroll_area.viewport()
                    and event.type() == QEvent.Type.Resize):
                self._resize_debounce.start()
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def _on_scroll(self, value):
        """Debounce scroll events before loading next batch."""
        if not self._pending_games or self._is_loading_batch:
            return
        scrollbar = self.scroll_area.verticalScrollBar()
        max_val = scrollbar.maximum()
        if max_val <= 0:
            return
        if value >= max_val * 0.60:
            # Restart the debounce timer — only fires after 150ms of      
            # no scroll events, preventing rapid-fire batch loads
            self._scroll_debounce.start()

    def _do_load_batch(self):
        """Actually load the next batch — called after scroll debounce."""
        if not self._pending_games or self._is_loading_batch:
            return
        scrollbar = self.scroll_area.verticalScrollBar()
        max_val = scrollbar.maximum()
        value = scrollbar.value()
        if max_val <= 0 or value < max_val * 0.60:
            return

        self._is_loading_batch = True
        self._render_next_batch()

    def _set_install_filter(self, filter_id):
        self.current_install_filter = filter_id
        self.apply_filters()

    def _on_search_text_changed(self, text):
        if not text:
            self.apply_filters()

    def _on_platform_changed(self, text):
        self._platform_selection = text or "All Platforms"
        self.apply_filters()

    def apply_filters(self):
        from src import download_registry
        
        # BUG FIX: Stop any pending scroll loads before starting a new filter
        self._scroll_debounce.stop()
        self._is_loading_batch = False

        self._filter_generation += 1
        my_filter_gen = self._filter_generation

        text = self.search_input.text().lower()
        platform = getattr(self, "_platform_selection", None) or self.platform_filter.currentText() or "All Platforms"
        self._selected_index = -1

        if platform == "⚠️ No Emulator":
            all_known = set(RETROARCH_PLATFORMS)
            for emu in emulators.load_emulators():
                all_known.update(emu.get("platform_slugs", []))
            base_filtered = [
                g for g in self.main_window.all_games
                if g.get("platform_slug") not in all_known
                and (not text
                     or text in str(g.get('name', '')).lower()
                     or text in str(g.get('fs_name', '')).lower())
            ]
        else:
            base_filtered = [
                g for g in self.main_window.all_games
                if (not text
                    or text in str(g.get('name', '')).lower()
                    or text in str(g.get('fs_name', '')).lower())
                and (platform == "All Platforms"
                     or g.get('platform_display_name') == platform
                     or g.get('platform_slug') == platform)
            ]

        filtered = []
        for g in base_filtered:
            is_installed = g.get('_local_exists', False) or download_registry.get(str(g.get('id'))) is not None

            if self.current_install_filter == "installed":
                if is_installed:
                    filtered.append(g)
            elif self.current_install_filter == "not_installed":
                if not is_installed:
                    filtered.append(g)
            else:
                filtered.append(g)

        filtered.sort(key=lambda g: str(g.get('name', '') or g.get('fs_name', '')).lower())

        if my_filter_gen != self._filter_generation:
            return

        platform_changed = (
            not hasattr(self, '_current_platform') or
            self._current_platform != platform
        )

        grid_ids = getattr(self, "_grid_game_ids", None)
        filtered_ids_set = set(g.get('id') for g in filtered)
        can_reflow = (
            not platform_changed
            and self._all_cards
            and not self._pending_games
            and isinstance(grid_ids, set)
            and filtered_ids_set.issubset(grid_ids)
        )

        if can_reflow:
            self.grid_widget.setUpdatesEnabled(False)
            try:
                visible_cards = []

                for card in self._all_cards:
                    try:
                        visible = card.game.get('id') in filtered_ids_set
                        card.setVisible(visible)
                        card.set_selected(False)
                        if visible:
                            visible_cards.append(card)
                    except (RuntimeError, AttributeError):
                        continue

                while self.grid_layout.count():
                    item = self.grid_layout.takeAt(0)
                    if item and item.widget() and not isinstance(item.widget(), GameCard):
                        try:
                            item.widget().hide()
                            item.widget().deleteLater()
                        except (RuntimeError, AttributeError):
                            pass

                _, cols = self._get_card_size()
                for idx, card in enumerate(visible_cards):
                    if my_filter_gen != self._filter_generation:
                        return

                    row = idx // cols
                    col = idx % cols
                    try:
                        self.grid_layout.addWidget(card, row, col)
                        try:
                            if hasattr(card, '_update_badge_layout'):
                                card._update_badge_layout(force=True)
                        except Exception:
                            pass
                    except (RuntimeError, AttributeError):
                        continue

                if not visible_cards:
                    self.show_empty_message("No games match your search.")

            finally:
                self.grid_widget.setUpdatesEnabled(True)
        else:
            self.populate_grid(filtered)

    def _enqueue_cover_apply(self, card):
        try:
            gid = None
            try:
                gid = int(card.game.get('id'))
            except Exception:
                gid = id(card)
            if gid in self._cover_apply_pending:
                return
            self._cover_apply_pending.add(gid)
            self._cover_apply_queue.append((gid, card))
            if not self._cover_apply_timer.isActive():
                self._cover_apply_timer.start()
        except Exception:
            return

    def _process_cover_apply_queue(self):
        try:
            budget = 8
            while budget > 0 and self._cover_apply_queue:
                gid, card = self._cover_apply_queue.pop(0)
                try:
                    self._cover_apply_pending.discard(gid)
                except Exception:
                    pass
                try:
                    if card and card.isVisible():
                        card._apply_full_pixmap_to_label()
                except Exception:
                    pass
                budget -= 1
        finally:
            if self._cover_apply_queue:
                try:
                    self._cover_apply_timer.start()
                except Exception:
                    pass

    def update_game_local_status(self, game_id, exists):
        """Dynamically updates a GameCard's checkmark status."""
        found = False
        for card in self._all_cards:
            try:
                if card.game.get('id') == game_id:
                    card.set_local_exists(exists)
                    found = True
                    break
            except RuntimeError:
                continue

        if self.current_install_filter != "all":
            # Re-apply install-specific filters so cards can appear/disappear as
            # local discovery progresses, even if the card is not currently visible.
            self._queue_library_refresh()

    def refresh_card_states(self):
        """Re-apply filters to catch any registry changes (e.g. cancellations)."""
        self.apply_filters()

    def populate_games(self, games, status=None):
        """Standard method to populate the grid from a list of games."""
        if status:
            self.set_status(status)
        self.apply_filters()

    def populate_grid(self, games):
        viewport = self.scroll_area.viewport()
        viewport.setUpdatesEnabled(False)

        # Reset refresh button style
        try:
            self.refresh_btn.setStyleSheet("")
            self.refresh_btn.setText("🔄 Refresh")

            # Increment generation — any pending render callbacks will
            # check this and abort immediately
            self._render_generation += 1
            my_gen = self._render_generation

            # BUG FIX: Also increment filter generation to stop any pending scroll loads
            self._filter_generation += 1
            my_filter_gen = self._filter_generation

            # BUG FIX: Stop all active image fetchers before clearing grid.
            # We request interruption and quit, but do NOT wait() because it blocks the UI.
            for fetcher in list(getattr(self.main_window, 'active_image_fetchers', [])):
                try:
                    fetcher.requestInterruption()
                    fetcher.quit()
                except (RuntimeError, AttributeError):
                    pass
            self.main_window.active_image_fetchers = []
            self.main_window.image_fetch_queue = []

            # Start a new cover-fetch generation for this grid rebuild. This is used
            # to ignore in-flight fetch completions from the previous grid.
            try:
                self.main_window.fetch_generation += 1
            except Exception:
                pass

            self._all_cards = []
            self._pending_games = list(games)  # full list, render in batches
            self._scroll_debounce.stop()  # cancel any pending debounce on grid reset
            self._is_loading_batch = False
            self._current_platform = getattr(self, "_platform_selection", None) or self.platform_filter.currentText()
            self._grid_game_ids = {g.get('id') for g in games if isinstance(g, dict) and g.get('id') is not None}
            self._selected_index = -1

            # Clear grid — use deleteLater for safety
            while self.grid_layout.count():
                item = self.grid_layout.takeAt(0)
                if item and item.widget():
                    try:
                        item.widget().hide()
                        item.widget().deleteLater()
                    except (RuntimeError, AttributeError):
                        pass

            # Remove old load-more label ref
            self._load_more_label = None

            if not games:
                self.show_empty_message("No games match your search.")
                return

            # Render first batch immediately
            self._render_next_batch(my_gen, my_filter_gen)
        finally:
            viewport.setUpdatesEnabled(True)
            viewport.update()

    def _do_load_batch(self):
        """Actually load the next batch — called after scroll debounce."""
        if not self._pending_games or self._is_loading_batch:
            return
        
        # BUG FIX: Capture current generations
        my_gen = self._render_generation
        my_filter_gen = self._filter_generation

        scrollbar = self.scroll_area.verticalScrollBar()
        max_val = scrollbar.maximum()
        value = scrollbar.value()
        if max_val <= 0 or value < max_val * 0.60:
            return

        self._is_loading_batch = True
        self._render_next_batch(my_gen, my_filter_gen)

    def _render_next_batch(self, generation=None, filter_generation=None):
        """Render the next LOAD_BATCH pending games into the grid."""       
        # Use current generation if not specified
        if generation is None:
            generation = self._render_generation
        if filter_generation is None:
            filter_generation = self._filter_generation

        # Abort if stale (race condition guard)
        if generation != self._render_generation or filter_generation != self._filter_generation:
            self._is_loading_batch = False
            return

        if not self._pending_games:
            # Remove load-more label if present
            if self._load_more_label:
                try:
                    self._load_more_label.hide()
                    self._load_more_label.deleteLater()
                except (RuntimeError, AttributeError):
                    pass
                self._load_more_label = None
            self._is_loading_batch = False
            return

        batch = self._pending_games[:self.LOAD_BATCH]
        self._pending_games = self._pending_games[self.LOAD_BATCH:]

        sync_cache = (self.main_window.watcher.sync_cache
                      if self.main_window.watcher else {})

        # Remove load-more label before adding new cards
        if self._load_more_label:
            try:
                self.grid_layout.removeWidget(self._load_more_label)
                self._load_more_label.hide()
                self._load_more_label.deleteLater()
            except (RuntimeError, AttributeError):
                pass
            self._load_more_label = None

        card_w, cols_per_row = self._get_card_size()

        max_concurrent = 6

        self.grid_widget.setUpdatesEnabled(False)
        try:
            # Calculate starting row/col from existing card count
            total_so_far = len(self._all_cards)
            row = total_so_far // cols_per_row
            col = total_so_far % cols_per_row
            start_idx = len(self._all_cards)

            for i, game in enumerate(batch):
                # RACE CONDITION GUARD
                if generation != self._render_generation or filter_generation != self._filter_generation:
                    return

                try:
                    card = GameCard(game, self.client, self.config, sync_cache)
                    if game.get('_local_exists'):
                        card.set_local_exists(True)
                    card.clicked.connect(lambda g=game: self.open_detail(g))

                    img_w = max(1, card_w - 10)
                    title_h = 30
                    img_h = max(1, int(img_w * 1.5))
                    gap_h = 5
                    card.setFixedSize(card_w, img_h + gap_h + title_h)
                    card.img_label.setFixedSize(img_w, img_h)
                    card.update_title_width(img_w)
                    try:
                        if hasattr(card, '_update_badge_layout'):
                            card._update_badge_layout(force=True)
                    except Exception:
                        pass
                    self.grid_layout.addWidget(card, row, col)
                    self._all_cards.append(card)
                except (RuntimeError, AttributeError):
                    continue

                col += 1
                if col >= cols_per_row:
                    col = 0
                    row += 1
        finally:
            self.grid_widget.setUpdatesEnabled(True)

        my_fetch_gen = self.main_window.fetch_generation
        for j, card in enumerate(self._all_cards[start_idx:]):
            if generation != self._render_generation or filter_generation != self._filter_generation:
                break

            abs_idx = start_idx + j
            if abs_idx < max_concurrent:
                try:
                    fetcher = card.start_image_fetch(self.main_window, my_fetch_gen)
                    if fetcher:
                        self.main_window.active_image_fetchers.append(fetcher)
                except (RuntimeError, AttributeError):
                    continue
            else:
                self.main_window.image_fetch_queue.append(card)

        try:
            self._request_cloud_badge_prime(batch)
        except Exception:
            pass

        try:
            while (len(self.main_window.active_image_fetchers) < max_concurrent
                   and self.main_window.image_fetch_queue):
                next_card = self.main_window.image_fetch_queue.pop(0)
                try:
                    new_fetcher = next_card.start_image_fetch(self.main_window, my_fetch_gen)
                    if new_fetcher:
                        self.main_window.active_image_fetchers.append(new_fetcher)
                except (RuntimeError, AttributeError):
                    continue
        except Exception:
            pass

        if self._pending_games:
            if generation != self._render_generation or filter_generation != self._filter_generation:
                self._is_loading_batch = False
                return

            remaining = len(self._pending_games)
            self._load_more_label = QLabel(f"⬇ Scroll down to load {remaining} more games...")
            self._load_more_label.setAlignment(Qt.AlignCenter)
            self._load_more_label.setStyleSheet(
                "color: #1e88e5; font-size: 13px; "
                "padding: 20px; background: #1a1a1a;")
            next_row = (len(self._all_cards) + cols_per_row - 1) // cols_per_row
            self.grid_layout.addWidget(
                self._load_more_label, next_row, 0, 1, cols_per_row)

        self._is_loading_batch = False

    def open_detail(self, game):
        # Local import to avoid circular dependency
        from src.ui.dialogs.game_detail import GameDetailPanel

        try:
            self._request_cloud_badge_prime([game])
        except Exception:
            pass
        
        # Remove old detail page if exists
        if self.detail_panel:
            try:
                self.stack.removeWidget(self.detail_panel)
                self.detail_panel.deleteLater()
            except (RuntimeError, AttributeError):
                pass
        
        self.detail_panel = GameDetailPanel(
            game, self.client, self.config,
            self.main_window,
            on_close=self._close_detail,
            parent=self
        )
        self.stack.addWidget(self.detail_panel)
        self.stack.setCurrentWidget(self.detail_panel)
        self.filter_widget.hide() # Hide filters while in detail view
        self.install_filter_widget.hide()

    def _close_detail(self):
        self.stack.setCurrentWidget(self.grid_page)
        self.filter_widget.show()
        self.install_filter_widget.show()
        if self.detail_panel:
            try:
                self.stack.removeWidget(self.detail_panel)
                self.detail_panel.deleteLater()
            except (RuntimeError, AttributeError):
                pass
            self.detail_panel = None
        self._resize_all_cards()

    def show_empty_message(self, message):
        # The empty-state UI clears widgets from the layout. If we don't
        # also reset our internal card tracking, apply_filters() may try to
        # reflow widgets that have been deleted, leaving the grid stuck blank.
        self._all_cards = []
        self._pending_games = []
        self._load_more_label = None
        self._is_loading_batch = False

        for i in reversed(range(self.grid_layout.count())):
            item = self.grid_layout.itemAt(i)
            if item and item.widget():
                try:
                    item.widget().setParent(None)
                    item.widget().deleteLater()
                except (RuntimeError, AttributeError):
                    pass

        # If it's an error message, highlight the refresh button
        if any(word in message.lower() for word in ["error", "failed", "could not", "unable"]):
            self.refresh_btn.setStyleSheet(
                "background: #e65100; color: white; padding: 4px 10px; font-weight: bold;"
            )
            self.refresh_btn.setText("⚠️ Retry")
        else:
            self.refresh_btn.setStyleSheet("")
            self.refresh_btn.setText("🔄 Refresh")

        empty_label = QLabel(message)
        empty_label.setAlignment(Qt.AlignCenter)
        empty_label.setStyleSheet("color: #888; font-size: 14px; padding: 40px;")
        self.grid_layout.addWidget(empty_label, 0, 0)

    def closeEvent(self, event):
        try:
            t = getattr(self, '_cloud_probe_thread', None)
            if t and t.isRunning():
                try:
                    t.requestInterruption()
                except Exception:
                    pass
                try:
                    t.quit()
                except Exception:
                    pass
                try:
                    t.wait(1000)
                except Exception:
                    pass
            self._cloud_probe_thread = None
            self._cloud_probe_backlog = []
            self._cloud_probe_pending.clear()
        except Exception:
            pass
        super().closeEvent(event)

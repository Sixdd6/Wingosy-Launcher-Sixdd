import os
from pathlib import Path
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit, QMessageBox, QFileDialog)
from PySide6.QtCore import Qt, QTimer
from src import windows_saves

EXCLUDED_EXES = [
    "unins000.exe", "uninstall.exe", "setup.exe",
    "vcredist", "directx", "dxsetup.exe",
    "vc_redist", "crashpad_handler.exe",
    "notification_helper.exe", "UnityCrashHandler",
    "dotnet", "netfx", "oalinst.exe",
    "DXSETUP.exe", "installscript",
    "dx_setup", "redist"
]

class WindowsGameSettingsDialog(QWidget):
    def __init__(self, game, config, main_window, parent=None):
        super().__init__(main_window)
        self.game = game
        self.config = config
        self.main_window = main_window
        
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowTitleHint)
        self.setFixedSize(550, 400)
        self.setWindowTitle(f"Game Settings — {game.get('name')} — Wingosy")
        
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
        """)

        saved = windows_saves.get_windows_save(game['id']) or {"name": game.get('name')}
        self.default_exe = saved.get("default_exe")
        self.save_dir = saved.get("save_dir")
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        
        layout.addWidget(QLabel("<h3>Default Executable</h3><p>Choose which .exe to launch by default.</p>"))
        
        self.exe_status = QLabel()
        self.exe_status.setStyleSheet("color: #aaa; background: transparent;")
        layout.addWidget(self.exe_status)
        
        eb = QHBoxLayout()
        ab = QPushButton("🔍 Auto-detect")
        ab.clicked.connect(self.auto_detect_exe)
        eb.addWidget(ab)
        bb = QPushButton("📁 Browse")
        bb.clicked.connect(self.browse_exe)
        eb.addWidget(bb)
        layout.addLayout(eb)
        layout.addSpacing(20)
        
        layout.addWidget(QLabel("<h3>Save Directory</h3><p>Where does this game store its saves?</p>"))
        self.save_status = QLabel()
        self.save_status.setStyleSheet("color: #aaa; background: transparent;")
        layout.addWidget(self.save_status)
        
        sb = QHBoxLayout()
        mb = QPushButton("📁 Browse Manually")
        mb.clicked.connect(self.browse_save_dir)
        sb.addWidget(mb)
        layout.addLayout(sb)
        
        self.sync_status = QLabel()
        self.sync_status.setStyleSheet("font-weight: bold; background: transparent;")
        layout.addWidget(self.sync_status)
        layout.addStretch()
        
        btns = QHBoxLayout()
        btns.addStretch()
        save_btn = QPushButton("Save Settings")
        save_btn.setStyleSheet("background: #1565c0; color: white; padding: 8px 20px; font-weight: bold;")
        save_btn.clicked.connect(self.save_and_close)
        btns.addWidget(save_btn)
        layout.addLayout(btns)
        
        QTimer.singleShot(0, self._apply_dark_frame)
        QTimer.singleShot(50, self._center_on_parent)
        self.update_ui()

    def _apply_dark_frame(self):
        import sys, ctypes
        if sys.platform != "win32": return
        try:
            hwnd = int(self.winId())
            v = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(v), ctypes.sizeof(v))
        except Exception: pass

    def _center_on_parent(self):
        p = self.parent()
        if not p: return
        pg = p.geometry()
        x = pg.x() + (pg.width() - self.width()) // 2
        y = pg.y() + (pg.height() - self.height()) // 2
        self.move(x, y)

    def update_ui(self):
        if self.default_exe:
            self.exe_status.setText(f"<b>{os.path.basename(self.default_exe)}</b><br><small>{self.default_exe}</small>")
        else:
            self.exe_status.setText("No default set")
            
        self.save_status.setText(self.save_dir or "Not configured")
        
        if self.save_dir and os.path.exists(self.save_dir):
            self.sync_status.setText("<span style='color: #4caf50;'>✅ Cloud sync active</span>")
        elif self.save_dir:
            self.sync_status.setText("<span style='color: #ff5252;'>⚠️ Folder does not exist</span>")
        else:
            self.sync_status.setText("")
            
    def auto_detect_exe(self):
        rom = self.game.get('fs_name')
        win_dir = self.config.get("windows_games_dir")
        if not rom or not win_dir: return
        folder = Path(win_dir) / Path(rom).stem
        if not folder.exists(): return
        exes = [str(p) for p in folder.rglob("*.exe") if not any(ex.lower() in str(p).lower() for e in EXCLUDED_EXES)]
        if not exes:
            QMessageBox.information(self, "No EXEs — Wingosy", "None found.")
            return
        if len(exes) == 1:
            self.default_exe = exes[0]
            self.update_ui()
        else:
            from src.ui.dialogs.emulator_editor import ExePickerDialog
            p = ExePickerDialog(exes, self.game.get("name"), self)
            p.show()
                
    def browse_exe(self):
        p, _ = QFileDialog.getOpenFileName(self, "Select Executable — Wingosy", "", "Executables (*.exe)")
        if p:
            self.default_exe = p
            self.update_ui()
            
    def browse_save_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Save Folder — Wingosy")
        if directory:
            self.save_dir = directory
            self.update_ui()
            
    def save_and_close(self):
        windows_saves.set_windows_save(self.game['id'], self.game['name'], self.save_dir, self.default_exe)
        self.close()

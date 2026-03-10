import os
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, QListWidget, QListWidgetItem, QMessageBox)
from PySide6.QtCore import Qt, QTimer
from src.ui.widgets import format_size

class ExePickerDialog(QWidget):
    def __init__(self, exes, game_name, parent=None):
        super().__init__(parent)
        
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint | Qt.WindowTitleHint)
        self.setFixedSize(600, 450)
        self.setWindowTitle(f"Choose Executable — {game_name} — Wingosy")
        
        self.selected_exe = None
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
            QListWidget {
                background-color: #2b2b2b;
                color: #ffffff;
                border: 1px solid #555;
                font-size: 10pt;
            }
            QListWidget::item {
                padding: 12px;
                border-bottom: 1px solid #3a3a3a;
            }
            QListWidget::item:selected {
                background-color: #0d6efd;
                color: #ffffff;
            }
            QListWidget::item:hover {
                background-color: #3a3a3a;
            }
        """)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        
        header = QLabel("Multiple executables found. Select one to launch:")
        header.setStyleSheet("font-size: 12pt; font-weight: bold; margin-bottom: 10px; background: transparent;")
        layout.addWidget(header)
        
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)
        
        for path in exes:
            try:
                size_str = format_size(os.path.getsize(path))
            except:
                size_str = "Unknown"
            item = QListWidgetItem(f"{os.path.basename(path)}\n({size_str}) — {path}")
            item.setData(Qt.UserRole, path)
            self.list_widget.addItem(item)
            
        btns = QHBoxLayout()
        btns.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet("background: #444; color: #eee; padding: 10px 20px;")
        cancel_btn.clicked.connect(self.close)
        btns.addWidget(cancel_btn)
        
        launch_btn = QPushButton("▶ Launch Selected")
        launch_btn.setStyleSheet("background: #2e7d32; color: white; font-weight: bold; padding: 10px 20px; font-size: 11pt;")
        launch_btn.clicked.connect(self.accept_selection)
        btns.addWidget(launch_btn)
        
        layout.addLayout(btns)
        
        QTimer.singleShot(0, self._apply_dark_frame)
        QTimer.singleShot(50, self._center_on_parent)

    def _apply_dark_frame(self):
        import sys, ctypes
        if sys.platform == "win32":
            try: ctypes.windll.dwmapi.DwmSetWindowAttribute(int(self.winId()), 20, ctypes.byref(ctypes.c_int(1)), 4)
            except: pass

    def _center_on_parent(self):
        p = self.parent()
        if not p: return
        pg = p.geometry()
        x = pg.x() + (pg.width() - self.width()) // 2
        y = pg.y() + (pg.height() - self.height()) // 2
        self.move(x, y)

    def accept_selection(self):
        if self.list_widget.currentItem():
            self.selected_exe = self.list_widget.currentItem().data(Qt.UserRole)
            # Find GameDetailDialog parent if possible to call play_game
            p = self.parent()
            while p:
                if hasattr(p, 'play_game') and hasattr(p, 'default_exe'):
                    p.default_exe = self.selected_exe
                    p.play_game()
                    break
                p = p.parent()
            self.close()
        else:
            QMessageBox.warning(self, "No Selection — Wingosy", "Please select an executable.")

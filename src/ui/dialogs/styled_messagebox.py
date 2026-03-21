from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


@dataclass(frozen=True)
class _BtnSpec:
    label: str
    object_name: str = ""
    role: str = "accept"  # accept | reject


class StyledMessageBox(QDialog):
    Yes = 0x00004000
    No = 0x00010000
    Cancel = 0x00400000
    Ok = 0x00000400

    Accepted = QDialog.Accepted
    Rejected = QDialog.Rejected

    _BUTTON_SPECS: Dict[int, _BtnSpec] = {
        Yes: _BtnSpec("Yes", "PrimaryBtn", "accept"),
        No: _BtnSpec("No", "", "reject"),
        Cancel: _BtnSpec("Cancel", "", "reject"),
        Ok: _BtnSpec("OK", "PrimaryBtn", "accept"),
    }

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        title_text: str = "",
        message: str = "",
        buttons: int = Ok,
        default_button: Optional[int] = None,
        primary_object_name: str = "PrimaryBtn",
    ):
        super().__init__(parent)
        self._drag_pos = None
        self._clicked_button: Optional[int] = None

        self.setWindowTitle(title_text)
        self.setFixedSize(520, 230)

        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self.setStyleSheet("""
            #MsgRoot {
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
            QPushButton#PrimaryBtn {
                background: #1565c0;
                border-color: #1d76da;
                color: #ffffff;
            }
            QPushButton#PrimaryBtn:hover {
                background: #1a73d6;
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
        root.setObjectName("MsgRoot")
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
        close_btn.clicked.connect(self._on_close_clicked)
        title_row.addWidget(close_btn)

        layout.addLayout(title_row)

        msg_lbl = QLabel(message)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet("color: #d9d9d9;")
        layout.addWidget(msg_lbl)
        self._message_label = msg_lbl
        layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        self._default_qt_button = default_button

        if buttons:
            btns = self._build_buttons(buttons, primary_object_name)
            for b, btn in btns:
                btn_row.addWidget(btn)
            layout.addLayout(btn_row)

        QTimer.singleShot(0, self._apply_dark_frame)
        QTimer.singleShot(50, self._center_on_parent)

    def _build_buttons(self, buttons: int, primary_object_name: str) -> Tuple[Tuple[int, QPushButton], ...]:
        out = []
        for bit, spec in self._BUTTON_SPECS.items():
            if not (buttons & bit):
                continue
            btn = QPushButton(spec.label)
            if spec.object_name:
                obj_name = primary_object_name if spec.object_name == "PrimaryBtn" else spec.object_name
                btn.setObjectName(obj_name)
            btn.clicked.connect(lambda checked=False, b=bit: self._on_button_clicked(b))
            out.append((bit, btn))

        # Default button handling
        default_bit = self._default_qt_button
        if default_bit is None:
            # Prefer OK/Yes if present, otherwise first
            for pref in (self.Ok, self.Yes):
                if any(b == pref for b, _ in out):
                    default_bit = pref
                    break
            if default_bit is None and out:
                default_bit = out[0][0]

        if default_bit is not None:
            for bit, btn in out:
                if bit == default_bit:
                    btn.setDefault(True)
                    btn.setAutoDefault(True)
                    break

        return tuple(out)

    def _on_button_clicked(self, button: int):
        self._clicked_button = button
        spec = self._BUTTON_SPECS.get(button)
        if spec and spec.role == "accept":
            self.accept()
        else:
            self.reject()

    def _on_close_clicked(self):
        self._clicked_button = None
        self.reject()

    def clickedButton(self) -> Optional[int]:
        return self._clicked_button

    def setText(self, text: str) -> None:
        try:
            self._message_label.setText(text)
        except Exception:
            pass

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
        import sys
        import ctypes

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

    @classmethod
    def question(cls, parent: Optional[QWidget], title: str, text: str, buttons: int = Yes | No, default: Optional[int] = None) -> int:
        dlg = cls(parent, title, text, buttons=buttons, default_button=default)
        dlg.exec()
        return dlg.clickedButton() if dlg.clickedButton() is not None else cls.Cancel

    @classmethod
    def information(cls, parent: Optional[QWidget], title: str, text: str) -> int:
        dlg = cls(parent, title, text, buttons=cls.Ok)
        dlg.exec()
        return cls.Ok

    @classmethod
    def warning(cls, parent: Optional[QWidget], title: str, text: str) -> int:
        dlg = cls(parent, title, text, buttons=cls.Ok)
        dlg.exec()
        return cls.Ok

    @classmethod
    def critical(cls, parent: Optional[QWidget], title: str, text: str) -> int:
        dlg = cls(parent, title, text, buttons=cls.Ok, primary_object_name="DangerBtn")
        dlg.exec()
        return cls.Ok

import os
import sys

# Fix certifi path for PyInstaller frozen exe BEFORE 
# any other imports cache the wrong path
if getattr(sys, 'frozen', False):
    # We are running as a PyInstaller bundle
    # certifi is bundled in the _MEIPASS/certifi/ folder
    _mei = getattr(sys, '_MEIPASS', None)
    if _mei:
        _ca_bundle = os.path.join(_mei, 'certifi', 'cacert.pem')
        if os.path.exists(_ca_bundle):
            os.environ['REQUESTS_CA_BUNDLE'] = _ca_bundle
            os.environ['SSL_CERT_FILE'] = _ca_bundle
            os.environ['CURL_CA_BUNDLE'] = _ca_bundle

import logging
from pathlib import Path
from src.app_paths import primary_app_dir

import io
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        elif sys.stdout and hasattr(sys.stdout, 'buffer'):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True, write_through=True)
    except Exception:
        pass
    try:
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        elif sys.stderr and hasattr(sys.stderr, 'buffer'):
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True, write_through=True)
    except Exception:
        pass

_log_path = primary_app_dir() / "app.log"
_log_path.parent.mkdir(parents=True, exist_ok=True)
# Overwrite log on each launch so it stays small
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(str(_log_path), mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,
)
logging.info("=== Rom Mate starting ===")
logging.info(f"frozen={getattr(sys, 'frozen', False)}")
logging.info(f"executable={sys.executable}")
logging.info(f"argv={sys.argv}")
logging.info(f"cwd={os.getcwd()}")

from PySide6.QtWidgets import QApplication, QMessageBox, QDialog, QVBoxLayout, QLabel, QProgressBar
from PySide6.QtCore import QTimer, QThread, Signal, Slot, Qt
from src.config import ConfigManager
from src.api import RomMClient
from src.watcher import RomMateWatcher
from src.ui import RomMateMainWindow, SetupDialog

VERSION = "0.7.5"


class LoadingDialog(QDialog):
    def __init__(self):
        super().__init__(None)
        self.setWindowTitle("Rom Mate")
        self.setFixedSize(420, 140)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        self.title = QLabel("Rom Mate")
        self.title.setStyleSheet("font-size: 18px; font-weight: 600; color: #ffffff;")
        layout.addWidget(self.title)

        self.status = QLabel("Starting...")
        self.status.setStyleSheet("font-size: 12px; color: #cccccc;")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        self.progress.setStyleSheet(
            "QProgressBar { background: #2a2a2a; border: 1px solid #3a3a3a; border-radius: 5px; }"
            "QProgressBar::chunk { background: #0d6efd; border-radius: 5px; }"
        )
        layout.addWidget(self.progress)

        self.setStyleSheet("QDialog { background: #1a1a1a; border: 1px solid #333333; border-radius: 10px; }")

    def set_status(self, text: str, pct: int | None = None):
        self.status.setText(text)
        if pct is not None:
            try:
                self.progress.setValue(int(pct))
            except Exception:
                pass


class StartupWorker(QThread):
    progress = Signal(int, str)  # pct, status
    need_setup = Signal()
    error = Signal(str)
    ready = Signal(object, object)  # config, client

    def __init__(self, host: str | None = None, username: str | None = None, password: str | None = None):
        super().__init__()
        self.host = host
        self.username = username
        self.password = password

    def run(self):
        try:
            self.progress.emit(5, "Loading configuration...")
            config = ConfigManager()

            self.progress.emit(15, "Checking configuration migrations...")
            from src.emulators import migrate_old_config
            migrate_old_config(config)

            self.progress.emit(25, "Applying logging settings...")
            log_level_str = config.get("log_level", "INFO").upper()
            log_level = getattr(logging, log_level_str, logging.INFO)
            logging.getLogger().setLevel(log_level)

            if self.host is not None:
                config.set("host", (self.host or "").rstrip('/'))
            if self.username is not None:
                config.set("username", self.username)

            self.progress.emit(35, "Connecting...")
            client = RomMClient(config.get("host"), config=config)

            success = False
            self.progress.emit(55, "Verifying session...")
            if client.token:
                try:
                    if client.fetch_library():
                        success = True
                except Exception:
                    success = False

            if not success:
                self.progress.emit(70, "Authenticating...")
                password = self.password
                if password is None:
                    password = config.get("password")

                if password:
                    ok, _result = client.login(config.get("username"), password)
                    if ok:
                        config.set("password", None)
                        success = True

            if not success:
                self.progress.emit(80, "Setup required...")
                self.need_setup.emit()
                return

            self.progress.emit(100, "Starting...")
            self.ready.emit(config, client)
        except Exception as e:
            self.error.emit(str(e))

def _cleanup_old_mei_folders():
    """Delete stale PyInstaller _MEI temp folders from previous runs."""
    try:
        if not getattr(sys, 'frozen', False):
            return
        import time, shutil
        mei_parent = Path(sys._MEIPASS).parent
        current = Path(sys._MEIPASS).name
        now = time.time()
        for item in mei_parent.iterdir():
            if (item.is_dir() 
                    and item.name.startswith('_MEI')
                    and item.name != current):
                try:
                    # Only delete if older than 60 seconds
                    age = now - item.stat().st_mtime
                    if age > 60:
                        shutil.rmtree(str(item))
                        logging.info(
                            f"[MEI] Cleaned up {item.name} "
                            f"(age={age:.0f}s)")
                except Exception as e:
                    logging.info(f"[MEI] Skip {item}: {e}")
    except Exception as e:
        logging.info(f"[MEI cleanup] Error: {e}")

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Rom Mate")
    app.setOrganizationName("RomMate")
    app.setQuitOnLastWindowClosed(True)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QMessageBox QPushButton,
        QDialogButtonBox QPushButton {
            background: #2d2d2d;
            color: #e6e6e6;
            border: 1px solid #444;
            border-radius: 4px;
            padding: 6px 14px;
            min-height: 24px;
        }
        QMessageBox QPushButton:hover,
        QDialogButtonBox QPushButton:hover {
            background: #3a3a3a;
            border-color: #555;
        }
        QMessageBox QPushButton:pressed,
        QDialogButtonBox QPushButton:pressed {
            background: #242424;
        }
        QMessageBox QPushButton:default {
            border: 1px solid #0d6efd;
        }
        QMessageBox QPushButton:disabled,
        QDialogButtonBox QPushButton:disabled {
            color: #777;
            border-color: #333;
            background: #1f1f1f;
        }
    """)

    splash = LoadingDialog()
    splash.show()
    app.processEvents()

    state = {"worker": None, "window": None}

    def _start_worker(host=None, username=None, password=None):
        if state.get("worker") and state["worker"].isRunning():
            try:
                state["worker"].requestInterruption()
            except Exception:
                pass
        worker = StartupWorker(host=host, username=username, password=password)
        state["worker"] = worker
        worker.progress.connect(lambda pct, msg: splash.set_status(msg, pct))

        def _on_error(msg: str):
            try:
                splash.close()
            except Exception:
                pass
            QMessageBox.critical(None, "Startup Error", msg)
            sys.exit(1)

        def _on_need_setup():
            splash.hide()
            config = ConfigManager()
            setup = SetupDialog(config)
            if setup.exec() == SetupDialog.Accepted:
                data = setup.get_data()
                splash.show()
                app.processEvents()
                _start_worker(host=data["host"], username=data["username"], password=data["password"])
            else:
                sys.exit(0)

        def _on_ready(config, client):
            window = RomMateMainWindow(config, client, RomMateWatcher, VERSION)
            state["window"] = window
            try:
                app.aboutToQuit.connect(window._shutdown_threads)
            except Exception:
                pass
            splash.set_status("Building library view...", 95)

            gate = {"constructed": False, "library_ok": None}

            def _try_show_main_window():
                if not gate["constructed"]:
                    return
                if gate["library_ok"] is None:
                    return
                try:
                    splash.close()
                except Exception:
                    pass
                window.show()
                if gate["library_ok"] is False:
                    QMessageBox.warning(
                        window,
                        "Library Load Failed",
                        "Could not load your library from the server.\n\n"
                        "Check your server settings and try again."
                    )

            def _on_constructed():
                gate["constructed"] = True
                _try_show_main_window()

            def _on_library_ready(ok: bool):
                gate["library_ok"] = bool(ok)
                _try_show_main_window()

            try:
                window.startup_ready.connect(_on_constructed)
            except Exception:
                gate["constructed"] = True

            try:
                window.initial_library_ready.connect(_on_library_ready)
            except Exception:
                gate["library_ok"] = True

            _try_show_main_window()

        worker.error.connect(_on_error)
        worker.need_setup.connect(_on_need_setup)
        worker.ready.connect(_on_ready)
        worker.start()

    _start_worker()
    
    # Delay MEI cleanup to ensure certifi bundle is loaded
    QTimer.singleShot(30000, _cleanup_old_mei_folders)
    
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        logging.info("Calling main()")
        main()
        logging.info("main() returned normally")
    except Exception as e:
        logging.exception(f"FATAL: {e}")
        raise

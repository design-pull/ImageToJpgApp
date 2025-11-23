# app/gui/main_window.py
# ImageToJpgApp - MainWindow (完全版)
# - サフィックスのバリデーションとサニタイズ
# - 起動時にモニター中心で表示（showEventで確実にリサイズ＋中央配置）
# - 長方形サムネイル対応
# 要件: PyQt5, Pillow, app.core.converter, app.utils.logging.setup_logger

from pathlib import Path
from typing import List, Optional, Tuple, Dict
import concurrent.futures
import threading
import time
import traceback
import queue
import os
import re

from PyQt5 import QtWidgets, QtCore, QtGui

from app.core import converter
from app.utils.logging import setup_logger

# ---------- Signals container for worker threads ----------
class WorkerSignals(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, int, str, str, object)  # idx, total, src, dst, error
    finished = QtCore.pyqtSignal(list)  # list of (src, dst, error)
    log = QtCore.pyqtSignal(str)  # textual log lines

# ---------- PoolWorker: manages ThreadPoolExecutor and per-task retry ----------
class PoolWorker(QtCore.QObject):
    def __init__(self, src_items: List[Dict], dst_dir: str, common_options: dict, max_workers: int = 4):
        super().__init__()
        self.signals = WorkerSignals()
        self._stop_event = threading.Event()
        self.src_items = src_items  # list of dicts: {"path": str, "suffix": str, "overwrite": bool}
        self.dst_dir = dst_dir
        self.options = common_options
        self.max_workers = max_workers
        self._futures = []
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None

    def start(self):
        """Starts submission loop. Intended to be invoked from a QThread (via QThread.started)."""
        total = len(self.src_items)
        self.signals.log.emit(f"ワーカープールを開始 (max_workers={self.max_workers})")
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)
        try:
            for idx, item in enumerate(self.src_items, start=1):
                if self._stop_event.is_set():
                    break
                src = item["path"]
                per_opts = dict(self.options)
                per_opts["overwrite"] = item.get("overwrite", per_opts.get("overwrite", False))
                per_opts["retry_attempts"] = per_opts.get("retry_attempts", 3)
                per_opts["backoff_seconds"] = per_opts.get("backoff_seconds", 0.5)
                future = self._executor.submit(self._run_convert, idx, total, src, per_opts, item.get("suffix"))
                self._futures.append(future)
            concurrent.futures.wait(self._futures, return_when=concurrent.futures.ALL_COMPLETED)
        finally:
            if self._executor:
                self._executor.shutdown(wait=False)
            results = []
            for fut in self._futures:
                try:
                    results.append(fut.result())
                except Exception as e:
                    results.append(("", "", str(e)))
            self.signals.finished.emit(results)
            self.signals.log.emit("ワーカープール終了")

    def stop(self):
        self.signals.log.emit("キャンセル要求を受け取りました")
        self._stop_event.set()
        if self._executor:
            self._executor.shutdown(wait=False)

    def _run_convert(self, idx: int, total: int, src: str, opts: dict, suffix: Optional[str]) -> Tuple[str, str, Optional[str]]:
        """
        Call converter.convert_to_jpg with retry logic.
        Returns tuple (src, saved_path_or_empty, error_text_or_None).
        """
        retry_attempts = int(opts.get("retry_attempts", 3))
        base_backoff = float(opts.get("backoff_seconds", 0.5))
        attempt = 0
        last_err_text = None
        src_path = Path(src)
        desired_out_name = None
        try:
            base = src_path.stem
            desired_out_name = f"{base}{(suffix or '')}.jpg"
        except Exception:
            desired_out_name = None

        while attempt < retry_attempts and not self._stop_event.is_set():
            attempt += 1
            try:
                self.signals.log.emit(f"[{idx}/{total}] 変換試行 {attempt}/{retry_attempts} - {src_path.name}")
                saved = converter.convert_to_jpg(
                    src_path=src,
                    dst_dir=str(self.dst_dir),
                    quality=opts.get("quality", 85),
                    background=opts.get("background", (255, 255, 255)),
                    keep_exif=opts.get("keep_exif", False),
                    overwrite=opts.get("overwrite", False),
                )
                saved_path = Path(saved)
                # rename if suffix requested and saved name differs
                if desired_out_name and saved_path.name != desired_out_name:
                    final_dst = Path(self.dst_dir).joinpath(desired_out_name)
                    try:
                        if final_dst.exists() and not opts.get("overwrite", False):
                            i = 1
                            while True:
                                candidate = final_dst.with_name(f"{final_dst.stem}_{i}.jpg")
                                if not candidate.exists():
                                    final_dst = candidate
                                    break
                                i += 1
                        saved_path.rename(final_dst)
                        saved = str(final_dst)
                        self.signals.log.emit(f"リネーム成功: {saved_path.name} -> {final_dst.name}")
                    except Exception as ex_rename:
                        self.signals.log.emit(f"リネーム失敗: {ex_rename}")
                # success
                self.signals.progress.emit(idx, total, src, saved, None)
                self.signals.log.emit(f"変換成功: {src_path.name} -> {Path(saved).name}")
                return (src, saved, None)
            except Exception as e:
                err_text = "".join(traceback.format_exception_only(type(e), e)).strip()
                last_err_text = err_text
                self.signals.log.emit(f"エラー({attempt}/{retry_attempts}): {src_path.name} : {err_text}")
                # fatal error check (example: PermissionError -> no retry)
                if isinstance(e, PermissionError):
                    self.signals.log.emit("致命的エラーのためリトライを中止します")
                    break
                if attempt >= retry_attempts:
                    break
                # exponential backoff + jitter
                backoff = base_backoff * (2 ** (attempt - 1))
                jitter = min(1.0, backoff * 0.1)
                sleep_time = backoff + (jitter * (0.5 - (time.time() % 1)))
                time.sleep(sleep_time)
                continue

        # final failure after retries
        self.signals.progress.emit(idx, total, src, "", last_err_text or "Unknown error")
        self.signals.log.emit(f"変換最終失敗: {src_path.name} : {last_err_text}")
        return (src, "", last_err_text)

# ---------- File row widget (thumbnail + per-item options) ----------
class FileRowWidget(QtWidgets.QWidget):
    MAX_SUFFIX_LEN = 32
    # allowed chars: ASCII letters, digits, hyphen, underscore, dot
    _SUFFIX_RE = re.compile(r'^[A-Za-z0-9_.\-]*$')

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.path = path
        self._build_ui()
        QtCore.QTimer.singleShot(10, self._generate_thumbnail)

    def _build_ui(self):
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(4, 4, 4, 4)
        # thumbnail rectangle (adjustable)
        self.thumb_width = 96
        self.thumb_height = 64
        self.thumb_lbl = QtWidgets.QLabel()
        self.thumb_lbl.setFixedSize(self.thumb_width, self.thumb_height)
        self.thumb_lbl.setScaledContents(False)
        h.addWidget(self.thumb_lbl)
        # name/path
        v = QtWidgets.QVBoxLayout()
        self.lbl_name = QtWidgets.QLabel(Path(self.path).name)
        self.lbl_path = QtWidgets.QLabel(str(Path(self.path).parent))
        self.lbl_path.setStyleSheet("color: gray; font-size: 10pt;")
        v.addWidget(self.lbl_name)
        v.addWidget(self.lbl_path)
        h.addLayout(v)
        # spacer
        h.addStretch()
        # per-item options with validation
        form = QtWidgets.QFormLayout()
        self.edit_suffix = QtWidgets.QLineEdit("")  # e.g. "_v2"
        self.edit_suffix.setPlaceholderText("例: _v2  （英数字 _ - . を使用）")
        self.edit_suffix.textChanged.connect(self._on_suffix_changed)
        self.chk_overwrite = QtWidgets.QCheckBox("上書き")
        form.addRow("サフィックス", self.edit_suffix)
        form.addRow(self.chk_overwrite)
        h.addLayout(form)

    def _on_suffix_changed(self, text: str):
        # validate length
        if len(text) > self.MAX_SUFFIX_LEN:
            self._set_suffix_invalid(f"長すぎます（最大 {self.MAX_SUFFIX_LEN} 文字）")
            return
        # validate allowed chars
        if not self._SUFFIX_RE.match(text):
            self._set_suffix_invalid("使用できない文字が含まれています（許可: 英数字, -, _, .）")
            return
        # valid
        self._set_suffix_valid()

    def _set_suffix_invalid(self, reason: str):
        self.edit_suffix.setStyleSheet("border: 1px solid #d9534f;")  # red border
        self.edit_suffix.setToolTip(reason)

    def _set_suffix_valid(self):
        self.edit_suffix.setStyleSheet("")  # reset
        self.edit_suffix.setToolTip("")

    def sanitize_suffix(self, text: str) -> str:
        """Return sanitized suffix, removing disallowed chars and trimming length."""
        # keep allowed characters only
        sanitized_chars = [ch for ch in text if self._SUFFIX_RE.match(ch)]
        sanitized = "".join(sanitized_chars)
        if len(sanitized) > self.MAX_SUFFIX_LEN:
            sanitized = sanitized[: self.MAX_SUFFIX_LEN]
        return sanitized

    def _generate_thumbnail(self):
        """Create a rectangular thumbnail with preserved aspect ratio and background fill."""
        try:
            from PIL import Image, ImageOps
            img = Image.open(self.path)
            # target rectangle
            tw, th = self.thumb_width, self.thumb_height
            img = ImageOps.exif_transpose(img)
            img.thumbnail((tw, th), Image.LANCZOS)
            # Create background and paste centered
            bg = Image.new("RGBA", (tw, th), (240, 240, 240, 255))
            x = (tw - img.width) // 2
            y = (th - img.height) // 2
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            bg.paste(img, (x, y), img if "A" in img.mode else None)
            data = bg.tobytes("raw", "RGBA")
            qimg = QtGui.QImage(data, bg.width, bg.height, QtGui.QImage.Format_RGBA8888)
            pix = QtGui.QPixmap.fromImage(qimg)
            self.thumb_lbl.setPixmap(pix)
            try:
                img.close()
            except Exception:
                pass
            try:
                bg.close()
            except Exception:
                pass
        except Exception:
            icon = self.style().standardIcon(QtWidgets.QStyle.SP_FileIcon)
            pix = icon.pixmap(self.thumb_width, self.thumb_height)
            self.thumb_lbl.setPixmap(pix)

    def get_options(self) -> Dict:
        # sanitize suffix before returning options
        raw_suffix = self.edit_suffix.text().strip()
        safe_suffix = self.sanitize_suffix(raw_suffix)
        if safe_suffix != raw_suffix:
            # reflect sanitized value back to UI (non-blocking)
            self.edit_suffix.setText(safe_suffix)
        return {
            "path": self.path,
            "suffix": safe_suffix,
            "overwrite": self.chk_overwrite.isChecked(),
        }

# ---------- MainWindow ----------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ImageToJpgApp (詳細版)")
        self._worker_thread: Optional[QtCore.QThread] = None
        self._pool_worker: Optional[PoolWorker] = None
        self._ui_log_queue: Optional[queue.Queue] = None
        self._ui_logger_thread: Optional[threading.Thread] = None
        self.logger = None
        self._first_show = True
        self._build_ui()
        self._connect_signals()
        # 初期ウィンドウサイズ指定（showEventで中央化されます）
        self.resize(1100, 673)

    def showEvent(self, event):
        """
        初回表示時に一度だけ
        - ウィンドウを 1100x673 にリサイズ（確実に適用）
        - レイアウトをフラッシュしてフレームジオメトリを確定
        - ウィンドウの属するスクリーン（画面）を取得して中央に移動
        """
        super().showEvent(event)
        if not getattr(self, "_first_show", False):
            return

        self._first_show = False

        # 1) 強制リサイズ（ここで指定サイズを確定させる）
        try:
            self.resize(1100, 673)
        except Exception:
            pass

        # 2) レイアウトを反映させる（ジオメトリ確定のため）
        QtCore.QCoreApplication.processEvents()

        # 3) 中央配置
        screen = QtWidgets.QApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = QtWidgets.QApplication.primaryScreen()

        if screen:
            screen_geo = screen.availableGeometry()
            win_geo = self.frameGeometry()
            x = screen_geo.x() + (screen_geo.width() - win_geo.width()) // 2
            y = screen_geo.y() + (screen_geo.height() - win_geo.height()) // 2
            self.move(max(x, 0), max(y, 0))
        else:
            desktop = QtWidgets.QApplication.desktop()
            try:
                center = desktop.screen().rect().center()
                self.move(center - self.rect().center())
            except Exception:
                pass

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_v = QtWidgets.QVBoxLayout(central)

        # toolbar
        toolbar = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("追加")
        self.btn_remove = QtWidgets.QPushButton("選択行を削除")
        self.btn_clear = QtWidgets.QPushButton("全クリア")
        toolbar.addWidget(self.btn_add)
        toolbar.addWidget(self.btn_remove)
        toolbar.addWidget(self.btn_clear)
        toolbar.addStretch()
        main_v.addLayout(toolbar)

        # splitter
        splitter = QtWidgets.QSplitter()
        main_v.addWidget(splitter)

        # left: list
        left_widget = QtWidgets.QWidget()
        left_v = QtWidgets.QVBoxLayout(left_widget)
        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list_widget.setDragDropMode(QtWidgets.QAbstractItemView.DropOnly)
        left_v.addWidget(self.list_widget)
        splitter.addWidget(left_widget)

        # right: settings + log
        right_widget = QtWidgets.QWidget()
        right_v = QtWidgets.QVBoxLayout(right_widget)

        out_h = QtWidgets.QHBoxLayout()
        out_h.addWidget(QtWidgets.QLabel("出力先"))
        self.out_edit = QtWidgets.QLineEdit(str(Path.home()))
        self.btn_browse = QtWidgets.QPushButton("参照")
        out_h.addWidget(self.out_edit)
        out_h.addWidget(self.btn_browse)
        right_v.addLayout(out_h)

        form = QtWidgets.QFormLayout()
        self.spin_quality = QtWidgets.QSpinBox()
        self.spin_quality.setRange(1, 95)
        self.spin_quality.setValue(85)
        self.btn_bgcolor = QtWidgets.QPushButton("背景色選択")
        self.lbl_bg = QtWidgets.QLabel("#ffffff")
        self.chk_overwrite_all = QtWidgets.QCheckBox("全て上書き")
        self.spin_workers = QtWidgets.QSpinBox()
        self.spin_workers.setRange(1, 16)
        self.spin_workers.setValue(4)
        self.spin_retry = QtWidgets.QSpinBox()
        self.spin_retry.setRange(0, 10)
        self.spin_retry.setValue(3)
        self.spin_backoff = QtWidgets.QDoubleSpinBox()
        self.spin_backoff.setRange(0.0, 10.0)
        self.spin_backoff.setSingleStep(0.1)
        self.spin_backoff.setValue(0.5)

        form.addRow("品質", self.spin_quality)
        row_bg = QtWidgets.QHBoxLayout()
        row_bg.addWidget(self.btn_bgcolor)
        row_bg.addWidget(self.lbl_bg)
        form.addRow("透過合成色", row_bg)
        form.addRow(self.chk_overwrite_all)
        form.addRow("並列ワーカー数", self.spin_workers)
        form.addRow("リトライ回数", self.spin_retry)
        form.addRow("バックオフ秒数", self.spin_backoff)
        right_v.addLayout(form)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setMinimum(0)
        self.progress.setValue(0)
        right_v.addWidget(self.progress)

        right_v.addWidget(QtWidgets.QLabel("ログ"))
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        right_v.addWidget(self.log_view)

        action_h = QtWidgets.QHBoxLayout()
        self.btn_convert = QtWidgets.QPushButton("変換開始")
        self.btn_cancel = QtWidgets.QPushButton("キャンセル")
        self.btn_cancel.setEnabled(False)
        action_h.addStretch()
        action_h.addWidget(self.btn_convert)
        action_h.addWidget(self.btn_cancel)
        right_v.addLayout(action_h)

        splitter.addWidget(right_widget)
        splitter.setSizes([600, 300])

        # drag & drop
        self.list_widget.viewport().setAcceptDrops(True)
        self.list_widget.installEventFilter(self)

    def _connect_signals(self):
        self.btn_add.clicked.connect(self.on_add_files)
        self.btn_remove.clicked.connect(self.on_remove_files)
        self.btn_clear.clicked.connect(self.on_clear)
        self.btn_browse.clicked.connect(self.on_browse)
        self.btn_bgcolor.clicked.connect(self.on_select_bg)
        self.btn_convert.clicked.connect(self.on_start)
        self.btn_cancel.clicked.connect(self.on_cancel)
        self.list_widget.itemDoubleClicked.connect(self.on_item_double_clicked)

    # ---------- File list helpers ----------
    def add_file_row(self, path: str):
        for i in range(self.list_widget.count()):
            if self.list_widget.item(i).data(QtCore.Qt.UserRole) == path:
                return
        item = QtWidgets.QListWidgetItem()
        item.setSizeHint(QtCore.QSize(520, 88))
        item.setData(QtCore.Qt.UserRole, path)
        widget = FileRowWidget(path)
        self.list_widget.addItem(item)
        self.list_widget.setItemWidget(item, widget)

    def on_add_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "画像を選択", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.gif *.tif *.tiff *.psd *.svg *.webp *.heic *.raw *.cr2 *.nef *.arw)"
        )
        for f in files:
            self.add_file_row(f)

    def on_remove_files(self):
        for item in self.list_widget.selectedItems():
            row = self.list_widget.row(item)
            self.list_widget.takeItem(row)

    def on_clear(self):
        self.list_widget.clear()

    def on_browse(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "出力フォルダを選択", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def on_select_bg(self):
        col = QtWidgets.QColorDialog.getColor(QtGui.QColor(255, 255, 255), self, "背景色を選択")
        if col.isValid():
            self.lbl_bg.setText(col.name())

    def on_item_double_clicked(self, item: QtWidgets.QListWidgetItem):
        pass

    def eventFilter(self, obj, event):
        if obj is self.list_widget and event.type() == QtCore.QEvent.Drop:
            mime = event.mime()
            if mime.hasUrls():
                for u in mime.urls():
                    path = u.toLocalFile()
                    if path:
                        self.add_file_row(path)
            return True
        return super().eventFilter(obj, event)

    # ---------- Start / Cancel ----------
    def on_start(self):
        # Sanitize suffixes across the list before collection
        fixed_count = 0
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            widget = self.list_widget.itemWidget(item)
            if hasattr(widget, "sanitize_suffix"):
                raw = widget.edit_suffix.text().strip()
                cleaned = widget.sanitize_suffix(raw)
                if cleaned != raw:
                    widget.edit_suffix.setText(cleaned)
                    fixed_count += 1
                    self.append_log(f"サフィックスを自動修正: {raw} -> {cleaned}")
        if fixed_count:
            self.append_log(f"{fixed_count} 件のサフィックスを自動修正しました")

        src_items = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            widget = self.list_widget.itemWidget(item)
            opts = widget.get_options()
            src_items.append(opts)
        if not src_items:
            QtWidgets.QMessageBox.warning(self, "警告", "変換するファイルを追加してください")
            return

        dst_dir = self.out_edit.text() or str(Path.home())
        try:
            Path(dst_dir).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "エラー", f"出力フォルダを作成できません: {e}")
            return

        # Prepare logger and UI queue
        log_path = os.path.join(dst_dir, "ImageToJpgApp.log")
        self._ui_log_queue = queue.Queue()
        self.logger = setup_logger("ImageToJpgApp", logfile=log_path, ui_queue=self._ui_log_queue)

        def _ui_log_poller():
            while getattr(self, "_pool_worker", None) is not None or not self._ui_log_queue.empty():
                try:
                    msg = self._ui_log_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                QtCore.QMetaObject.invokeMethod(self, "append_log", QtCore.Qt.QueuedConnection, QtCore.Q_ARG(str, msg))
        self._ui_logger_thread = threading.Thread(target=_ui_log_poller, daemon=True)
        self._ui_logger_thread.start()

        bg_color = QtGui.QColor(self.lbl_bg.text()) if QtGui.QColor(self.lbl_bg.text()).isValid() else QtGui.QColor(255, 255, 255)
        background = (bg_color.red(), bg_color.green(), bg_color.blue())
        common_opts = {
            "quality": self.spin_quality.value(),
            "background": background,
            "keep_exif": False,
            "overwrite": self.chk_overwrite_all.isChecked(),
            "retry_attempts": self.spin_retry.value(),
            "backoff_seconds": float(self.spin_backoff.value()),
        }

        # UI state
        self.btn_convert.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        total = len(src_items)
        self.progress.setMaximum(total)
        self.progress.setValue(0)
        self.log_view.clear()
        self.append_log(f"変換開始: {total} 件, 出力: {dst_dir}, workers={self.spin_workers.value()}")

        # Setup PoolWorker in QThread
        self._pool_worker = PoolWorker(src_items=src_items, dst_dir=dst_dir, common_options=common_opts, max_workers=self.spin_workers.value())
        self._worker_thread = QtCore.QThread()
        self._pool_worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._pool_worker.start)
        self._pool_worker.signals.progress.connect(self._on_progress)
        self._pool_worker.signals.finished.connect(self._on_finished)
        self._pool_worker.signals.log.connect(self.append_log)
        # also forward logs to file via logger
        self._pool_worker.signals.log.connect(lambda s: self.logger.debug(s) if self.logger else None)
        self._worker_thread.start()

    def on_cancel(self):
        if self._pool_worker:
            self._pool_worker.stop()
            self.append_log("キャンセル要求を送信しました")
            self.btn_cancel.setEnabled(False)

    # ---------- Slots ----------
    def _on_progress(self, idx, total, src, dst, error):
        self.progress.setValue(idx)
        if error:
            self.append_log(f"失敗: {Path(src).name} : {error}")
            if self.logger:
                self.logger.warning(f"失敗: {Path(src).name} : {error}")
        else:
            self.append_log(f"{idx}/{total} 完了: {Path(src).name} -> {Path(dst).name}")
            if self.logger:
                self.logger.info(f"{idx}/{total} 完了: {Path(src).name} -> {Path(dst).name}")

    def _on_finished(self, results):
        self.append_log("全タスク終了")
        if self._worker_thread:
            self._worker_thread.quit()
            self._worker_thread.wait(2000)
            self._worker_thread = None
        self._pool_worker = None
        self.btn_convert.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        errors = [r for r in results if r[2]]
        if errors:
            msg = "\n".join(f"{Path(e[0]).name}: {e[2]}" for e in errors[:10])
            QtWidgets.QMessageBox.warning(self, "一部失敗", f"一部ファイルの変換に失敗しました:\n{msg}")
            if self.logger:
                self.logger.warning("一部ファイルの変換に失敗しました")
        else:
            QtWidgets.QMessageBox.information(self, "完了", "すべてのファイルを変換しました")
            if self.logger:
                self.logger.info("すべてのファイルを変換しました")

    @QtCore.pyqtSlot(str)
    def append_log(self, text: str):
        self.log_view.appendPlainText(text)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

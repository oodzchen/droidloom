"""Diagnostics, Android logs, crashes, droidloom-doctor, and systemd journal viewer page."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ..backend.control_client import ControlClient
from ..backend.host_runtime import read_systemd_journal, run_doctor
from .widgets import (
    WorkerThread,
    WrappingLabel,
    make_section_card,
    set_button_role,
    show_message,
    spawn_worker,
    style_table,
    wrap_resizable,
)


class DiagnosticsPage(QtWidgets.QWidget):
    """Android logcat, crash buffer, host environment doctor, and systemd logs."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = ControlClient()
        self._workers: list[WorkerThread] = []

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(14)

        # 1. Host Environment Doctor
        doc_box, doc_lay = make_section_card("体检")
        doc_bar = QtWidgets.QHBoxLayout()
        doc_bar.setSpacing(10)
        run_doc_btn = set_button_role(QtWidgets.QPushButton("运行"), "primary")
        run_doc_btn.clicked.connect(self._run_doctor)
        self.doc_summary = WrappingLabel("就绪", muted=True)
        doc_bar.addWidget(run_doc_btn)
        doc_bar.addWidget(self.doc_summary, 1, QtCore.Qt.AlignmentFlag.AlignVCenter)
        doc_lay.addLayout(doc_bar)

        self.doc_table = QtWidgets.QTableWidget(0, 3)
        self.doc_table.setHorizontalHeaderLabels(["检查项", "状态", "详情"])
        self.doc_table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        style_table(self.doc_table)
        doc_lay.addWidget(wrap_resizable(self.doc_table, default_height=200, min_height=100))
        root.addWidget(doc_box)

        # 2. Android Logs & Crash Buffer + Systemd Journal
        log_box, log_lay = make_section_card("日志")
        ctrl = QtWidgets.QHBoxLayout()
        ctrl.setSpacing(8)
        self.source_combo = QtWidgets.QComboBox()
        self.source_combo.addItem("Logcat", "logs")
        self.source_combo.addItem("崩溃", "crashes")
        self.source_combo.addItem("droidloomd", "sys_journal")
        self.source_combo.addItem("droidloom", "user_journal")

        self.pkg_edit = QtWidgets.QLineEdit()
        self.pkg_edit.setPlaceholderText("包名过滤")
        self.lines_spin = QtWidgets.QSpinBox()
        self.lines_spin.setRange(50, 2000)
        self.lines_spin.setValue(300)
        self.lines_spin.setSuffix(" 行")

        fetch_btn = set_button_role(QtWidgets.QPushButton("获取"), "primary")
        export_btn = QtWidgets.QPushButton("导出...")
        fetch_btn.clicked.connect(self._fetch_logs)
        export_btn.clicked.connect(self._export_logs)

        ctrl.addWidget(self.source_combo)
        ctrl.addWidget(self.pkg_edit, 1)
        ctrl.addWidget(self.lines_spin)
        ctrl.addWidget(fetch_btn)
        ctrl.addWidget(export_btn)
        log_lay.addLayout(ctrl)

        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("在已拉取的日志中关键字过滤...")
        self.filter_edit.textChanged.connect(self._apply_text_filter)
        log_lay.addWidget(self.filter_edit)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("font-family: monospace; font-size: 12px;")
        log_lay.addWidget(wrap_resizable(self.log_view, default_height=340, min_height=140))
        root.addWidget(log_box)
        root.addStretch(1)

        self._raw_log_text = ""

    def _spawn(self, fn, on_ok, on_err=None):
        spawn_worker(self, self._workers, fn, on_ok, on_err)

    def _run_doctor(self) -> None:
        self.doc_summary.setText("体检中...")
        self._spawn(run_doctor, self._on_doctor_done)

    def _on_doctor_done(self, report: dict) -> None:
        if "error" in report and "checks" not in report:
            self.doc_summary.setText(f"体检失败: {report['error']}")
            return
        checks = report.get("checks") or []
        self.doc_table.setRowCount(len(checks))
        passed = 0
        for r, chk in enumerate(checks):
            name = str(chk.get("name") or chk.get("id") or f"check-{r}")
            status = str(chk.get("status") or chk.get("outcome") or "unknown")
            detail = str(chk.get("message") or chk.get("detail") or chk.get("summary") or "")
            if status.lower() in ("pass", "ok", "satisfied"):
                passed += 1
            self.doc_table.setItem(r, 0, QtWidgets.QTableWidgetItem(name))
            self.doc_table.setItem(r, 1, QtWidgets.QTableWidgetItem(status))
            self.doc_table.setItem(r, 2, QtWidgets.QTableWidgetItem(detail))
        overall = report.get("overall") or report.get("status") or ""
        self.doc_summary.setText(f"总结果: {overall} ({passed}/{len(checks)} 项通过)")

    def _fetch_logs(self) -> None:
        src = str(self.source_combo.currentData())
        pkg = self.pkg_edit.text().strip() or None
        lines = self.lines_spin.value()
        self.log_view.setPlainText("正在读取日志...")

        def task():
            if src == "logs":
                return self.client.diagnostics(package=pkg, lines=lines, crashes=False)
            if src == "crashes":
                return self.client.diagnostics(package=pkg, lines=lines, crashes=True)
            if src == "sys_journal":
                return read_systemd_journal(system_unit="droidloomd.service", lines=lines)
            return read_systemd_journal(user_unit="droidloom.service", lines=lines)

        def done(txt: str):
            self._raw_log_text = txt
            self._apply_text_filter()

        self._spawn(
            task,
            done,
            lambda e: self.log_view.setPlainText(f"读取失败: {e}"),
        )

    def _apply_text_filter(self) -> None:
        needle = self.filter_edit.text().strip().lower()
        if not needle:
            self.log_view.setPlainText(self._raw_log_text)
            return
        filtered = [
            line for line in self._raw_log_text.splitlines() if needle in line.lower()
        ]
        self.log_view.setPlainText("\n".join(filtered))

    def _export_logs(self) -> None:
        if not self._raw_log_text:
            show_message(self, "导出日志", "当前没有可导出的日志内容。")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出日志文件", "droidloom-logs.txt", "Text Files (*.txt)"
        )
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._raw_log_text)
            show_message(self, "导出完成", f"日志已保存到 {path}")

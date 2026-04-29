from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QScrollArea,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTextEdit,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .workers.device_worker import DeviceWorker
from .workers.hook_worker import HookWorker
from .workers.action_worker import ActionWorker
from .workers.workspace_worker import WorkspaceWorker


@dataclass
class MainWindowDependencies:
    # GUI 依赖注入容器。
    # 这里不直接 new service，而是由外层统一装配后注入进来，
    # 这样后续更容易做测试、替换实现，或者把同一套 service 同时给 CLI/GUI 使用。
    device_service: Any
    session_service: Any
    workspace_service: Any
    rpc_service: Any
    context: Any


@dataclass
class LogRecord:
    # GUI 内部使用的日志记录模型。
    #
    # 这里把“原始文本”“分类”“级别”拆开保存，
    # 后续过滤、着色、导出都围绕这份结构化数据工作。
    message: str
    category: str
    level: str


class MainWindow(QMainWindow):
    # 第一版 PySide6 主窗口。
    #
    # 目标不是一次性覆盖 CLI 的全部能力，而是先把这几个高频流程跑通：
    # 1. 选择脚本目录并显示脚本
    # 2. 准备设备环境并刷新 App 列表
    # 3. 选择目标 App
    # 4. 选择 attach / spawn 模式开始注入
    # 5. 停止当前 Hook
    # 6. 在右侧面板实时查看日志
    log_emitted = Signal(str)
    MAX_LOG_RECORDS = 3000

    def __init__(self, deps: MainWindowDependencies) -> None:
        super().__init__()
        self.deps = deps
        self.log_records: list[LogRecord] = []
        self.log_file_path: Path | None = None
        self.result_windows: list[QDialog] = []

        # 这两个线程分别服务于两个耗时场景：
        # 1. device_thread：准备环境、刷新应用列表
        # 2. hook_thread：切换 App、准备工作区、发起注入
        #
        # 线程本身只负责启动动作，不长期持有 Hook 会话。
        self.device_thread: QThread | None = None
        self.device_worker: DeviceWorker | None = None
        self.workspace_thread: QThread | None = None
        self.workspace_worker: WorkspaceWorker | None = None
        self.hook_thread: QThread | None = None
        self.hook_worker: HookWorker | None = None
        self.action_thread: QThread | None = None
        self.action_worker: ActionWorker | None = None

        # GUI 层接管日志出口：所有 service 的日志最终都通过这个信号回到主线程显示。
        self.deps.context.log_handler = self._handle_log_from_worker
        self.log_emitted.connect(self.append_log)

        self.script_root = self.deps.context.js_dir

        self.setWindowTitle("Hookers GUI 工作台")
        self.resize(1500, 920)
        self._build_ui()
        self._apply_styles()
        self.update_script_root_display()
        self.refresh_script_list()
        self.refresh_app_status_panel()

    def _build_ui(self) -> None:
        # 主体布局采用左右三栏：
        # 左：脚本选择
        # 中：App/模式/动作控制
        # 右：实时日志
        root = QWidget(self)
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(8)
        self.splitter.setOpaqueResize(False)
        layout.addWidget(self.splitter)

        self.script_panel = self._build_script_panel()
        self.control_panel = self._build_control_panel()
        self.log_panel = self._build_log_panel()

        self.script_scroll = self._build_scroll_container(self.script_panel)
        self.control_scroll = self._build_scroll_container(self.control_panel)

        self.script_scroll.setMinimumWidth(300)
        self.script_scroll.setMaximumWidth(380)
        self.control_scroll.setMinimumWidth(360)
        self.control_scroll.setMaximumWidth(500)
        self.log_panel.setMinimumWidth(620)

        self.script_scroll.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.control_scroll.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.log_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.splitter.addWidget(self.script_scroll)
        self.splitter.addWidget(self.control_scroll)
        self.splitter.addWidget(self.log_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setStretchFactor(2, 1)
        self.splitter.setSizes([340, 420, 920])

        self.status_bar = QStatusBar(self)
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("GUI 已就绪，先点击“准备环境并刷新 App”")

    def _build_scroll_container(self, widget: QWidget) -> QScrollArea:
        # 左侧和中间区都放进滚动容器里，避免窗口尺寸偏小时内容被遮挡。
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(widget)
        return scroll

    def _with_dropdown_marker(self, combo: QComboBox) -> QWidget:
        # 某些主题下 QComboBox 原生箭头不明显，这里额外补一个稳定可见的下拉标识。
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(combo, 1)

        marker = QLabel("▼")
        marker.setObjectName("comboMarker")
        marker.setAlignment(Qt.AlignCenter)
        marker.setMinimumWidth(18)
        layout.addWidget(marker)
        return container

    def _build_section_divider(self, title: str) -> QWidget:
        # 给中间工具区加轻量分组分隔，让不同功能块更容易扫视。
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(8)

        left_line = QFrame()
        left_line.setFrameShape(QFrame.HLine)
        left_line.setObjectName("sectionLine")
        layout.addWidget(left_line, 1)

        label = QLabel(title)
        label.setObjectName("sectionCaption")
        layout.addWidget(label)

        right_line = QFrame()
        right_line.setFrameShape(QFrame.HLine)
        right_line.setObjectName("sectionLine")
        layout.addWidget(right_line, 1)
        return container

    def _build_script_panel(self) -> QWidget:
        # 左侧脚本面板。
        # 这里允许用户切换脚本目录，所以 GUI 不强制绑定到项目内置 js/。
        panel = QWidget()
        panel.setObjectName("sidePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("脚本库 (.js)")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        self.script_dir_input = QLineEdit()
        self.script_dir_input.setReadOnly(True)
        layout.addWidget(self.script_dir_input)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.select_script_dir_button = QPushButton("选择脚本文件夹")
        self.select_script_dir_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.select_script_dir_button.clicked.connect(self.choose_script_directory)
        button_row.addWidget(self.select_script_dir_button)

        self.reload_script_dir_button = QPushButton("刷新脚本")
        self.reload_script_dir_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.reload_script_dir_button.clicked.connect(self.refresh_script_list)
        button_row.addWidget(self.reload_script_dir_button)
        layout.addLayout(button_row)

        self.selected_script_label = QLabel("当前脚本：未选择")
        self.selected_script_label.setWordWrap(True)
        self.selected_script_label.setMinimumHeight(48)
        layout.addWidget(self.selected_script_label)

        script_combo_label = QLabel("脚本文件选择")
        script_combo_label.setObjectName("mutedLabel")
        layout.addWidget(script_combo_label)

        self.script_combo = QComboBox()
        self.script_combo.setEditable(False)
        self.script_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.script_combo.currentIndexChanged.connect(self._update_selected_script_tip)
        layout.addWidget(self._with_dropdown_marker(self.script_combo))

        app_status_title = QLabel("App 状态")
        app_status_title.setObjectName("panelTitle")
        layout.addWidget(app_status_title)

        self.left_pid_uid_status_value = QLabel("PID: - | UID: -")
        self.left_pid_uid_status_value.setWordWrap(True)
        self.left_pid_uid_status_value.setObjectName("statusValue")
        layout.addWidget(self.left_pid_uid_status_value)

        self.left_version_mode_status_value = QLabel("Version: - | 模式: 未启动")
        self.left_version_mode_status_value.setWordWrap(True)
        self.left_version_mode_status_value.setObjectName("statusValue")
        layout.addWidget(self.left_version_mode_status_value)

        layout.addStretch(1)
        return panel

    def _build_control_panel(self) -> QWidget:
        # 中间控制区。
        # 这块主要承载“准备环境 -> 选择 App -> 选择模式 -> 启动/停止”的主操作流。
        panel = QWidget()
        panel.setObjectName("controlPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        title = QLabel("控制台")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(10)
        layout.addLayout(form)

        env_label = QLabel("脚本来源")
        self.script_source_hint = QLabel("当前目录")
        self.script_source_hint.setObjectName("mutedLabel")
        form.addWidget(env_label, 0, 0)
        form.addWidget(self.script_source_hint, 0, 1)

        self.refresh_apps_button = QPushButton("准备环境并刷新 App")
        self.refresh_apps_button.clicked.connect(self.start_device_prepare)
        form.addWidget(self.refresh_apps_button, 1, 0, 1, 2)

        app_label = QLabel("目标 App")
        self.app_combo = QComboBox()
        self.app_combo.setEditable(False)
        self.app_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.app_combo.currentIndexChanged.connect(self.on_package_changed)
        form.addWidget(app_label, 2, 0)
        form.addWidget(self._with_dropdown_marker(self.app_combo), 2, 1)

        workspace_label = QLabel("工作目录")
        self.workspace_path_input = QLineEdit()
        self.workspace_path_input.setReadOnly(True)
        self.workspace_path_input.setPlaceholderText(
            "选择 App 后自动创建并显示 workspaces/<package>/ 工作目录"
        )
        form.addWidget(workspace_label, 3, 0)
        form.addWidget(self.workspace_path_input, 3, 1)

        self.prepare_workspace_button = QPushButton("初始化工作目录并拉取 APK")
        self.prepare_workspace_button.clicked.connect(self.prepare_selected_workspace)
        self.prepare_workspace_button.setDisabled(True)
        form.addWidget(self.prepare_workspace_button, 4, 0, 1, 2)

        mode_label = QLabel("注入模式")
        mode_row = QHBoxLayout()
        mode_row.setSpacing(18)
        self.attach_mode_radio = QRadioButton("Attach 模式")
        self.spawn_mode_radio = QRadioButton("Spawn 模式 (-f)")
        self.attach_mode_radio.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_group.addButton(self.attach_mode_radio)
        self.mode_group.addButton(self.spawn_mode_radio)
        mode_row.addWidget(self.attach_mode_radio)
        mode_row.addWidget(self.spawn_mode_radio)
        mode_row.addStretch(1)
        form.addWidget(mode_label, 5, 0)
        form.addLayout(mode_row, 5, 1)

        # 调试工具区把 CLI 里已有的高频分析能力接到 GUI。
        debug_title = QLabel("调试工具")
        debug_title.setObjectName("panelTitle")
        layout.addWidget(debug_title)

        self.hook_target_input = QLineEdit()
        self.hook_target_input.setPlaceholderText("输入类名或类名:方法，例如 com.demo.Test:onCreate")

        self.generate_hook_button = QPushButton("生成 Hook 脚本")
        self.generate_hook_button.clicked.connect(self.generate_hook_script)

        self.view_activity_button = QPushButton("查看 Activity")
        self.view_activity_button.clicked.connect(self.show_activities)

        self.view_service_button = QPushButton("查看 Service")
        self.view_service_button.clicked.connect(self.show_services)

        self.restart_app_button = QPushButton("重启 App")
        self.restart_app_button.clicked.connect(self.restart_current_app)

        # 对象分析区承接 object_info / object_to_explain / view_info。
        self.inspect_target_input = QLineEdit()
        self.inspect_target_input.setPlaceholderText("输入对象 ID、类名或 View ID")

        self.object_info_button = QPushButton("对象信息")
        self.object_info_button.clicked.connect(self.show_object_info)

        self.object_explain_button = QPushButton("对象解释")
        self.object_explain_button.clicked.connect(self.show_object_explain)

        self.view_info_button = QPushButton("View 信息")
        self.view_info_button.clicked.connect(self.show_view_info)

        # 重新收编调试工具布局：
        # 1. 需要输入的功能，输入框和按钮上下放在一起
        # 2. 不需要输入的功能，按钮集中成一组
        hook_group = QWidget()
        hook_group_layout = QVBoxLayout(hook_group)
        hook_group_layout.setContentsMargins(0, 0, 0, 0)
        hook_group_layout.setSpacing(8)
        layout.addWidget(hook_group)

        hook_group_layout.addWidget(self._build_section_divider("脚本生成"))
        hook_group_layout.addWidget(self.hook_target_input)
        hook_group_layout.addWidget(self.generate_hook_button)

        inspect_group = QWidget()
        inspect_group_layout = QVBoxLayout(inspect_group)
        inspect_group_layout.setContentsMargins(0, 0, 0, 0)
        inspect_group_layout.setSpacing(8)
        layout.addWidget(inspect_group)

        inspect_group_layout.addWidget(self._build_section_divider("对象分析"))
        inspect_group_layout.addWidget(self.inspect_target_input)

        inspect_button_row = QGridLayout()
        inspect_button_row.setHorizontalSpacing(10)
        inspect_button_row.setVerticalSpacing(10)
        inspect_group_layout.addLayout(inspect_button_row)
        inspect_button_row.addWidget(self.object_info_button, 0, 0)
        inspect_button_row.addWidget(self.object_explain_button, 0, 1)
        inspect_button_row.addWidget(self.view_info_button, 1, 0, 1, 2)

        layout.addWidget(self._build_section_divider("页面查询"))

        no_input_group = QWidget()
        no_input_layout = QGridLayout(no_input_group)
        no_input_layout.setContentsMargins(0, 0, 0, 0)
        no_input_layout.setHorizontalSpacing(10)
        no_input_layout.setVerticalSpacing(10)
        layout.addWidget(no_input_group)
        no_input_layout.addWidget(self.view_activity_button, 0, 0)
        no_input_layout.addWidget(self.view_service_button, 0, 1)
        no_input_layout.addWidget(self.restart_app_button, 1, 0, 1, 2)

        layout.addWidget(self._build_section_divider("应用控制"))

        self.start_hook_button = QPushButton("开始注入")
        self.start_hook_button.setObjectName("primaryButton")
        self.start_hook_button.clicked.connect(self.start_hook)
        layout.addWidget(self.start_hook_button)

        self.stop_hook_button = QPushButton("停止 Hook")
        self.stop_hook_button.setObjectName("dangerButton")
        self.stop_hook_button.clicked.connect(self.stop_hook)
        self.stop_hook_button.setDisabled(True)
        layout.addWidget(self.stop_hook_button)

        self.current_state_label = QLabel("状态：空闲")
        self.current_state_label.setObjectName("stateLabel")
        self.current_state_label.setWordWrap(True)
        self.current_state_label.setMinimumHeight(54)
        layout.addWidget(self.current_state_label)

        layout.addStretch(1)
        return panel

    def _build_log_panel(self) -> QWidget:
        # 右侧日志区。
        # 所有 service 最终都走 context.log_handler -> log_emitted -> append_log
        # 这条链路，把后台工作线程里的文本安全地送回主线程显示。
        panel = QWidget()
        panel.setObjectName("logPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("运行日志")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        tools_row = QHBoxLayout()
        self.log_filter_combo = QComboBox()
        self.log_filter_combo.addItems(["全部日志", "只看 [JS]", "只看错误"])
        self.log_filter_combo.addItem("只看调试工具")
        self.log_filter_combo.currentIndexChanged.connect(self.render_logs)
        tools_row.addWidget(self._with_dropdown_marker(self.log_filter_combo), 1)

        self.choose_log_file_button = QPushButton("选择日志文件")
        self.choose_log_file_button.clicked.connect(self.choose_log_file)
        tools_row.addWidget(self.choose_log_file_button)

        self.clear_log_button = QPushButton("清空显示")
        self.clear_log_button.clicked.connect(self.clear_logs)
        tools_row.addWidget(self.clear_log_button)
        layout.addLayout(tools_row)

        self.log_file_input = QLineEdit()
        self.log_file_input.setReadOnly(True)
        self.log_file_input.setPlaceholderText("未启用日志文件保存")
        layout.addWidget(self.log_file_input)

        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        layout.addWidget(self.log_console, 1)
        return panel

    def _apply_styles(self) -> None:
        # 第一版风格偏“桌面工作台”，不是默认控件堆叠。
        # 左中区域用暖色浅底，右侧日志保持黑底高对比，便于长时间盯日志。
        self.setStyleSheet(
            """
            QWidget {
                background: #f5f4ef;
                color: #2e2a24;
                font-size: 14px;
            }
            QMainWindow {
                background: #f5f4ef;
            }
            QScrollArea {
                border: none;
                background: transparent;
            }
            QWidget#sidePanel, QWidget#controlPanel {
                background: #f8f5ef;
                border: 1px solid #e2d6c5;
                border-radius: 16px;
            }
            QWidget#logPanel {
                background: #f8f5ef;
                border: 1px solid #d7d0c4;
                border-radius: 16px;
            }
            QLabel#panelTitle {
                font-size: 20px;
                font-weight: 700;
                color: #574633;
                padding-bottom: 6px;
            }
            QLabel#mutedLabel {
                color: #8c7a65;
            }
            QLabel#comboMarker {
                color: #8c7a65;
                font-size: 12px;
                font-weight: 700;
                padding-right: 2px;
            }
            QLabel#sectionCaption {
                color: #90785d;
                font-size: 12px;
                font-weight: 600;
                padding: 0 4px;
            }
            QFrame#sectionLine {
                color: #cdb99e;
                background: #cdb99e;
                max-height: 1px;
            }
            QLabel#stateLabel {
                padding: 10px 12px;
                border-radius: 10px;
                background: #efe4d0;
                color: #6a553f;
                border: 1px solid #d9c4a6;
            }
            QLabel#statusValue {
                padding: 6px 8px;
                border-radius: 8px;
                background: #fffdf9;
                border: 1px solid #d8ccb9;
                color: #4e4033;
            }
            QLineEdit, QComboBox, QListWidget, QTextEdit {
                border: 1px solid #d8ccb9;
                border-radius: 10px;
                padding: 10px;
                background: #fffdf9;
                selection-background-color: #d6ead8;
            }
            QListWidget::item {
                padding: 8px 6px;
                border-radius: 6px;
            }
            QListWidget::item:selected {
                background: #d6ead8;
                color: #204c34;
            }
            QPushButton {
                border: 0;
                border-radius: 12px;
                padding: 12px 14px;
                background: #e2d3bf;
                color: #3d3228;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #d8c5ab;
            }
            QPushButton#primaryButton {
                background: #65c466;
                color: white;
                font-size: 16px;
            }
            QPushButton#primaryButton:hover {
                background: #54b657;
            }
            QPushButton#dangerButton {
                background: #e67676;
                color: white;
                font-size: 16px;
            }
            QPushButton#dangerButton:hover {
                background: #d96060;
            }
            QTextEdit {
                background: #101512;
                color: #3cff70;
                border: 1px solid #253228;
                font-family: Consolas, "Courier New", monospace;
                font-size: 13px;
            }
            QRadioButton {
                spacing: 8px;
                color: #4b3d30;
            }
            QRadioButton::indicator {
                width: 16px;
                height: 16px;
                border-radius: 8px;
                border: 2px solid #b79d7b;
                background: #fffdf9;
            }
            QRadioButton::indicator:checked {
                background: #8a6e4f;
                border: 2px solid #8a6e4f;
            }
            QRadioButton::indicator:unchecked:hover {
                border: 2px solid #9d8465;
            }
            QWidget#logPanel QLabel#panelTitle,
            QWidget#logPanel QLabel#mutedLabel {
                background: transparent;
                color: #574633;
            }
            QCheckBox {
                spacing: 8px;
            }
            """
        )

    def shorten_path(self, path: Path, keep: int = 18) -> str:
        # 左侧区域较窄，路径太长时做中间省略显示，完整路径放在 tooltip 里。
        text = str(path)
        if len(text) <= keep * 2 + 3:
            return text
        return f"{text[:keep]}...{text[-keep:]}"

    def update_script_root_display(self) -> None:
        # 统一刷新“当前脚本库目录”的显示文本和悬浮提示。
        display = self.shorten_path(self.script_root)
        self.script_dir_input.setText(display)
        self.script_dir_input.setToolTip(str(self.script_root))

    def _handle_log_from_worker(self, message: str) -> None:
        # 任何线程里的日志都不要直接操作 Qt 控件，
        # 统一转成信号，交回主线程更新界面。
        self.log_emitted.emit(message)

    def classify_log(self, message: str) -> LogRecord:
        # 根据日志前缀和关键词做粗粒度分类。
        #
        # 第一版不追求复杂的日志协议，只基于现有输出做足够实用的分类：
        # - [JS] / [JS:WARN] / [JS:ERROR]
        # - [!]
        # - 解密结果已保存到真实目录
        # 这样就能先把“过滤”和“着色”做起来。
        category = "general"
        level = "info"

        if message.startswith("[JS:ERROR]"):
            category = "js"
            level = "error"
        elif message.startswith("[JS:WARN]"):
            category = "js"
            level = "warn"
        elif message.startswith("[JS]"):
            category = "js"
            level = "info"
        elif message.startswith("[TOOL]"):
            category = "tool"
            level = "info"
        elif message.startswith("[!]"):
            category = "error"
            level = "error"
        elif "解密结果已保存到真实目录" in message or "日志文件已启用" in message:
            category = "general"
            level = "success"
        elif message.startswith("[+]"):
            category = "general"
            level = "success"

        return LogRecord(message=message, category=category, level=level)

    def log_color(self, record: LogRecord) -> str:
        # 为不同级别的日志分配颜色。
        if record.level == "error":
            return "#ff6b6b"
        if record.level == "warn":
            return "#ffd166"
        if record.category == "tool":
            return "#7ad7ff"
        if record.level == "success":
            return "#65f18c"
        return "#3cff70"

    def should_show_log(self, record: LogRecord) -> bool:
        # 根据当前过滤器判断一条日志是否需要显示。
        filter_name = self.log_filter_combo.currentText()
        if filter_name == "全部日志":
            return True
        if filter_name == "只看 [JS]":
            return record.category == "js"
        if filter_name == "只看错误":
            return record.level == "error"
        if filter_name == "只看调试工具":
            return record.category == "tool"
        return True

    def render_logs(self) -> None:
        # 重新渲染日志区。
        #
        # 过滤条件切换时直接基于内存中的结构化日志重绘，
        # 而不是去解析 QTextEdit 里的文本。
        html_lines: list[str] = []
        for record in self.log_records:
            if not self.should_show_log(record):
                continue
            html_lines.append(
                f'<div style="color: {self.log_color(record)}; white-space: pre-wrap;">'
                f"{escape(record.message)}"
                "</div>"
            )

        self.log_console.setUpdatesEnabled(False)
        self.log_console.setHtml("".join(html_lines))
        self.log_console.moveCursor(QTextCursor.End)
        self.log_console.setUpdatesEnabled(True)

    def persist_log(self, message: str) -> None:
        # 如果用户选择了日志文件，就把原始文本同步落盘。
        if self.log_file_path is None:
            return
        try:
            self.log_file_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file_path.open("a", encoding="utf-8") as fp:
                fp.write(message.rstrip() + "\n")
        except OSError as exc:
            self.status_bar.showMessage(f"日志写入失败: {exc}")

    def choose_log_file(self) -> None:
        # 选择 GUI 日志落盘文件。
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "选择日志文件",
            str(self.deps.context.project_root / "hookers_gui.log"),
            "Log Files (*.log);;Text Files (*.txt);;All Files (*.*)",
        )
        if not file_path:
            return
        selected_path = Path(file_path)
        try:
            selected_path.parent.mkdir(parents=True, exist_ok=True)
            # 立即创建/校验文件，这样用户选择后就能确认“保存已真正生效”。
            selected_path.touch(exist_ok=True)
            # 把当前缓存中的日志一并补写进去，避免“只能保存之后的新日志”造成误解。
            with selected_path.open("a", encoding="utf-8") as fp:
                if selected_path.stat().st_size == 0 and self.log_records:
                    for record in self.log_records:
                        fp.write(record.message.rstrip() + "\n")
        except OSError as exc:
            QMessageBox.critical(self, "日志文件不可用", f"无法写入日志文件：{exc}")
            self.status_bar.showMessage(f"日志文件不可用: {exc}")
            return

        self.log_file_path = selected_path
        self.log_file_input.setText(str(self.log_file_path))
        self.append_log(f"[*] 日志文件已启用：{self.log_file_path}")

    def clear_logs(self) -> None:
        # 清空界面中的日志缓存和显示。
        #
        # 这里只清空 GUI 内存和面板，不删除已经写入磁盘的日志文件。
        self.log_records.clear()
        self.log_console.clear()
        self.status_bar.showMessage("日志显示已清空")

    def append_log(self, message: str) -> None:
        # 追加日志时：
        # 1. 先做结构化分类
        # 2. 控制内存中的最大日志条数
        # 3. 根据当前过滤器刷新右侧面板
        # 4. 如果配置了日志文件，就同步落盘
        record = self.classify_log(message.rstrip())
        self.log_records.append(record)

        # 做一层简单的内存保护，避免高频日志长时间运行后把 GUI 拖慢。
        if len(self.log_records) > self.MAX_LOG_RECORDS:
            overflow = len(self.log_records) - self.MAX_LOG_RECORDS
            del self.log_records[:overflow]

        self.persist_log(record.message)
        self.render_logs()

    def set_busy(self, busy: bool, message: str | None = None) -> None:
        # 统一管理按钮禁用状态，避免多个分支各自写一套启停逻辑。
        self.refresh_apps_button.setDisabled(busy)
        # 活动会话存在时，不允许再次启动注入。
        self.start_hook_button.setDisabled(busy or self.deps.context.active_session is not None)
        self.select_script_dir_button.setDisabled(busy)
        self.reload_script_dir_button.setDisabled(busy)
        self.prepare_workspace_button.setDisabled(busy)
        self.attach_mode_radio.setDisabled(busy)
        self.spawn_mode_radio.setDisabled(busy)
        self.hook_target_input.setDisabled(busy)
        self.inspect_target_input.setDisabled(busy)
        self.generate_hook_button.setDisabled(busy)
        self.view_activity_button.setDisabled(busy)
        self.view_service_button.setDisabled(busy)
        self.restart_app_button.setDisabled(busy)
        self.object_info_button.setDisabled(busy)
        self.object_explain_button.setDisabled(busy)
        self.view_info_button.setDisabled(busy)
        self.app_combo.setDisabled(busy)
        self.stop_hook_button.setDisabled(self.deps.context.active_session is None)
        if message:
            self.current_state_label.setText(f"状态：{message}")
            self.status_bar.showMessage(message)

    def choose_script_directory(self) -> None:
        # 允许用户切换到项目外的脚本目录，方便把 GUI 当成一个通用 Frida 工作台。
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择脚本文件夹",
            str(self.script_root),
        )
        if not selected:
            return
        self.script_root = Path(selected)
        self.script_source_hint.setText(self.script_root.name or str(self.script_root))
        self.update_script_root_display()
        self.refresh_script_list()

    def refresh_script_list(self) -> None:
        # 扫描脚本目录下的 .js 文件并刷新左侧下拉框。
        self.script_combo.clear()
        if not self.script_root.exists():
            return

        files = sorted(self.script_root.glob("*.js"), key=lambda path: path.name.lower())
        for path in files:
            self.script_combo.addItem(path.name, str(path.resolve()))

        if self.script_combo.count() > 0:
            self.script_combo.setCurrentIndex(-1)
            self.selected_script_label.setText("当前脚本：未选择")
            self.selected_script_label.setToolTip("")
        else:
            self.selected_script_label.setText("当前脚本：未选择")

    def _update_selected_script_tip(self) -> None:
        # 底部显示当前选中脚本的完整路径，方便确认自己到底注入了哪个文件。
        script_path = self.selected_script_path()
        if script_path is None:
            self.selected_script_label.setText("当前脚本：未选择")
            self.selected_script_label.setToolTip("")
            return
        self.selected_script_label.setText(f"当前脚本：{script_path.name}")
        self.selected_script_label.setToolTip(str(script_path))

    def selected_script_path(self) -> Path | None:
        # 从脚本下拉框中取回当前选中脚本的完整路径。
        value = self.script_combo.currentData()
        if value is None:
            return None
        return Path(str(value))

    def selected_package_name(self) -> str | None:
        # ComboBox 保存的是包名，显示文本只是给人看的 label。
        value = self.app_combo.currentData()
        if value is None:
            return None
        return str(value)

    def find_cached_app(self, package_name: str | None) -> Any:
        # 从最近一次枚举的应用列表里找回当前包名的基础信息。
        if not package_name:
            return None
        for app in self.deps.context.apps:
            if app.identifier == package_name:
                return app
        return None

    def refresh_app_status_panel(self, package_name: str | None = None) -> None:
        # 统一刷新左侧 App 状态摘要。
        # 这里优先展示 current_app 的实时数据，缺失时再回退到应用列表缓存。
        package_name = package_name or self.selected_package_name()
        current_app = self.deps.context.current_app
        if current_app is not None and current_app.identifier != package_name:
            current_app = None

        cached_app = self.find_cached_app(package_name)
        active_session = self.deps.context.active_session

        pid = cached_app.pid if cached_app is not None else None
        uid = None
        version = None
        mode = "未启动"

        if current_app is not None:
            pid = current_app.pid if current_app.pid is not None else pid
            uid = current_app.uid
            version = current_app.version
        if active_session is not None and self.deps.context.current_app is not None:
            if package_name == self.deps.context.current_app.identifier:
                mode = active_session.mode

        self.left_pid_uid_status_value.setText(
            f"PID: {pid if pid is not None else '-'} | UID: {uid if uid is not None else '-'}"
        )
        self.left_version_mode_status_value.setText(
            f"Version: {version or '-'} | 模式: {mode}"
        )

    def on_package_changed(self) -> None:
        # 选中包名时只切换到该包的轻量工作区壳，不再自动触发完整初始化。
        package_name = self.selected_package_name()
        self.deps.rpc_service.invalidate_persistent_session()
        if not package_name:
            self.workspace_path_input.clear()
            self.prepare_workspace_button.setDisabled(True)
            self.refresh_app_status_panel(None)
            return

        workspace_dir = self.deps.workspace_service.ensure_workspace_shell(package_name)
        script_dir = self.deps.workspace_service.script_dir(package_name)
        self.prepare_workspace_button.setDisabled(False)

        self.workspace_path_input.setText(str(workspace_dir))
        self.workspace_path_input.setToolTip(str(workspace_dir))

        self.script_root = script_dir
        self.script_source_hint.setText(f"{package_name}/js")
        self.update_script_root_display()
        self.refresh_script_list()
        self.refresh_app_status_panel(package_name)

        self.append_log(f"[*] 当前工作目录：{workspace_dir}")
        self.append_log(f"[*] 当前脚本目录已切换到：{script_dir}")

    def prepare_selected_workspace(self) -> None:
        # 显式初始化当前选中 App 的完整工作目录，并在需要时拉取 APK。
        package_name = self.selected_package_name()
        if not package_name:
            QMessageBox.warning(self, "未选择 App", "请先选择一个目标 App。")
            return
        self.start_workspace_prepare(package_name)

    def start_workspace_prepare(self, package_name: str) -> None:
        # 完整初始化工作目录，并在需要时拉取 APK。
        if self.workspace_thread is not None:
            return

        self.set_busy(True, "正在初始化工作目录并拉取 APK")
        self.workspace_thread = QThread(self)
        self.workspace_worker = WorkspaceWorker(
            device_service=self.deps.device_service,
            workspace_service=self.deps.workspace_service,
            package_name=package_name,
        )
        self.workspace_worker.moveToThread(self.workspace_thread)

        self.workspace_thread.started.connect(self.workspace_worker.run)
        self.workspace_worker.ready.connect(self.on_workspace_ready)
        self.workspace_worker.failed.connect(self.on_worker_failed)
        self.workspace_worker.finished.connect(self.workspace_thread.quit)
        self.workspace_worker.finished.connect(self.workspace_worker.deleteLater)
        self.workspace_thread.finished.connect(self.workspace_thread.deleteLater)
        self.workspace_thread.finished.connect(self._clear_workspace_thread)
        self.workspace_thread.start()

    def _clear_workspace_thread(self) -> None:
        self.workspace_thread = None
        self.workspace_worker = None

    def on_workspace_ready(
        self,
        package_name: str,
        workspace_dir: str,
        script_dir: str,
    ) -> None:
        self.workspace_path_input.setText(workspace_dir)
        self.workspace_path_input.setToolTip(workspace_dir)
        self.script_root = Path(script_dir)
        self.script_source_hint.setText(f"{package_name}/js")
        self.update_script_root_display()
        self.refresh_script_list()
        self.refresh_app_status_panel(package_name)
        self.set_busy(False, "工作目录已初始化")
        self.append_log(f"[+] {package_name} 工作目录已完成初始化：{workspace_dir}")

    def start_device_prepare(self) -> None:
        # 启动“准备环境”后台任务。
        #
        # 这里先做一个并发保护：如果线程已经存在，就不重复启动第二个。
        if self.device_thread is not None:
            return

        self.append_log("[*] 开始准备设备环境并刷新 App 列表...")
        self.set_busy(True, "正在准备设备环境")

        self.device_thread = QThread(self)
        self.device_worker = DeviceWorker(
            device_service=self.deps.device_service,
        )
        self.device_worker.moveToThread(self.device_thread)

        # 线程启动后执行 worker.run()，
        # 结束后由 Qt 自己回收 worker / thread 对象。
        self.device_thread.started.connect(self.device_worker.run)
        self.device_worker.apps_ready.connect(self.on_apps_ready)
        self.device_worker.failed.connect(self.on_worker_failed)
        self.device_worker.finished.connect(self.device_thread.quit)
        self.device_worker.finished.connect(self.device_worker.deleteLater)
        self.device_thread.finished.connect(self.device_thread.deleteLater)
        self.device_thread.finished.connect(self._clear_device_thread)
        self.device_thread.start()

    def _clear_device_thread(self) -> None:
        # 把 Python 层的引用清空，允许后续再次点击“准备环境”。
        self.device_thread = None
        self.device_worker = None

    def on_apps_ready(self, apps: list[dict[str, Any]]) -> None:
        # 收到后台线程返回的应用列表后，刷新下拉框。
        self.deps.rpc_service.invalidate_persistent_session()
        self.app_combo.blockSignals(True)
        self.app_combo.clear()
        for app in apps:
            name = app["name"]
            pid = app["pid"]
            identifier = app["identifier"]
            label = f"{name} ({identifier})"
            if pid is not None:
                label = f"[{pid}] {label}"
            self.app_combo.addItem(label, identifier)
        self.app_combo.setCurrentIndex(-1)
        self.app_combo.blockSignals(False)
        self.prepare_workspace_button.setDisabled(True)

        self.set_busy(False, f"已同步设备 {len(apps)} 个应用")
        self.append_log(f"[v] 已同步设备 {len(apps)} 个进程/应用")
        if apps:
            self.current_state_label.setText("状态：环境已就绪，可以开始注入")
            self.append_log("[*] 准备已完成，请选择目标 APP。")
            QMessageBox.information(self, "准备已完成", "准备已完成，请选择目标 APP。")
        else:
            self.current_state_label.setText("状态：环境已就绪，但没有枚举到应用")
            self.append_log("[!] 准备已完成，但当前没有枚举到可选择的 APK 包名。")
            QMessageBox.warning(self, "未发现应用", "准备已完成，但当前没有枚举到可选择的 APK 包名。")
            self.workspace_path_input.clear()

        self.refresh_app_status_panel()

    def on_worker_failed(self, message: str) -> None:
        # 统一错误出口：
        # 1. 更新状态
        # 2. 记录日志
        # 3. 弹出错误框
        self.set_busy(False, "发生错误")
        self.append_log(f"[!] {message}")
        QMessageBox.critical(self, "执行失败", message)

    def start_action(
        self,
        *,
        busy_message: str,
        action,
        on_success,
    ) -> None:
        if self.action_thread is not None:
            return

        self.set_busy(True, busy_message)
        self.action_thread = QThread(self)
        self.action_worker = ActionWorker(action)
        self.action_worker.moveToThread(self.action_thread)

        self.action_thread.started.connect(self.action_worker.run)
        self.action_worker.succeeded.connect(on_success)
        self.action_worker.failed.connect(self.on_worker_failed)
        self.action_worker.finished.connect(self.action_thread.quit)
        self.action_worker.finished.connect(self.action_worker.deleteLater)
        self.action_thread.finished.connect(self.action_thread.deleteLater)
        self.action_thread.finished.connect(self._clear_action_thread)
        self.action_thread.start()

    def _clear_action_thread(self) -> None:
        self.action_thread = None
        self.action_worker = None

    def ensure_current_app_ready(self) -> str:
        # GUI 里的分析类动作不一定要求已经开始注入，
        # 但它们都需要一个有效的 current_app 上下文。
        # 这里统一负责把包名校验、拉前台、回填 current_app 串起来。
        package_name = self.selected_package_name()
        if not package_name:
            raise RuntimeError("请先选择一个目标 App")

        current_app = self.deps.context.current_app
        active_session = self.deps.context.active_session
        # 对 GUI 来说，如果当前包名已经有活动会话且 PID 有效，
        # 说明注入链路本身已经跑通，此时不再强制依赖 resumed-activity 检测。
        # 这样可以避免 spawn 后点击 Activity/Service 等按钮时，
        # 因 ROM/时序差异导致“前台未确认”误报。
        if (
            current_app is not None
            and active_session is not None
            and current_app.identifier == package_name
            and current_app.pid is not None
        ):
            workspace_dir = self.deps.workspace_service.workspace_dir(current_app.identifier)
            self.workspace_path_input.setText(str(workspace_dir))
            self.workspace_path_input.setToolTip(str(workspace_dir))
            self.refresh_app_status_panel(current_app.identifier)
            return current_app.identifier

        app = self.deps.device_service.ensure_app_in_foreground(package_name)
        workspace_dir = self.deps.workspace_service.workspace_dir(app.identifier)
        self.workspace_path_input.setText(str(workspace_dir))
        self.workspace_path_input.setToolTip(str(workspace_dir))
        self.refresh_app_status_panel(app.identifier)
        return app.identifier

    def format_result_text(self, result: Any) -> str:
        # RPC 返回值类型不固定，这里统一整理成可展示文本。
        if result is None:
            return "无结果"
        if isinstance(result, str):
            return result.strip() or "无结果"
        if isinstance(result, (list, tuple, dict)):
            try:
                return json.dumps(result, ensure_ascii=False, indent=2)
            except TypeError:
                pass
        return str(result)

    def show_result_dialog(self, title: str, content: str) -> None:
        # 结果窗口改成非模态，这样查看 Activity / Service 时，
        # 主窗口仍然可以继续操作，不会被 dialog.exec() 卡住。
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(860, 620)
        dialog.setModal(False)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        content_view = QTextEdit(dialog)
        content_view.setReadOnly(True)
        content_view.setPlainText(content)
        layout.addWidget(content_view, 1)

        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)

        self.result_windows.append(dialog)
        dialog.destroyed.connect(lambda _=None, win=dialog: self._forget_result_window(win))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _forget_result_window(self, dialog: QDialog) -> None:
        # 结果窗口关闭后，把引用从缓存里移除，避免长期积累无效对象。
        self.result_windows = [window for window in self.result_windows if window is not dialog]

    def inspect_target(self) -> str:
        # object_info / object_to_explain / view_info 共用一个输入框。
        target = self.inspect_target_input.text().strip()
        if not target:
            raise RuntimeError("请输入对象 ID、类名或 View ID")
        return target

    """

    def _legacy_generate_hook_script(self) -> None:
        # GUI 版 gs 命令：输入类名或类名:方法后直接生成脚本到当前包名工作目录。
        hook_target = self.hook_target_input.text().strip()
        if not hook_target:
            QMessageBox.warning(self, "缺少 Hook 目标", "请输入类名或类名:方法。")
            return

        try:
            self.set_busy(True, "正在生成 Hook 脚本")
            package_name = self.ensure_current_app_ready()
            script_path = self.deps.rpc_service.generate_hook_script(hook_target)
            self.append_log(f"[TOOL] 已生成 Hook 脚本：{script_path}")

            self.script_root = self.deps.workspace_service.script_dir(package_name)
            self.script_source_hint.setText(f"{package_name}/js")
            self.update_script_root_display()
            self.refresh_script_list()

            target_resolved = str(script_path.resolve())
            for index in range(self.script_combo.count()):
                if self.script_combo.itemData(index) == target_resolved:
                    self.script_combo.setCurrentIndex(index)
                    break

            self.status_bar.showMessage(f"Hook 脚本已生成：{script_path.name}")
            QMessageBox.information(self, "生成成功", f"脚本已生成到：\n{script_path}")
        except Exception as exc:
            self.on_worker_failed(str(exc))
        finally:
            self.set_busy(False, "环境已就绪")

    def _legacy_show_activities(self) -> None:
        # GUI 版 activitys 命令。
        try:
            self.set_busy(True, "正在查询 Activity")
            package_name = self.ensure_current_app_ready()
            result = self.deps.rpc_service.activitys()
            self.append_log(f"[TOOL] 已获取 {package_name} 的 Activity 信息")
            self.show_result_dialog("Activity 列表", self.format_result_text(result))
        except Exception as exc:
            self.on_worker_failed(str(exc))
        finally:
            self.set_busy(False, "环境已就绪")

    def _legacy_show_services(self) -> None:
        # GUI 版 services 命令。
        try:
            self.set_busy(True, "正在查询 Service")
            package_name = self.ensure_current_app_ready()
            result = self.deps.rpc_service.services()
            self.append_log(f"[TOOL] 已获取 {package_name} 的 Service 信息")
            self.show_result_dialog("Service 列表", self.format_result_text(result))
        except Exception as exc:
            self.on_worker_failed(str(exc))
        finally:
            self.set_busy(False, "环境已就绪")

    def _legacy_show_object_info(self) -> None:
        # GUI 版 object_info 命令。
        try:
            self.set_busy(True, "正在查询对象信息")
            package_name = self.ensure_current_app_ready()
            target = self.inspect_target()
            result = self.deps.rpc_service.object_info(target)
            self.append_log(f"[TOOL] 已获取 {package_name} 的对象信息：{target}")
            self.show_result_dialog(f"对象信息 - {target}", self.format_result_text(result))
        except Exception as exc:
            self.on_worker_failed(str(exc))
        finally:
            self.set_busy(False, "环境已就绪")

    def _legacy_show_object_explain(self) -> None:
        # GUI 版 object_to_explain 命令。
        try:
            self.set_busy(True, "正在解释对象")
            package_name = self.ensure_current_app_ready()
            target = self.inspect_target()
            result = self.deps.rpc_service.object_to_explain(target)
            self.append_log(f"[TOOL] 已获取 {package_name} 的对象解释：{target}")
            self.show_result_dialog(f"对象解释 - {target}", self.format_result_text(result))
        except Exception as exc:
            self.on_worker_failed(str(exc))
        finally:
            self.set_busy(False, "环境已就绪")

    def _legacy_show_view_info(self) -> None:
        # GUI 版 view_info 命令。
        try:
            self.set_busy(True, "正在查询 View 信息")
            package_name = self.ensure_current_app_ready()
            target = self.inspect_target()
            result = self.deps.rpc_service.view_info(target)
            self.append_log(f"[TOOL] 已获取 {package_name} 的 View 信息：{target}")
            self.show_result_dialog(f"View 信息 - {target}", self.format_result_text(result))
        except Exception as exc:
            self.on_worker_failed(str(exc))
        finally:
            self.set_busy(False, "环境已就绪")

    def _legacy_restart_current_app(self) -> None:
        # GUI 版 restart 命令。重启后顺手刷新一次应用列表，让 PID 等状态同步回来。
        try:
            self.set_busy(True, "正在重启目标 App")
            package_name = self.ensure_current_app_ready()
            if self.deps.context.active_session is not None:
                # 先温和停止当前会话，避免应用重启后留下失效的旧 session。
                self.deps.session_service.stop_active_session()
            self.deps.session_service.restart_current_app()
            self.append_log(f"[TOOL] 已重启 App：{package_name}")

            apps = self.deps.device_service.refresh_applications()
            self.on_apps_ready(
                [
                    {"name": app.name, "identifier": app.identifier, "pid": app.pid}
                    for app in apps
                ]
            )
            current_index = self.app_combo.findData(package_name)
            if current_index >= 0:
                self.app_combo.setCurrentIndex(current_index)
        except Exception as exc:
            self.on_worker_failed(str(exc))
        finally:
            self.set_busy(False, "环境已就绪")

    """

    def start_hook(self) -> None:
        # 启动“开始注入”后台任务。
        # 第一版只支持单会话，所以有 hook_thread 时直接忽略重复点击。
        if self.hook_thread is not None:
            return

        package_name = self.selected_package_name()
        if not package_name:
            QMessageBox.warning(self, "未选择 App", "请先准备环境并选择一个目标 App。")
            return

        script_path = self.selected_script_path()
        if script_path is None:
            QMessageBox.warning(self, "未选择脚本", "请先在左侧选择一个 .js 脚本。")
            return

        self.append_log(f"[i] 目标 App: {package_name}")
        self.append_log(f"[i] 已选中脚本: {script_path.name}")
        self.set_busy(True, "正在启动注入")

        self.hook_thread = QThread(self)
        self.hook_worker = HookWorker(
            device_service=self.deps.device_service,
            session_service=self.deps.session_service,
            workspace_service=self.deps.workspace_service,
            package_name=package_name,
            script_path=script_path,
            use_spawn=self.spawn_mode_radio.isChecked(),
            ensure_workspace=False,
        )
        self.hook_worker.moveToThread(self.hook_thread)

        # 这条链路和设备准备类似，只是这里的 worker 会触发 attach/spawn。
        self.hook_thread.started.connect(self.hook_worker.run)
        self.hook_worker.started.connect(self.on_hook_started)
        self.hook_worker.failed.connect(self.on_worker_failed)
        self.hook_worker.finished.connect(self.hook_thread.quit)
        self.hook_worker.finished.connect(self.hook_worker.deleteLater)
        self.hook_thread.finished.connect(self.hook_thread.deleteLater)
        self.hook_thread.finished.connect(self._clear_hook_thread)
        self.hook_thread.start()

    def _clear_hook_thread(self) -> None:
        # Hook 启动线程结束后，允许再次发起新的注入任务。
        self.hook_thread = None
        self.hook_worker = None

    def on_hook_started(self, mode: str, package_name: str, script_name: str) -> None:
        # 注入成功后，把界面切到“运行中”状态。
        # 由于 active_session 已经保存在 SessionService / context 中，
        # 此时线程虽然结束了，但 Hook 会话本身仍然存活。
        self.set_busy(False, f"{mode} 已启动")
        self.start_hook_button.setDisabled(True)
        self.stop_hook_button.setDisabled(False)
        self.current_state_label.setText(
            f"状态：{mode} 模式运行中 | App: {package_name} | 脚本: {script_name}"
        )
        self.append_log(f"[+] 已启动 {mode} 注入：{package_name} <- {script_name}")

        self.refresh_app_status_panel(package_name)

    def stop_hook(self) -> None:
        if self.action_thread is not None:
            return

        def action() -> None:
            self.deps.session_service.stop_active_session()

        self.start_action(
            busy_message="Stopping hook",
            action=action,
            on_success=self.on_hook_stopped,
        )

    def on_hook_stopped(self, _payload: Any) -> None:
        self.deps.rpc_service.invalidate_persistent_session()
        self.current_state_label.setText("状态：会话已停止")
        self.status_bar.showMessage("当前 Hook 已停止")
        self.append_log("[*] 当前 Hook 已停止")
        self.start_hook_button.setDisabled(False)
        self.stop_hook_button.setDisabled(True)
        self.refresh_app_status_panel()
        self.set_busy(False, "Ready")

    def generate_hook_script(self) -> None:
        hook_target = self.hook_target_input.text().strip()
        if not hook_target:
            QMessageBox.warning(self, "Missing Target", "Enter class or class:method.")
            return

        def action() -> dict[str, Any]:
            package_name = self.ensure_current_app_ready()
            script_path = self.deps.rpc_service.generate_hook_script(hook_target)
            return {"package_name": package_name, "script_path": str(script_path)}

        self.start_action(
            busy_message="Generating hook script",
            action=action,
            on_success=self.on_hook_script_generated,
        )

    def on_hook_script_generated(self, payload: Any) -> None:
        package_name = str(payload["package_name"])
        script_path = Path(str(payload["script_path"]))
        self.append_log(f"[TOOL] Generated hook script: {script_path}")
        self.script_root = self.deps.workspace_service.script_dir(package_name)
        self.script_source_hint.setText(f"{package_name}/js")
        self.update_script_root_display()
        self.refresh_script_list()

        target_resolved = str(script_path.resolve())
        for index in range(self.script_combo.count()):
            if self.script_combo.itemData(index) == target_resolved:
                self.script_combo.setCurrentIndex(index)
                break

        self.set_busy(False, "Ready")
        self.status_bar.showMessage(f"Hook script generated: {script_path.name}")
        QMessageBox.information(self, "Generated", f"Script saved to:\n{script_path}")

    def show_activities(self) -> None:
        def action() -> dict[str, Any]:
            package_name = self.ensure_current_app_ready()
            result = self.deps.rpc_service.activitys()
            return {"package_name": package_name, "result": result}

        self.start_action(
            busy_message="Loading activities",
            action=action,
            on_success=self.on_activities_ready,
        )

    def on_activities_ready(self, payload: Any) -> None:
        package_name = str(payload["package_name"])
        self.append_log(f"[TOOL] Loaded activities for {package_name}")
        self.show_result_dialog("Activity List", self.format_result_text(payload["result"]))
        self.set_busy(False, "Ready")

    def show_services(self) -> None:
        def action() -> dict[str, Any]:
            package_name = self.ensure_current_app_ready()
            result = self.deps.rpc_service.services()
            return {"package_name": package_name, "result": result}

        self.start_action(
            busy_message="Loading services",
            action=action,
            on_success=self.on_services_ready,
        )

    def on_services_ready(self, payload: Any) -> None:
        package_name = str(payload["package_name"])
        self.append_log(f"[TOOL] Loaded services for {package_name}")
        self.show_result_dialog("Service List", self.format_result_text(payload["result"]))
        self.set_busy(False, "Ready")

    def show_object_info(self) -> None:
        def action() -> dict[str, Any]:
            package_name = self.ensure_current_app_ready()
            target = self.inspect_target()
            result = self.deps.rpc_service.object_info(target)
            return {"package_name": package_name, "target": target, "result": result}

        self.start_action(
            busy_message="Loading object info",
            action=action,
            on_success=self.on_object_info_ready,
        )

    def on_object_info_ready(self, payload: Any) -> None:
        package_name = str(payload["package_name"])
        target = str(payload["target"])
        self.append_log(f"[TOOL] Loaded object info for {package_name}: {target}")
        self.show_result_dialog(
            f"Object Info - {target}",
            self.format_result_text(payload["result"]),
        )
        self.set_busy(False, "Ready")

    def show_object_explain(self) -> None:
        def action() -> dict[str, Any]:
            package_name = self.ensure_current_app_ready()
            target = self.inspect_target()
            result = self.deps.rpc_service.object_to_explain(target)
            return {"package_name": package_name, "target": target, "result": result}

        self.start_action(
            busy_message="Explaining object",
            action=action,
            on_success=self.on_object_explain_ready,
        )

    def on_object_explain_ready(self, payload: Any) -> None:
        package_name = str(payload["package_name"])
        target = str(payload["target"])
        self.append_log(f"[TOOL] Explained object for {package_name}: {target}")
        self.show_result_dialog(
            f"Object Explain - {target}",
            self.format_result_text(payload["result"]),
        )
        self.set_busy(False, "Ready")

    def show_view_info(self) -> None:
        def action() -> dict[str, Any]:
            package_name = self.ensure_current_app_ready()
            target = self.inspect_target()
            result = self.deps.rpc_service.view_info(target)
            return {"package_name": package_name, "target": target, "result": result}

        self.start_action(
            busy_message="Loading view info",
            action=action,
            on_success=self.on_view_info_ready,
        )

    def on_view_info_ready(self, payload: Any) -> None:
        package_name = str(payload["package_name"])
        target = str(payload["target"])
        self.append_log(f"[TOOL] Loaded view info for {package_name}: {target}")
        self.show_result_dialog(
            f"View Info - {target}",
            self.format_result_text(payload["result"]),
        )
        self.set_busy(False, "Ready")

    def restart_current_app(self) -> None:
        def action() -> dict[str, Any]:
            package_name = self.ensure_current_app_ready()
            self.deps.rpc_service.invalidate_persistent_session()
            if self.deps.context.active_session is not None:
                self.deps.session_service.stop_active_session()
            self.deps.session_service.restart_current_app()
            apps = self.deps.device_service.refresh_applications()
            return {
                "package_name": package_name,
                "apps": [
                    {"name": app.name, "identifier": app.identifier, "pid": app.pid}
                    for app in apps
                ],
            }

        self.start_action(
            busy_message="Restarting app",
            action=action,
            on_success=self.on_restart_current_app_finished,
        )

    def on_restart_current_app_finished(self, payload: Any) -> None:
        self.deps.rpc_service.invalidate_persistent_session()
        package_name = str(payload["package_name"])
        self.append_log(f"[TOOL] Restarted app: {package_name}")
        self.on_apps_ready(payload["apps"])
        current_index = self.app_combo.findData(package_name)
        if current_index >= 0:
            self.app_combo.setCurrentIndex(current_index)
        self.set_busy(False, "Ready")

    def closeEvent(self, event) -> None:  # noqa: N802
        # 关闭窗口时尽量做一次温和清理：
        # 1. 停止当前 Hook
        # 2. 不强杀线程，让 Qt 走正常收尾流程
        try:
            self.deps.rpc_service.invalidate_persistent_session()
        except Exception:
            pass
        try:
            self.deps.session_service.stop_active_session()
        except Exception:
            pass
        super().closeEvent(event)

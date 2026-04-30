from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal, Slot


class WorkspaceWorker(QObject):
    # 负责在后台线程里完成“选中 App 后初始化工作目录”的重活。
    # 这里不强制把目标 App 拉到前台，只准备 AppContext 并初始化工作区。
    # 这样更符合 spawn 工作流：工作区准备和“界面是否已到前台”不是一回事。
    ready = Signal(str, str, str)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        device_service: Any,
        workspace_service: Any,
        package_name: str,
    ) -> None:
        super().__init__()
        self.device_service = device_service
        self.workspace_service = workspace_service
        self.package_name = package_name

    @Slot()
    def run(self) -> None:
        try:
            self.workspace_service.context.emit(
                f"[*] 已选中目标 App，开始初始化工作目录并检查 APK：{self.package_name}"
            )
            app = self.device_service.prepare_app_context(self.package_name)
            workspace_dir = self.workspace_service.ensure_workspace(app)
            script_dir = self.workspace_service.script_dir(self.package_name)
            self.ready.emit(
                self.package_name,
                str(workspace_dir),
                str(script_dir),
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

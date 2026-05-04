# 此文件用于放共享数据模型
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional


LogHandler = Callable[[str], None]
FridaServerVariant = Literal["default", "florida"]


@dataclass
class AppRecord:
    # 设备上枚举到的应用条目。
    name: str
    identifier: str
    pid: Optional[int] = None


@dataclass
class AppContext:
    # 当前选中应用的完整上下文，用于工作区和会话层复用。
    identifier: str
    name: str
    pid: Optional[int]
    version: Optional[str]
    install_path: str
    install_apk_filename: str
    uid: Optional[int]


@dataclass
class HookSession:
    # 一次 attach/spawn 注入对应的运行态会话对象。
    session: Any
    script: Any
    script_path: Optional[Path] = None
    mode: str = "attach"


@dataclass
class HookerContext:
    # 项目级共享上下文。
    # 这层负责存放设备连接、当前应用、活动会话等跨服务共享状态，
    # 让 device/session/workspace/rpc 服务之间通过同一份上下文协作。
    project_root: Path
    mobile_deploy_dir: Path
    js_dir: Path
    workspaces_dir: Path
    remote_frida_dir: str = "/data/local/tmp/fr"
    remote_default_frida_server_name: str = "fri-ser"
    remote_florida_server_name: str = "flo-ser"
    remote_radar_dex: str = "/data/local/tmp/radar.dex"
    frida_server_variant: FridaServerVariant = "default"
    frida_server_arm64: str = "frida-server-16.7.19-android-arm64"
    frida_server_arm: str = "frida-server-16.7.19-android-arm"
    florida_server_arm64: str = "florida-server-16.7.19"
    adb_device: Any = None
    frida_device: Any = None
    apps: list[AppRecord] = field(default_factory=list)
    current_app: Optional[AppContext] = None
    current_local_apk_path: Optional[Path] = None
    active_session: Optional[HookSession] = None
    webserver_url: Optional[str] = None
    log_handler: Optional[LogHandler] = None

    def emit(self, message: str) -> None:
        # 统一日志出口，未来 GUI 可以在这里接管日志显示。
        if self.log_handler is not None:
            self.log_handler(message)
        else:
            print(message)

    @property
    def local_radar_dex(self) -> Path:
        # 返回项目内置 radar.dex 的本地路径。
        return self.mobile_deploy_dir / "radar.dex"

    @classmethod
    def from_project_root(
        cls,
        project_root: Path,
        log_handler: Optional[LogHandler] = None,
    ) -> "HookerContext":
        # 根据项目根目录构造默认上下文。
        root = project_root.resolve()
        return cls(
            project_root=root,
            mobile_deploy_dir=root / "mobile-deploy",
            js_dir=root / "js",
            workspaces_dir=root / "workspaces",
            log_handler=log_handler,
        )

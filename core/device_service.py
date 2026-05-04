from __future__ import annotations

import re
import subprocess
import time
from typing import Optional

import adbutils
import frida
from adbutils.errors import AdbError

from .models import AppContext, AppRecord, HookerContext


class DeviceService:
    # 负责设备发现、ADB/Frida 环境准备和应用枚举。
    def __init__(self, context: HookerContext) -> None:
        self.context = context

    @property
    def adb_device(self):
        if self.context.adb_device is None:
            raise RuntimeError("ADB device is not connected")
        return self.context.adb_device

    def connect(self) -> None:
        self.context.adb_device = adbutils.adb.device()
        self.context.emit(f"连接到设备: {self.context.adb_device.serial}")

    def _get_frida_device(self):
        serial = getattr(self.context.adb_device, "serial", None)
        if serial:
            return frida.get_device(serial, timeout=3)
        return frida.get_usb_device(timeout=3)

    def _parse_package_apk_path(self, package_name: str) -> tuple[str, str]:
        raw_output = self.adb_shell(f"pm path {package_name}")
        candidates = [
            line.replace("package:", "").strip()
            for line in raw_output.splitlines()
            if line.strip().startswith("package:")
        ]
        if not candidates:
            raise RuntimeError(f"无法解析 {package_name} 的安装路径: {raw_output!r}")

        apk_path = next((path for path in candidates if path.endswith("/base.apk")), None)
        if apk_path is None:
            apk_path = next((path for path in candidates if path.endswith(".apk")), None)
        if apk_path is None:
            raise RuntimeError(f"无法从 pm path 输出中找到 APK 文件: {raw_output!r}")

        install_path, install_apk_filename = apk_path.rsplit("/", 1)
        return install_path, install_apk_filename

    def _read_app_metadata(
        self,
        package_name: str,
    ) -> tuple[Optional[int], str, str, str, Optional[str]]:
        shell_result = self.adb_shell(f"dumpsys package {package_name}")
        uid = None
        match_uid = re.search(r"(userId|uid|appId)=(\d+)", shell_result)
        if match_uid:
            uid = int(match_uid.group(2))

        install_path, install_apk_filename = self._parse_package_apk_path(package_name)
        if hasattr(self.adb_device, "app_info"):
            app_info = self.adb_device.app_info(package_name)
            version_name = app_info.version_name
        else:
            app_info = self.adb_device.package_info(package_name)
            version_name = app_info["version_name"]
        return uid, install_path, install_apk_filename, shell_result, version_name

    def _build_app_context(
        self,
        package_name: str,
        *,
        pid: Optional[int],
        name: Optional[str],
        uid: Optional[int],
        version_name: Optional[str],
        install_path: str,
        install_apk_filename: str,
    ) -> AppContext:
        app_context = AppContext(
            identifier=package_name,
            name=name or package_name,
            pid=pid,
            version=version_name,
            install_path=install_path,
            install_apk_filename=install_apk_filename,
            uid=uid,
        )
        self.context.current_app = app_context
        return app_context

    def prepare_app_context(self, package_name: str) -> AppContext:
        # 为 spawn / 工作区初始化这类场景准备应用上下文，但不强制要求应用已在前台。
        uid, install_path, install_apk_filename, _, version_name = self._read_app_metadata(
            package_name
        )
        pid, name = self._find_running_process(package_name, refresh_apps=True)
        return self._build_app_context(
            package_name,
            pid=pid,
            name=name,
            uid=uid,
            version_name=version_name,
            install_path=install_path,
            install_apk_filename=install_apk_filename,
        )

    def _find_running_process(
        self,
        package_name: str,
        *,
        refresh_apps: bool = False,
    ) -> tuple[Optional[int], Optional[str]]:
        if refresh_apps or not self.context.apps:
            self.refresh_applications()

        for app in self.context.apps:
            if app.identifier == package_name and app.pid:
                return app.pid, app.name

        try:
            frida_device = self.context.frida_device or self._get_frida_device()
            process = frida_device.get_process(package_name)
            return process.pid, process.name
        except frida.ProcessNotFoundError:
            return None, None

    def adb_shell(self, cmd: str) -> str:
        return self.adb_device.shell(cmd).strip()

    def run_root_cmd(self, cmd: str, read_output: bool = True) -> str:
        stream = self.adb_device.shell(["su", "-c", cmd], stream=True)
        try:
            if not read_output:
                time.sleep(1)
                return ""
            return stream.read_until_close().strip()
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def is_root(self) -> bool:
        try:
            return "uid=0" in self.run_root_cmd("id")
        except Exception:
            return False

    def is_magisk_root(self) -> bool:
        output = self.run_root_cmd("id")
        if "uid=0" not in output:
            self.context.emit("设备没有被 root")
            return False
        if "context=u:r:magisk:s0" in output:
            return True
        magisk_marker = self.run_root_cmd("ls /data/adb/magisk.db 2>/dev/null")
        return bool(magisk_marker)

    def is_frida_environment_ready(
        self,
        target_package: str = "com.android.systemui",
    ) -> bool:
        try:
            self.context.frida_device = self._get_frida_device()
            pid = self.context.frida_device.get_process(target_package).pid
            session = self.context.frida_device.attach(pid)
            session.detach()
            self.context.emit("frida-server 已经在运行了")
            return True
        except frida.ServerNotRunningError:
            return False
        except frida.ProcessNotFoundError:
            return True
        except frida.TimedOutError:
            return False
        except Exception as exc:
            self.context.emit(f"Frida 环境检查失败: {exc}")
            return False

    def get_cpu_arch(self) -> str:
        abi = self.adb_shell("getprop ro.product.cpu.abi")
        if "arm64" in abi:
            return "arm64"
        if "armeabi" in abi:
            return "arm"
        if "x86_64" in abi:
            return "x86_64"
        if "x86" in abi:
            return "x86"
        return "arm64"

    def get_frida_server_variant_label(self) -> str:
        if self.context.frida_server_variant == "florida":
            return "过检测 Florid sever"
        return "正常 Frida sever"

    def get_remote_frida_server_name(self) -> str:
        if self.context.frida_server_variant == "florida":
            return self.context.remote_florida_server_name
        return self.context.remote_default_frida_server_name

    def get_all_remote_frida_server_names(self) -> list[str]:
        return [
            self.context.remote_default_frida_server_name,
            self.context.remote_florida_server_name,
        ]

    def get_frida_server_file(self) -> str:
        cpu_arch = self.get_cpu_arch()
        variant = self.context.frida_server_variant
        if variant == "florida":
            if cpu_arch != "arm64":
                raise RuntimeError(
                    f"Florida 当前仅支持 arm64 设备，当前架构为: {cpu_arch}"
                )
            return self.context.florida_server_arm64
        if cpu_arch == "arm64":
            return self.context.frida_server_arm64
        if cpu_arch == "arm":
            return self.context.frida_server_arm
        raise RuntimeError(
            f"当前 Frida Server 暂不支持设备架构: {cpu_arch}。请手动启动。"
        )

    def get_remote_frida_server_path(self) -> str:
        return f"{self.context.remote_frida_dir}/{self.get_remote_frida_server_name()}"

    def get_remote_server_pid(self, remote_server_name: str) -> Optional[str]:
        output = self.run_root_cmd(
            f"pidof {remote_server_name} 2>/dev/null || true"
        ).strip()
        if not output:
            return None
        return output.split()[0]

    def cleanup_remote_frida_files(self) -> None:
        remote_dir = self.context.remote_frida_dir
        if remote_dir != "/data/local/tmp/fr":
            raise RuntimeError(f"拒绝清理非预期目录: {remote_dir}")
        if self.context.adb_device is None:
            return
        if not self.is_root():
            self.context.emit("设备没有被 root，跳过远端 Frida 清理")
            return

        if not self.remote_dir_exists(remote_dir):
            return

        stopped_any = False
        for remote_server_name in self.get_all_remote_frida_server_names():
            pid = self.get_remote_server_pid(remote_server_name)
            if pid is None:
                continue
            self.context.emit(
                f"检测到远端 {remote_server_name} 进程正在运行 (pid={pid})，准备停止"
            )
            self.run_root_cmd(f"kill -9 {pid}")
            stopped_any = True
        if stopped_any:
            time.sleep(0.5)

        self.context.emit(f"清理远端 Frida 目录: {remote_dir}")
        files = " ".join(
            f"{remote_dir}/{name}" for name in self.get_all_remote_frida_server_names()
        )
        self.run_root_cmd(f"rm -f {files} 2>/dev/null || true")

    def remote_file_exists(self, path: str) -> bool:
        result = self.adb_shell(f"test -f {path} && echo exists || echo missing")
        return result == "exists"

    def remote_dir_exists(self, path: str) -> bool:
        result = self.adb_shell(f"[ -d {path} ] && echo exists || echo missing")
        return result == "exists"

    def push_file_to_remote(self, local_path, remote_path: str) -> None:
        try:
            self.adb_device.sync.push(str(local_path), remote_path)
        except AdbError:
            subprocess.run(
                [
                    "adb",
                    "-s",
                    self.adb_device.serial,
                    "push",
                    str(local_path),
                    remote_path,
                ],
                check=True,
            )

    def deploy_radar_dex(self) -> None:
        if not self.context.local_radar_dex.exists():
            raise FileNotFoundError(
                f"缺少本地 radar.dex 文件: {self.context.local_radar_dex}"
            )
        if not self.is_root():
            raise RuntimeError("设备没有被 root，无法部署 radar.dex")
        if self.remote_file_exists(self.context.remote_radar_dex):
            return
        self.push_file_to_remote(self.context.local_radar_dex, "/sdcard/")
        self.run_root_cmd(
            f"cp /sdcard/{self.context.local_radar_dex.name} {self.context.remote_radar_dex}"
        )
        self.run_root_cmd(f"chmod 755 {self.context.remote_radar_dex}")

    def start_frida_server(self) -> None:
        if not self.is_root():
            raise RuntimeError("设备没有被 root，无法启动 frida-server")
        if self.is_magisk_root():
            self.context.emit(
                "设备被 Magisk root 了，如运行不成功，请先在 Magisk 中授予 su 权限。"
            )

        frida_server_file = self.get_frida_server_file()
        remote_file = self.get_remote_frida_server_path()
        self.context.emit(
            "当前选择的 Frida Server: "
            f"{self.get_frida_server_variant_label()} ({frida_server_file}) -> {remote_file}"
        )

        local_frida_server = self.context.mobile_deploy_dir / frida_server_file
        if not local_frida_server.exists():
            server_kind = "florida-server" if self.context.frida_server_variant == "florida" else "frida-server"
            raise FileNotFoundError(f"缺少本地 {server_kind} 文件: {local_frida_server}")

        if not self.remote_dir_exists(self.context.remote_frida_dir):
            self.run_root_cmd(f"mkdir -p {self.context.remote_frida_dir}")
        self.cleanup_remote_frida_files()

        remote_server_name = self.get_remote_frida_server_name()
        temp_remote = f"/sdcard/{remote_server_name}"
        self.push_file_to_remote(local_frida_server, temp_remote)
        self.run_root_cmd(f"mv {temp_remote} {remote_file}")
        self.run_root_cmd(f"chmod 755 {remote_file}")

        self.run_root_cmd(
            f"cd {self.context.remote_frida_dir} && ./{remote_server_name} > /sdcard/f_server.log 2>&1 &",
            read_output=False,
        )

        for _ in range(20):
            if self.is_frida_environment_ready():
                self.context.emit(
                    f"Frida Server 启动成功: {remote_file} (来源: {frida_server_file})"
                )
                return
            time.sleep(0.5)
        raise RuntimeError(
            f"Frida Server 启动失败: {remote_file}。请检查权限或手动启动。"
        )

    def refresh_applications(self) -> list[AppRecord]:
        self.context.frida_device = self._get_frida_device()
        self.context.emit("正在获取应用列表...")
        raw_apps = self.context.frida_device.enumerate_applications()
        self.context.apps = [
            AppRecord(name=app.name, identifier=app.identifier, pid=app.pid)
            for app in raw_apps
        ]
        return self.context.apps

    def start_app(self, package_name: str) -> tuple[Optional[int], Optional[str]]:
        shell_result = self.adb_shell(
            f"dumpsys package {package_name} | grep -A 1 MAIN | grep {package_name}"
        )
        match = re.search(r"\s+([^\s]+)\s+filter", shell_result) if shell_result else None
        if match:
            self.adb_shell(f"am start -n {match.group(1)}")
        else:
            self.adb_shell(
                f"monkey -p {package_name} -c android.intent.category.LAUNCHER 1"
            )

        for _ in range(100):
            time.sleep(0.5)
            if self._is_app_in_foreground(package_name):
                pid, name = self._find_running_process(package_name, refresh_apps=True)
                if pid is not None:
                    return pid, name
        return self._find_running_process(package_name, refresh_apps=True)

    def _get_foreground_state(self, package_name: str) -> tuple[bool, str]:
        # 不同 Android 版本 / ROM 对前台 Activity 的 dumpsys 字段并不一致，
        # 这里只要任一稳定信号命中目标包名，就认为该 App 已经在前台。
        outputs = [
            (
                "mResumedActivity",
                self.adb_shell("dumpsys activity activities | grep mResumedActivity"),
            ),
            (
                "topResumedActivity",
                self.adb_shell("dumpsys activity activities | grep topResumedActivity"),
            ),
            (
                "mCurrentFocus",
                self.adb_shell("dumpsys window windows | grep mCurrentFocus"),
            ),
            (
                "mFocusedApp",
                self.adb_shell("dumpsys window windows | grep mFocusedApp"),
            ),
        ]
        combined = "\n".join(
            f"{label}: {text}" for label, text in outputs if text
        )
        return package_name in combined, combined

    def _is_app_in_foreground(self, package_name: str) -> bool:
        is_foreground, _ = self._get_foreground_state(package_name)
        return is_foreground

    def _wait_for_foreground_and_refresh_pid(
        self,
        package_name: str,
        fallback_name: Optional[str] = None,
    ) -> tuple[Optional[int], Optional[str]]:
        for _ in range(100):
            time.sleep(0.2)
            if self._is_app_in_foreground(package_name):
                break
        pid, name = self._find_running_process(package_name, refresh_apps=True)
        return pid, name or fallback_name

    def _require_running_foreground_app(
        self,
        package_name: str,
        pid: Optional[int],
        *,
        reason: str,
    ) -> int:
        is_foreground, foreground_output = self._get_foreground_state(package_name)
        if pid is None or not is_foreground:
            diagnostic = foreground_output or "<no foreground signals matched>"
            raise RuntimeError(
                f"{reason}，但未确认 {package_name} 已稳定进入前台并暴露可用 PID。"
                f" foreground={is_foreground}, pid={pid}"
                f" diagnostic={diagnostic}"
            )
        return pid

    def ensure_app_in_foreground(self, package_name: str) -> AppContext:
        uid, install_path, install_apk_filename, _, version_name = self._read_app_metadata(
            package_name
        )

        proc_map: dict[str, tuple[int, str]] = {}
        for app in sorted(self.refresh_applications(), key=lambda item: item.pid or 0):
            if app.pid:
                proc_map[app.identifier] = (app.pid, app.name)

        is_running = package_name in proc_map
        is_foreground, _ = self._get_foreground_state(package_name)

        if is_running:
            if is_foreground:
                self.context.emit(f"App {package_name} 已在前台运行。")
                pid, name = proc_map[package_name]
                pid = self._require_running_foreground_app(
                    package_name,
                    pid,
                    reason="检测到目标 App 已在前台运行",
                )
            else:
                self.context.emit(f"App {package_name} 在后台运行，正在拉到前台...")
                self.adb_shell(
                    f"monkey -p {package_name} -c android.intent.category.LAUNCHER 1"
                )
                old_pid, old_name = proc_map[package_name]
                pid, name = self._wait_for_foreground_and_refresh_pid(
                    package_name,
                    fallback_name=old_name,
                )
                if pid is not None and pid != old_pid:
                    self.context.emit(
                        f"App {package_name} 前台切换后 PID 已更新：{old_pid} -> {pid}"
                    )
                pid = self._require_running_foreground_app(
                    package_name,
                    pid,
                    reason="已尝试将后台 App 拉到前台",
                )
        else:
            self.context.emit(f"App {package_name} 没有运行，正在启动...")
            pid, name = self.start_app(package_name)
            pid = self._require_running_foreground_app(
                package_name,
                pid,
                reason="已尝试启动目标 App",
            )

        return self._build_app_context(
            package_name,
            pid=pid,
            name=name,
            uid=uid,
            version_name=version_name,
            install_path=install_path,
            install_apk_filename=install_apk_filename,
        )

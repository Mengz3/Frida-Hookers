from __future__ import annotations

from pathlib import Path
from typing import Optional

from .models import AppContext, HookerContext


BUILTIN_JS_FILES = [
    "android_ui.js",
    "apk_shell_scanner.js",
    "bypass_root_detect.js",
    "bypass_vpn_detect.js",
    "click.js",
    "detect_network_stack.js",
    "DumpDex.js",
    "edit_text.js",
    "find_anit_frida_so.js",
    "get_device_info.js",
    "hook_register_natives.js",
    "just_trust_me.js",
    "keystore_dump.js",
    "okhttp.js",
    "print_okhttp_interceptors.js",
    "text_view.js",
    "url.js",
    "activity_events.js",
]

DEFAULT_PACKAGE_PLACEHOLDER = "com.smile.gifmaker"


class WorkspaceService:
    # 负责工作目录、脚本资源和产物文件管理。
    def __init__(self, context: HookerContext) -> None:
        self.context = context

    def workspace_dir(self, package_name: str) -> Path:
        # 返回某个包名对应的工作目录。
        return self.context.workspaces_dir / package_name

    def script_dir(self, package_name: str) -> Path:
        # 返回某个包名对应的脚本目录。
        return self.workspace_dir(package_name) / "js"

    def read_local_file(self, filename) -> Optional[str]:
        # 读取本地文本文件，主要用于 JS 脚本和资源文件。
        path = Path(filename)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.context.emit(f"File {path} not found.")
            return None
        except OSError as exc:
            self.context.emit(f"Error reading file {path}: {exc}")
            return None

    def read_js_resource(self, filename: str) -> Optional[str]:
        # 读取项目内置 js 资源。
        return self.read_local_file(self.context.js_dir / filename)

    def get_resource_script(self, filename: str) -> str:
        # 读取内置脚本；缺失时直接抛错，避免后续拼接出错。
        content = self.read_js_resource(filename)
        if content is None:
            raise FileNotFoundError(f"缺少内置资源: {filename}")
        return content

    def create_working_file(self, filename, text: str) -> Path:
        # 在工作目录中创建文件，并自动补齐父目录。
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def _render_builtin_script(self, js_file: str, package_name: str) -> Optional[str]:
        src = self.context.js_dir / js_file
        if not src.exists():
            return None
        text = src.read_text(encoding="utf-8", errors="ignore")
        return text.replace(DEFAULT_PACKAGE_PLACEHOLDER, package_name)

    def materialize_builtin_scripts(
        self,
        package_name: str,
        script_dir: Path,
        rewrite_existing: bool = False,
    ) -> None:
        # 把内置脚本物化到工作区，并在需要时修正仍保留默认包名占位值的旧副本。
        script_dir.mkdir(parents=True, exist_ok=True)
        for js_file in BUILTIN_JS_FILES:
            rendered = self._render_builtin_script(js_file, package_name)
            if rendered is None:
                continue

            target = script_dir / js_file
            if not target.exists():
                self.create_working_file(target, rendered)
                continue

            if not rewrite_existing:
                continue

            try:
                existing = target.read_text(encoding="utf-8")
            except OSError as exc:
                self.context.emit(f"Error reading file {target}: {exc}")
                continue

            if DEFAULT_PACKAGE_PLACEHOLDER not in existing:
                continue

            updated = existing.replace(DEFAULT_PACKAGE_PLACEHOLDER, package_name)
            if updated != existing:
                self.create_working_file(target, updated)

    def ensure_workspace_shell(self, package_name: str) -> Path:
        # 先创建一个“轻量工作区壳”。
        #
        # 这个方法不会依赖 AppContext，也不会拉 APK，只做两件事：
        # 1. 确保 workspaces/<package>/ 和 workspaces/<package>/js/ 存在
        # 实际路径位于 workspaces/<package>/ 下。
        # 2. 如果脚本目录为空，就从全局模板复制一份默认脚本进去
        #
        # GUI 在用户刚选中包名时，可以先用这个方法把工作区准备出来，
        # 然后把左侧脚本目录默认切换到 workspaces/<package>/js。
        package_dir = self.workspace_dir(package_name)
        script_dir = self.script_dir(package_name)
        self.context.workspaces_dir.mkdir(parents=True, exist_ok=True)
        package_dir.mkdir(parents=True, exist_ok=True)
        script_dir.mkdir(parents=True, exist_ok=True)

        if any(script_dir.glob("*.js")):
            return package_dir

        self.materialize_builtin_scripts(package_name, script_dir)
        return package_dir

    def pull_current_apk(self, app: AppContext) -> Path:
        # 把当前应用 APK 拉到本地工作目录。
        local_apk_path = self.workspace_dir(app.identifier) / (
            f"{app.name.replace(' ', '')}_{app.version}.apk"
        )
        remote_apk = f"{app.install_path}/{app.install_apk_filename}"
        self.context.adb_device.sync.pull(remote_apk, str(local_apk_path))
        self.context.current_local_apk_path = local_apk_path
        return local_apk_path

    def create_initial_workspace(self, app: AppContext) -> Path:
        # 首次进入某个应用时，初始化默认脚本和辅助 bat 文件。
        package_dir = self.workspace_dir(app.identifier)
        self.context.workspaces_dir.mkdir(parents=True, exist_ok=True)
        package_dir.mkdir(parents=True, exist_ok=True)
        self.context.emit(f"创建工作目录: {package_dir.name}")

        log_hooking = (
            "@echo off\r\n"
            + "echo hooking %1 > log.txt\r\n"
            + "echo %date% %time% >> log.txt\r\n"
            + f"frida -U -l %1 -N {app.identifier} >> log.txt 2>&1\r\n"
        )
        attach_shell = "@echo off\r\n" + f"frida -U -l %1 -N {app.identifier}\r\n"
        spawn_shell = (
            "@echo off\r\n" + f"frida -U --runtime=v8 -f {app.identifier} -l %1\r\n"
        )
        kill_shell = "@echo off\r\n" + f"frida-kill -U {app.identifier}\r\n"
        objection_shell = (
            "@echo off\r\n" + f"objection -d -g {app.identifier} explore\r\n"
        )

        self.create_working_file(package_dir / "hooking.bat", log_hooking)
        self.create_working_file(package_dir / "attach.bat", attach_shell)
        self.create_working_file(package_dir / "spawn.bat", spawn_shell)
        self.create_working_file(package_dir / "kill.bat", kill_shell)
        self.create_working_file(package_dir / "objection.bat", objection_shell)

        # 为每个应用复制一份脚本副本，后续修改不会污染全局模板。
        self.materialize_builtin_scripts(app.identifier, package_dir / "js")

        self.pull_current_apk(app)
        self.context.emit("工作目录第一次初始化完成")
        return package_dir

    def initialize_existing_workspace(self, app: AppContext) -> Path:
        # 已有工作目录时补齐缺失的 APK 文件，并修正仍保留默认包名占位值的脚本。
        package_dir = self.workspace_dir(app.identifier)
        self.context.workspaces_dir.mkdir(parents=True, exist_ok=True)
        package_dir.mkdir(parents=True, exist_ok=True)
        self.materialize_builtin_scripts(
            app.identifier,
            package_dir / "js",
            rewrite_existing=True,
        )
        apk_path = package_dir / f"{app.name.replace(' ', '')}_{app.version}.apk"
        self.context.current_local_apk_path = apk_path
        if apk_path.is_file():
            return package_dir
        if apk_path.is_dir():
            raise IsADirectoryError(f"APK 路径异常，当前是目录: {apk_path}")
        self.pull_current_apk(app)
        self.context.emit("工作目录初始化完成")
        return package_dir

    def ensure_workspace(self, app: AppContext) -> Path:
        # 根据工作目录是否存在，选择初始化或补全。
        self.context.workspaces_dir.mkdir(parents=True, exist_ok=True)
        package_dir = self.workspace_dir(app.identifier)
        if package_dir.exists():
            return self.initialize_existing_workspace(app)
        return self.create_initial_workspace(app)

    def list_scripts(self, package_name: str) -> list[Path]:
        # 列出某个工作目录下可直接执行的 js 脚本。
        script_dir = self.script_dir(package_name)
        if not script_dir.is_dir():
            return []
        return sorted(
            [
                path
                for path in script_dir.iterdir()
                if path.is_file() and path.suffix == ".js"
            ]
        )

    def script_names(self, package_name: str) -> list[str]:
        # 仅返回脚本文件名，适合给 CLI/GUI 做展示和补全。
        return [path.name for path in self.list_scripts(package_name)]

    def resolve_script_path(self, script_name_or_path: str, package_name: str) -> Path:
        # 把脚本名解析成实际路径。
        # 优先解析工作目录下的相对路径，便于 CLI 和 GUI 只传入文件名。
        path = Path(script_name_or_path)
        if path.is_absolute():
            return path
        workspace_path = self.script_dir(package_name) / script_name_or_path
        if workspace_path.exists():
            return workspace_path
        return path

    def save_decrypt_output(self, package_name: str, filename: str, content: str) -> Path:
        # 保存脚本通过 send 回传的解密结果。
        safe_package = "".join(
            ch if ch.isalnum() or ch in "._-" else "_"
            for ch in (package_name or "unknown_package")
        )
        safe_filename = Path(filename or "decrypt_output.txt").name
        output_dir = self.workspace_dir(safe_package) / "hook_da5_outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / safe_filename
        normalized_content = (
            content.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
        )
        output_path.write_text(normalized_content, encoding="utf-8")
        self.context.emit(
            f"解密结果已保存到真实目录: {output_path} ({output_path.stat().st_size} bytes)"
        )
        return output_path

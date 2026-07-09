fn main() {
    // Tauri 构建钩子：生成 capability schema 等。
    // dev 模式下不打包 python-build-standalone，使用系统 Python（通过 HERMES_PYTHON_PATH 指向）。
    // 完整 NSIS 安装包构建（v0.2）会在此下载 python-build-standalone 到 binaries/。
    tauri_build::build()
}

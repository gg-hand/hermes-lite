@echo off
where rustup >nul 2>nul || set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
echo === uninstalling existing toolchain ===
rustup toolchain uninstall stable-x86_64-pc-windows-msvc
echo === installing stable-msvc minimal ===
rustup toolchain install stable-msvc --profile minimal
echo === setting default ===
rustup default stable-msvc
echo === verifying ===
rustc --version
cargo --version
echo === DONE ===

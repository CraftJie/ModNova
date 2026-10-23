"""ModNova Windows 打包脚本（PyInstaller）。

用法：
    python build/build.py              # 构建目录版 GUI + zip 便携包
    python build/build.py --no-zip     # 只构建，不打 zip

产物（均不入库，见 .gitignore）：
    build/dist/ModNova/                # 目录版（GUI）
    build/dist/ModNova-<版本>-win-x64.zip
"""
from __future__ import annotations

import argparse
import importlib
import pathlib
import shutil
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BUILD = ROOT / "build"
DIST = BUILD / "dist"
WORK = BUILD / "work"


def get_version() -> str:
    mod = importlib.import_module("modbus_tool")
    return mod.__version__


def write_version_file(version: str) -> pathlib.Path:
    """生成 exe 的版本资源（文件属性里看到的 ProductVersion 等就来自这里）。"""
    parts = [int(x) for x in version.split(".")[:3]]
    while len(parts) < 4:
        parts.append(0)
    v = ",".join(str(x) for x in parts)
    text = f"""# 由 build/build.py 自动生成，勿手改。
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v}), prodvers=({v}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'ModNova Project'),
      StringStruct('FileDescription', 'ModNova - Modbus TCP/RTU master & slave debugging tool'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'ModNova'),
      StringStruct('LegalCopyright', 'MIT License. Bundles CPython (PSF License) and Tcl/Tk.'),
      StringStruct('OriginalFilename', 'ModNova.exe'),
      StringStruct('ProductName', 'ModNova'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"""
    path = BUILD / "version_info.txt"
    path.write_text(text, encoding="utf-8")
    return path


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def build(version: str) -> pathlib.Path:
    venv_pyinstaller = pathlib.Path(sys.executable).parent / "pyinstaller.exe"
    if not venv_pyinstaller.exists():
        sys.exit("pyinstaller not found; run: pip install -r build/requirements-build.txt")

    version_file = write_version_file(version)
    exe_name = "ModNova"
    cmd = [
        str(venv_pyinstaller), "-y", "--clean", "--noconfirm",
        "--windowed", "--name", exe_name,
        "--version-file", str(version_file),
        "--collect-all", "serial",
        "--hidden-import", "serial.tools.list_ports",
        "--distpath", str(DIST), "--workpath", str(WORK),
        "--specpath", str(BUILD),
        str(ROOT / "modnova.py"),
    ]
    run(cmd)
    out = DIST / exe_name
    if not (out / f"{exe_name}.exe").exists():
        sys.exit("build failed: ModNova.exe missing")
    print(f"[OK] {out}")
    return out


def make_zip(src: pathlib.Path, version: str) -> pathlib.Path:
    top = f"ModNova-{version}-win-x64"
    zip_path = DIST / f"{top}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(src.rglob("*")):
            arcname = f"{top}/{path.relative_to(src)}"
            if path.is_file():
                zf.write(path, arcname)
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"[OK] {zip_path}  {size_mb:.1f} MB")
    return zip_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args()

    version = get_version()
    print(f"building ModNova {version} ...")
    shutil.rmtree(DIST / "ModNova", ignore_errors=True)
    out = build(version)
    if not args.no_zip:
        make_zip(out, version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

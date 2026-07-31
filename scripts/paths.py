"""跨平台路径处理：桌面定位、文件名清洗、输出目录构建。

单独成模块的原因：Windows 上的"桌面"并不总是 %USERPROFILE%\\Desktop
——OneDrive 备份开启后会把桌面重定向到 OneDrive 目录，直接拼路径会把
文件写到一个用户在资源管理器里看不到的地方。必须读注册表。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

__all__ = ["desktop_dir", "safe_name", "build_out_dir", "human_size"]


def desktop_dir() -> Path:
    """返回当前用户的桌面目录，取不到时回退到主目录。"""
    if sys.platform == "win32":
        p = _windows_desktop()
        if p:
            return p
    elif sys.platform == "darwin":
        p = Path.home() / "Desktop"
        if p.is_dir():
            return p
    else:
        p = _xdg_desktop()
        if p:
            return p

    fallback = Path.home() / "Desktop"
    return fallback if fallback.is_dir() else Path.home()


def _windows_desktop() -> Path | None:
    """从注册表读取真实桌面路径（能正确处理 OneDrive 重定向）。"""
    try:
        import winreg
    except ImportError:
        return None

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            raw, _ = winreg.QueryValueEx(key, "Desktop")
        expanded = Path(os.path.expandvars(raw))
        if expanded.is_dir():
            return expanded
    except OSError:
        pass
    return None


def _xdg_desktop() -> Path | None:
    """Linux：优先问 xdg-user-dir，它知道本地化后的目录名。"""
    try:
        out = subprocess.run(
            ["xdg-user-dir", "DESKTOP"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode == 0:
            p = Path(out.stdout.strip())
            if p.is_dir():
                return p
    except (OSError, subprocess.SubprocessError):
        pass
    return None


# Windows 保留字符 + 控制字符。全平台统一清洗，保证同一份教材在
# Windows / macOS / Linux 上落到同名目录，方便同步和跨机复现。
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_name(name: str, max_len: int = 120) -> str:
    """把任意字符串清洗成各平台都合法的文件/目录名。

    教材标题里常见全角括号、间隔号（·）、书名号，这些都是合法的，予以保留；
    只处理真正会导致写入失败的字符。
    """
    name = unicodedata.normalize("NFC", name).strip()
    name = _ILLEGAL.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")  # Windows 不允许结尾是点或空格

    if name.upper() in _WIN_RESERVED:
        name = f"_{name}"

    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")

    return name or "untitled"


def build_out_dir(root: Path, grade: str, volume: str, subject: str,
                  version: str, publisher: str) -> Path:
    """按 年级+册次 / 科目 / 版本（出版社） 三层构建输出目录。

    例：~/Desktop/教材下载/三年级上册/数学/苏教版（江苏凤凰教育出版社）/

    任一维度缺失时降级，不留空目录名——宁可层级少一点，也不要出现
    名为 "未知" 的目录让用户困惑。
    """
    level1 = safe_name(f"{grade}{volume}") if (grade or volume) else "未分类"

    if version and publisher:
        level3 = safe_name(f"{version}（{publisher}）")
    else:
        level3 = safe_name(version or publisher or "")

    out = root / level1 / safe_name(subject or "未分类科目")
    if level3:
        out = out / level3
    return out


def human_size(num_bytes: float) -> str:
    """字节数转成人看的大小。"""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{int(num_bytes)} B"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"

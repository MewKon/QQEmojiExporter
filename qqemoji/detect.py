"""自动检测 QQ 数据目录。

检测策略（逐级放宽，全部为只读操作）：

1. Windows 注册表中所有指向真实目录的腾讯相关字符串值；
2. 系统「文档」已知文件夹（能正确处理 OneDrive / 手动重定向）；
3. 若干常见候选路径；
4. 各固定磁盘根目录下 1~4 层的 ``Tencent Files`` 目录。

每个候选最终都要通过「看起来像一个 QQ 数据根目录」的校验才会返回，
即目录下存在以 QQ 号命名的子目录，且其中含有 ``nt_qq`` 或旧版 ``*.db``。
"""

from __future__ import annotations

import os
import string
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

IS_WINDOWS = os.name == "nt"

TENCENT_DIR_NAMES = ("tencent files", "tencentfiles")
NTQQ_DIRNAME = "nt_qq"
LEGACY_EMOJI_DBS = ("CustomFace.db", "FaceStore.db")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Account:
    """一个 QQ 账号的数据目录。"""

    uin: str
    path: str
    has_ntqq: bool = False
    has_legacy: bool = False
    size_bytes: int = 0
    last_used: float = 0.0

    @property
    def display(self) -> str:
        kind = []
        if self.has_ntqq:
            kind.append("NTQQ")
        if self.has_legacy:
            kind.append("旧版QQ")
        suffix = "/".join(kind) if kind else "未知版本"
        return f"{self.uin}（{suffix}）"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["display"] = self.display
        return data


@dataclass
class Root:
    """一个腾讯文件根目录（其下是各账号目录）。"""

    path: str
    reason: str
    accounts: list[Account] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "reason": self.reason,
            "accounts": [a.to_dict() for a in self.accounts],
        }


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _looks_like_uin(name: str) -> bool:
    return name.isdigit() and 5 <= len(name) <= 12


def _dir_size(path: Path, cap: int = 3000, budget: float = 0.35) -> int:
    """粗略统计目录大小（用于排序展示）。

    带条目数和时间预算，避免在几万个文件的账号目录上卡住界面；
    返回的是「下限估计」，仅供展示参考。
    """
    total = 0
    seen = 0
    deadline = time.monotonic() + budget
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except OSError:
                    continue
                seen += 1
                if seen >= cap:
                    return total
            if time.monotonic() > deadline:
                return total
    except OSError:
        pass
    return total


def _newest_mtime(path: Path, cap: int = 1200, budget: float = 0.25) -> float:
    """取目录下最新的文件修改时间，同样带预算。"""
    newest = 0.0
    seen = 0
    deadline = time.monotonic() + budget
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    newest = max(newest, (Path(root) / f).stat().st_mtime)
                except OSError:
                    continue
                seen += 1
                if seen >= cap:
                    return newest
            if time.monotonic() > deadline:
                return newest
    except OSError:
        pass
    return newest


def _describe_account(path: Path) -> Account | None:
    if not path.is_dir() or not _looks_like_uin(path.name):
        return None
    ntqq = (path / NTQQ_DIRNAME).is_dir()
    legacy = any((path / name).exists() for name in LEGACY_EMOJI_DBS)
    if not ntqq and not legacy:
        # 仍可能只有 Image/ 之类的旧目录，保留但标记为未知
        has_any = any((path / n).is_dir() for n in ("Image", "QQ", "MyCollection"))
        if not has_any:
            return None
    return Account(
        uin=path.name,
        path=str(path),
        has_ntqq=ntqq,
        has_legacy=legacy,
        size_bytes=_dir_size(path),
        last_used=_newest_mtime(path),
    )


def enumerate_accounts(root: str | os.PathLike[str]) -> list[Account]:
    """列出某个根目录下的所有账号目录，按最近使用时间倒序。"""
    root_path = Path(root)
    if not root_path.is_dir():
        return []
    accounts: list[Account] = []
    try:
        entries = list(os.scandir(root_path))
    except OSError:
        return []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        acc = _describe_account(Path(entry.path))
        if acc:
            accounts.append(acc)
    accounts.sort(key=lambda a: a.last_used, reverse=True)
    return accounts


# --------------------------------------------------------------------------- #
# 候选来源 1：注册表
# --------------------------------------------------------------------------- #
def _registry_candidates() -> list[tuple[str, str]]:
    """扫描注册表中腾讯相关的字符串值，挑出真实存在的目录。"""
    if not IS_WINDOWS:
        return []
    import winreg

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent"),
    ]

    def visit(hive, subkey: str, depth: int) -> None:
        if depth > 4:
            return
        try:
            with winreg.OpenKey(hive, subkey) as key:
                info = winreg.QueryInfoKey(key)
                for i in range(info[1]):
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        continue
                    if not isinstance(value, str) or len(value) < 4:
                        continue
                    _consider(value, f"注册表 {subkey}\\{name}")
                for i in range(info[0]):
                    try:
                        child = winreg.EnumKey(key, i)
                    except OSError:
                        continue
                    visit(hive, f"{subkey}\\{child}", depth + 1)
        except OSError:
            return

    def _consider(value: str, reason: str) -> None:
        raw = value.strip().strip('"')
        for candidate in _expand_path_candidates(raw):
            key = candidate.lower()
            if key in seen or not os.path.isdir(candidate):
                continue
            seen.add(key)
            found.append((candidate, reason))

    for hive, subkey in roots:
        visit(hive, subkey, 0)
    return found


def _expand_path_candidates(value: str) -> Iterable[str]:
    """把注册表里的字符串值展开成可能的目录（处理各种前缀/环境变量）。"""
    if not value:
        return
    value = value.replace("/", "\\")
    # 常见的 "MyDocument:" 之类前缀
    for prefix in ("MyDocument:", "MyDocuments:", "Documents:"):
        if value.lower().startswith(prefix.lower()):
            rest = value[len(prefix):].lstrip("\\")
            docs = _documents_dir()
            if docs:
                yield str(docs / rest) if rest else str(docs)
    expanded = os.path.expandvars(value)
    yield expanded
    # 值可能直接是一个根目录，也可能指向账户目录，尝试向上回溯
    p = Path(expanded)
    for _ in range(3):
        if p.name.lower() in ("tencent files", "tencentfiles"):
            yield str(p)
            break
        p = p.parent
        if p == p.parent:
            break


def _documents_dir() -> Path | None:
    """获取系统「文档」文件夹（正确处理重定向 / OneDrive）。"""
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_byte * 8),
                ]

            # FOLDERID_Documents = {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            folder_id = GUID(
                0xFDD39AD0,
                0x238F,
                0x46AF,
                (ctypes.c_byte * 8)(*[0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7]),
            )
            ptr = ctypes.c_wchar_p()
            hres = ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(folder_id), 0, None, ctypes.byref(ptr)
            )
            if hres == 0 and ptr.value:
                path = Path(ptr.value)
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                if path.is_dir():
                    return path
        except Exception:
            pass

    for env in ("USERPROFILE", "HOME"):
        base = os.environ.get(env)
        if not base:
            continue
        for sub in ("Documents", "文档", "OneDrive/Documents", "OneDrive/文档"):
            p = Path(base) / sub
            if p.is_dir():
                return p
    return None


# --------------------------------------------------------------------------- #
# 候选来源 2：常见路径
# --------------------------------------------------------------------------- #
def _common_candidates() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    home = Path(os.environ.get("USERPROFILE") or Path.home())
    docs = _documents_dir()

    bases: list[tuple[Path, str]] = []
    if docs:
        bases.append((docs, "系统文档目录"))
    for env, reason in (
        ("USERPROFILE", "用户主目录"),
        ("APPDATA", "AppData\\Roaming"),
        ("LOCALAPPDATA", "AppData\\Local"),
        ("OneDrive", "OneDrive 目录"),
        ("OneDriveConsumer", "OneDrive 目录"),
        ("OneDriveCommercial", "OneDrive 目录"),
    ):
        raw = os.environ.get(env)
        if raw:
            bases.append((Path(raw), reason))

    for base, reason in bases:
        for name in ("Tencent Files", "TencentFiles", "Tencent Files/All Users"):
            p = base / name
            out.append((str(p), f"{reason}下"))
        if base.name.lower() in ("documents", "文档"):
            for name in ("Tencent Files",):
                out.append((str(base / name), f"{reason}下"))

    # 常见的手动迁移位置
    for drive in _fixed_drives():
        out.append((str(Path(drive) / "Tencent Files"), "固定磁盘根目录"))
        out.append((str(Path(drive) / "Documents" / "Tencent Files"), "固定磁盘 Documents 目录"))
        out.append((str(Path(drive) / "QQ" / "Tencent Files"), "固定磁盘 QQ 目录"))
    # 旧版 QQ 安装目录（注册表里的 InstallDir 由上面注册表扫描覆盖）

    return out


def _fixed_drives() -> list[str]:
    if not IS_WINDOWS:
        return ["/"]
    drives: list[str] = []
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << i):
                root = f"{letter}:\\"
                # 2 = DRIVE_REMOVABLE, 3 = DRIVE_FIXED
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:
                    drives.append(root)
    except Exception:
        # 查询磁盘类型失败时退回系统盘。
        # 注意：这里以前写的是 f"{c}:\\"，而 c 从未定义过——真走到这个分支会再抛 NameError。
        drives = [f"{os.environ.get('SystemDrive', 'C:')}\\"]
    return drives


def _shallow_drive_scan(max_depth: int = 3) -> list[tuple[str, str]]:
    """在固定磁盘上浅层搜索名为 Tencent Files 的目录。"""
    out: list[tuple[str, str]] = []
    targets = {"tencent files", "tencentfiles"}
    skip = {
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        "$recycle.bin",
        "system volume information",
        "perflogs",
        "appdata",
        "node_modules",
    }
    for drive in _fixed_drives():
        stack: list[tuple[Path, int]] = [(Path(drive), 0)]
        visited = 0
        while stack and visited < 600:
            current, depth = stack.pop()
            visited += 1
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if entry.name.startswith("$"):
                            continue
                        if entry.name.lower() in targets:
                            out.append((entry.path, f"{drive} 磁盘扫描"))
                            continue
                        if depth + 1 < max_depth and entry.name.lower() not in skip:
                            stack.append((Path(entry.path), depth + 1))
            except (OSError, PermissionError):
                continue
    return out


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def _validate_root(path: str) -> list[Account]:
    """把一个用户/自动检测到的路径解析成账号列表。

    兼容用户手动选择的各种层级：

    * ``…\\Tencent Files``            → 其下所有 QQ 号目录
    * ``…\\Tencent Files\\123456789`` → 该账号目录本身
    * 某个包含 ``Tencent Files`` 的父目录（如 ``D:\\QQ``）
    """
    accounts = enumerate_accounts(path)
    if accounts:
        return accounts

    base = Path(path)
    # 选中的就是某个账号目录
    single = _describe_account(base)
    if single:
        return [single]

    # 选中的是 Tencent Files 的上级目录
    try:
        for entry in os.scandir(base):
            if entry.is_dir() and entry.name.lower() in TENCENT_DIR_NAMES:
                found = enumerate_accounts(entry.path)
                if found:
                    return found
    except OSError:
        pass
    return []


def detect_roots(deep: bool = True, extra_paths: Iterable[str] = ()) -> list[Root]:
    """检测所有可能的 QQ 数据根目录。

    :param deep: 是否执行较慢的磁盘浅层扫描
    :param extra_paths: 用户手动指定的额外路径
    """
    seen: dict[str, Root] = {}

    def add(candidate: str, reason: str) -> None:
        if not candidate:
            return
        try:
            resolved = str(Path(candidate).expanduser().resolve())
        except OSError:
            resolved = candidate
        if not os.path.isdir(resolved):
            return
        key = resolved.lower()
        if key in seen:
            return
        accounts = _validate_root(resolved)
        if not accounts:
            return
        seen[key] = Root(path=resolved, reason=reason, accounts=accounts)

    for path in extra_paths:
        add(str(path), "手动指定")

    for candidate, reason in _registry_candidates():
        add(candidate, reason)

    for candidate, reason in _common_candidates():
        add(candidate, reason)

    if deep:
        for candidate, reason in _shallow_drive_scan():
            add(candidate, reason)

    # 同一个账号可能被多个根目录发现（例如 Documents 与 Documents\Tencent Files），
    # 这里按账号路径去重，并把它归到「最具体」的那个根目录下。
    best: dict[str, tuple[int, str, Account]] = {}
    reasons: dict[str, str] = {}
    for key, root in seen.items():
        reasons[key] = root.reason
        for account in root.accounts:
            account_key = account.path.lower()
            score = len(root.path)
            current = best.get(account_key)
            if current is None or score > current[0]:
                best[account_key] = (score, key, account)

    grouped: dict[str, list[Account]] = {}
    for _score, root_key, account in best.values():
        grouped.setdefault(root_key, []).append(account)

    roots = [
        Root(path=seen[key].path, reason=reasons.get(key, ""),
             accounts=sorted(accounts, key=lambda a: a.last_used, reverse=True))
        for key, accounts in grouped.items()
    ]
    roots.sort(
        key=lambda r: max((a.last_used for a in r.accounts), default=0),
        reverse=True,
    )
    return roots


def detect_table(deep: bool = False, extra_paths: Iterable[str] = ()) -> dict:
    """返回适合前端展示的检测结果。"""
    roots = detect_roots(deep=deep, extra_paths=extra_paths)
    accounts = [a for r in roots for a in r.accounts]
    accounts.sort(key=lambda a: a.last_used, reverse=True)
    return {
        "roots": [r.to_dict() for r in roots],
        "accounts": [a.to_dict() for a in accounts],
        "count": len(accounts),
        "documents_dir": str(_documents_dir() or ""),
        "platform": os.name,
    }


if __name__ == "__main__":  # pragma: no cover - 手动调试
    import json

    print(json.dumps(detect_table(deep=True), ensure_ascii=False, indent=2))

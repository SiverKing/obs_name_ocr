# 版权所有 © 2026 www.siver.top
"""name.txt 目标清单的解析 / 校验 / 序列化（纯逻辑，不依赖 PySide6）。

文件语义与 worker.py 的 parse_targets() 保持一致：

- 空行忽略；
- ``#`` 开头的行是分组表头，可带 ``#R/#F/#P/#M/#N/#D`` 后缀指定该分组特效；
- 其余行是目标文字，归属上方最近的分组表头；
- 第一个表头之前的目标，以及裸 ``#`` 行之后的目标，都属于“未分组”（组名为空字符串）。
  序列化时未分组组排在文件最前面就不写表头，否则写一行裸 ``#``，位置与语义都能保真。

GUI 通过本模块读写 name.txt，worker 仍然按自己的规则热重载同一个文件，
所以这里的解析结果必须和 worker.parse_targets() 完全对齐。
"""

import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

GROUP_STYLE_SUFFIXES: Dict[str, str] = {
    "R": "rainbow",
    "F": "flash",
    "P": "pulse",
    "M": "march",
    "N": "neon",
    "D": "duotone",
}
"""分组表头后缀 → 特效名，必须与 worker.GROUP_STYLE_SUFFIXES 保持一致。"""

STYLE_LETTERS: Dict[str, str] = {style: letter for letter, style in GROUP_STYLE_SUFFIXES.items()}
"""特效名 → 分组表头后缀。"""

ANIMATED_STYLES = frozenset(GROUP_STYLE_SUFFIXES.values())
"""需要动画渲染的特效名集合。"""

UNGROUPED_LABEL = "（未分组）"
"""未分组目标的界面显示名；空字符串才是它的真实名字。"""

WRITE_RETRY_TIMES = 10
WRITE_RETRY_DELAY = 0.06
"""os.replace() 在 Windows 上会被正在读取目标文件的进程挡住（读取方没有共享删除权限）。

worker 每 2 秒才读一次、打开时间只有微秒级，撞上的概率极低；这里做约 0.6 秒的有限重试，
仍然失败就如实报错，而不是改成可能被读到半截内容的就地写入。
"""


class NameGroup:
    """一个分组：名字、特效、组内目标，以及文件里表头前是否留了空行。"""

    __slots__ = ("name", "style", "blank_before", "targets")

    def __init__(
        self,
        name: str = "",
        style: str = "",
        blank_before: bool = False,
        targets: Optional[Sequence[str]] = None,
    ) -> None:
        self.name = str(name)
        self.style = style if style in ANIMATED_STYLES else ""
        self.blank_before = bool(blank_before)
        self.targets: List[str] = [str(target) for target in targets] if targets else []

    @property
    def is_ungrouped(self) -> bool:
        return not self.name

    @property
    def label(self) -> str:
        return self.name or UNGROUPED_LABEL

    def copy(self) -> "NameGroup":
        return NameGroup(self.name, self.style, self.blank_before, list(self.targets))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"NameGroup(name={self.name!r}, style={self.style!r}, targets={len(self.targets)})"


class NameList:
    """name.txt 的内存模型：分组顺序 + 组内目标顺序 + 原始排版细节。"""

    def __init__(
        self,
        groups: Optional[Sequence[NameGroup]] = None,
        newline: str = "\r\n",
        trailing_newline: bool = False,
        source_mtime_ns: Optional[int] = None,
        had_bom: bool = False,
    ) -> None:
        self.groups: List[NameGroup] = [group.copy() for group in groups] if groups else []
        self.newline = newline or "\n"
        self.trailing_newline = bool(trailing_newline)
        self.source_mtime_ns = source_mtime_ns
        self.had_bom = bool(had_bom)
        """原文件是否带 UTF-8 BOM；worker 用 utf-8 读取，带 BOM 会让第一行表头失效。"""
        self.normalize_ungrouped()

    # ---------- 查询 ----------

    def target_total(self) -> int:
        return sum(len(group.targets) for group in self.groups)

    def ungrouped_index(self) -> Optional[int]:
        for index, group in enumerate(self.groups):
            if group.is_ungrouped:
                return index
        return None

    def duplicate_locations(self) -> Dict[str, List[Tuple[int, int]]]:
        """返回重复出现（不区分大小写）的目标：key → 全部出现位置，第一个位置才是生效分组。"""
        locations: Dict[str, List[Tuple[int, int]]] = {}
        for group_index, group in enumerate(self.groups):
            for target_index, target in enumerate(group.targets):
                locations.setdefault(target.casefold(), []).append((group_index, target_index))
        return {key: value for key, value in locations.items() if len(value) > 1}

    def duplicate_total(self) -> int:
        """重复出现的“名字个数”（同一个名字出现 3 次只算 1 个）。"""
        return len(self.duplicate_locations())

    def duplicate_extra_total(self) -> int:
        """多余的重复条目数，等于“清理重复项”会删掉的条数。"""
        return sum(len(locations) - 1 for locations in self.duplicate_locations().values())

    # ---------- 结构维护 ----------

    def normalize_ungrouped(self) -> None:
        """把多个“未分组”合并成一个：原地复用第一个对象，并保留它在文件里的位置。

        不能搬动位置：worker 用 setdefault 让重复目标以“第一次出现”为准，
        改变分组顺序会连带改变生效分组和特效，所以这里只合并、不重排。
        """
        first: Optional[NameGroup] = None
        result: List[NameGroup] = []
        for group in self.groups:
            if group.is_ungrouped:
                if first is None:
                    first = group
                    result.append(group)
                else:
                    first.targets.extend(group.targets)
            else:
                result.append(group)
        self.groups = result

    def ensure_ungrouped(self) -> int:
        """取得“未分组”的下标，不存在就新建一个并放在最前面。"""
        index = self.ungrouped_index()
        if index is None:
            self.groups.insert(0, NameGroup("", "", False))
            index = 0
        return index

    def remove_duplicates(self) -> int:
        """删除后出现的重复目标（保留每个目标第一次出现的位置），返回删除数量。"""
        seen = set()
        removed = 0
        for group in self.groups:
            kept: List[str] = []
            for target in group.targets:
                key = target.casefold()
                if key in seen:
                    removed += 1
                    continue
                seen.add(key)
                kept.append(target)
            group.targets = kept
        return removed


def parse_name_text(content: str) -> Tuple[List[NameGroup], bool]:
    """按 worker 的规则解析 name.txt 文本，返回 (分组列表, 原文件是否以换行结尾)。"""
    groups: List[NameGroup] = []
    current: Optional[NameGroup] = None
    pending_blank = False

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            pending_blank = True
            continue

        if line.startswith("#"):
            header = line[1:].strip()
            base, sep, suffix = header.rpartition("#")
            style = GROUP_STYLE_SUFFIXES.get(suffix.strip().upper(), "") if sep else ""
            if style and base.strip():
                name = base.strip()
            else:
                name, style = header, ""
            current = NameGroup(name=name, style=style, blank_before=pending_blank)
            groups.append(current)
            pending_blank = False
            continue

        if current is None:
            current = NameGroup(name="", style="", blank_before=pending_blank)
            groups.append(current)
            pending_blank = False
        current.targets.append(line)

    trailing_newline = content.endswith(("\n", "\r"))
    return groups, trailing_newline


def serialize_name_text(groups: Sequence[NameGroup], trailing_newline: bool = True) -> str:
    """把分组列表写回 name.txt 文本（保留分组顺序、组内顺序和表头前空行）。

    “未分组”排在文件最前面时不需要表头；不在最前面时写一行裸 ``#`` 回到未分组状态，
    worker 解析 ``#`` 得到的组名同样是空字符串，所以语义等价、位置也能保真。
    """
    lines: List[str] = []
    for group in groups:
        if not group.name and not group.targets:
            continue
        if group.blank_before and lines:
            lines.append("")
        if group.name:
            letter = STYLE_LETTERS.get(group.style, "")
            lines.append(f"# {group.name}" + (f"#{letter}" if letter else ""))
        elif lines:
            lines.append("#")
        lines.extend(group.targets)

    text = "\n".join(lines)
    if text and trailing_newline:
        text += "\n"
    return text


def load_name_list(path: Path) -> NameList:
    """读取 name.txt；文件不存在时返回空清单（不创建文件，新建文件默认以换行结尾）。"""
    if not path.exists():
        return NameList(newline="\r\n", trailing_newline=True, source_mtime_ns=None)

    data = path.read_bytes()
    # worker 用 encoding="utf-8" 读取，带 BOM 会让第一行表头失效；这里按 utf-8-sig 容错解析、
    # 记录 had_bom 让界面提示用户，写回时不带 BOM（保存后 worker 也会恢复正常）。
    had_bom = data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    groups, trailing_newline = parse_name_text(text)
    newline = "\r\n" if b"\r\n" in data else "\n"
    return NameList(
        groups,
        newline=newline,
        trailing_newline=trailing_newline,
        source_mtime_ns=path.stat().st_mtime_ns,
        had_bom=had_bom,
    )


def save_name_list(path: Path, name_list: NameList) -> None:
    """原子写回 name.txt：先写同目录临时文件，再 os.replace() 覆盖，避免 worker 读到半截内容。"""
    name_list.normalize_ungrouped()
    text = serialize_name_text(name_list.groups, name_list.trailing_newline)
    tmp_path = path.with_name(path.name + ".tmp")

    try:
        with tmp_path.open("w", encoding="utf-8", newline=name_list.newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        last_error: Optional[BaseException] = None
        for _ in range(WRITE_RETRY_TIMES):
            try:
                os.replace(tmp_path, path)
                last_error = None
                break
            except PermissionError as exc:  # worker / 杀软可能瞬时占用目标文件
                last_error = exc
                time.sleep(WRITE_RETRY_DELAY)
        if last_error is not None:
            raise last_error
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise

    name_list.source_mtime_ns = path.stat().st_mtime_ns
    name_list.had_bom = False


def validate_group_name(name: str) -> Optional[str]:
    """校验分组名，返回错误提示；None 表示通过。

    判定标准是“写回 ``# 分组名`` 后能否原样解析回来”：名字里最后一个 ``#``
    之后如果（忽略空白后）是合法后缀，就会被 worker 当成特效后缀。
    """
    value = str(name).strip()
    if not value:
        return "分组名不能为空。"
    if value.endswith("#"):
        return "分组名不能以 # 结尾。"
    _, sep, suffix = value.rpartition("#")
    if sep and suffix.strip().upper() in GROUP_STYLE_SUFFIXES:
        letters = "/".join(f"#{letter}" for letter in GROUP_STYLE_SUFFIXES)
        return f"分组名不能以 {letters} 结尾（中间可以有空格），它会被解析成特效后缀。"
    return None


def validate_target_name(name: str) -> Optional[str]:
    """校验目标文字，返回错误提示；None 表示通过。"""
    value = str(name).strip()
    if not value:
        return "目标文字不能为空。"
    if value.startswith("#"):
        return "目标文字不能以 # 开头，它会被 worker 当成分组表头。"
    return None


def split_target_input(text: str) -> Tuple[List[str], int, int]:
    """拆分多行粘贴输入。

    返回 (目标列表, 被忽略的 # 行数, 输入内部重复数)；
    空白行忽略，目标文字按 strip() 归一化，与 worker 的读取规则一致。
    """
    names: List[str] = []
    ignored = 0
    duplicates = 0
    seen = set()

    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            ignored += 1
            continue
        key = line.casefold()
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        names.append(line)

    return names, ignored, duplicates

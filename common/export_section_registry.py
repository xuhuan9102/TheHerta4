"""导出段登记表：导出期写入、后处理期读取的进程内桥接层（无 bpy 依赖）。

背景
----
「条件判断」后处理节点需要知道：某个 draw_ib（物体）在本次导出里产出了哪些 ini 段，
这样当检测哈希在游戏画面上出现时，就能让该物体相关的**全部** ini 段生效，否则不执行。

这件事不能靠扫描导出后的 ini 文本、按段名 / hash 关键词反推：

- VB 段里的 ``hash =`` 是 **VB hash**，不是物体标识；
- hash 风格贴图段（见 ``common/m_ini_helper.py`` 的
  ``[Resource_Texture_<贴图hash>]`` + ``[TextureOverride_<贴图hash>]``）段名与 ``hash =``
  都是**贴图** hash，段名匹配必然漏段。

所以改成「导出的时候就记下来」：``M_IniBuilder`` 写盘前把本节所有段
（段名 + 段类型 + 构造该段时的 draw_ib 上下文）整批提交到本模块的登记表，
后处理节点再按 draw_ib 反查。

生命周期
--------
1. **每次导出开始时**调用 :func:`clear`（一次导出 = 一份全新登记）；
2. **导出期写**：导出器用 :func:`draw_ib_context` 包住「构造 + 提交某个 draw_ib 段」的代码，
   ``M_IniSection`` 在构造时快照当前 draw_ib（``M_IniSection.owner_draw_ib``）；
   ``M_IniBuilder.save_to_file`` / ``save_to_file_not_reorder`` 写盘前调用
   :func:`commit_ini` 整批登记；
3. **后处理期读**：条件判断节点调用 :func:`get_sections_for_draw_ib` /
   :func:`get_sections_for_draw_ibs` 反查出该 draw_ib 的全部段；
4. 导出结束**不**自动清理（后处理还要用），下一次导出开头再 :func:`clear`。

线程安全
--------
不适用：Blender 导出是单线程的，这里只用普通 dict / list，不加锁。

容错
----
任何非法 / 空值一律静默丢弃、不抛异常：导出链路上多一条坏数据不该毁掉整个导出。
"""

import os
from contextlib import contextmanager


# ini_path -> [{"name": str, "type": str, "draw_ib": str, "index": int}, ...]
_TABLES: dict[str, list[dict]] = {}

# 当前正在生成的 ini 路径（register_section 的 ini_path 缺省值）。
_CURRENT_INI: str = ""

# draw_ib 上下文栈：支持嵌套，栈顶即 current_draw_ib()。
_DRAW_IB_STACK: list[str] = []


def _text(value) -> str:
    """把任意值转成去首尾空白的字符串；None / 转换失败一律当成空串。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return ""
    return value.strip()


def _normalize_ini_path(ini_path) -> str:
    """把 ini 路径规范化成绝对路径，作为登记表的键；空值返回空串。"""
    text = _text(ini_path)
    if not text:
        return ""
    try:
        return os.path.normpath(os.path.abspath(text))
    except (OSError, ValueError):
        # 极端情况下（非法字符等）退化成原始文本，仍然可以当键用。
        return text


def _make_entry(name: str, section_type: str, draw_ib: str, index: int) -> dict:
    """构造一条登记记录。"""
    return {
        "name": name,
        "type": section_type,
        "draw_ib": draw_ib,
        "index": index,
    }


def clear() -> None:
    """清空全部登记与上下文（每次导出开始时调用）。"""
    global _CURRENT_INI
    _TABLES.clear()
    _CURRENT_INI = ""
    _DRAW_IB_STACK.clear()


def set_current_ini(ini_path: str) -> None:
    """设置当前 ini 路径（register_section 未显式传 ini_path 时使用）。"""
    global _CURRENT_INI
    _CURRENT_INI = _normalize_ini_path(ini_path)


def get_current_ini() -> str:
    """返回当前 ini 路径（已规范化为绝对路径），未设置时为空串。"""
    return _CURRENT_INI


@contextmanager
def draw_ib_context(draw_ib: str):
    """栈式 draw_ib 上下文：进入时入栈、退出时出栈，支持嵌套（异常也保证恢复）。

    用法::

        with export_section_registry.draw_ib_context(draw_ib):
            ...  # 这里构造的 M_IniSection 会快照 draw_ib
    """
    depth = len(_DRAW_IB_STACK)
    _DRAW_IB_STACK.append(_text(draw_ib))
    try:
        yield
    finally:
        # 按进入时的深度截断：即使内部被 clear() 打乱也不会留下脏栈。
        del _DRAW_IB_STACK[depth:]


def current_draw_ib() -> str:
    """返回当前 draw_ib 上下文（栈顶），无上下文时返回空串。"""
    if not _DRAW_IB_STACK:
        return ""
    return _DRAW_IB_STACK[-1]


def register_section(
    section_name: str,
    section_type: str = "",
    draw_ib: str = "",
    ini_path: str = "",
) -> None:
    """登记单个段（追加语义）。

    - ``ini_path`` 为空时用 :func:`get_current_ini`；
    - ``draw_ib`` 为空时用 :func:`current_draw_ib`；
    - 段名为空、或最终没有可用的 ini 路径时静默忽略；
    - 完全相同的 (段名, 段类型, draw_ib) 重复登记只保留第一条，避免重复累积。

    主要给「逐个 append_section」的旧链路兜底；``M_IniBuilder`` 走的是 :func:`commit_ini`。
    """
    name = _text(section_name)
    if not name:
        return
    ini = _normalize_ini_path(ini_path) or _CURRENT_INI
    if not ini:
        return
    owner = _text(draw_ib) or current_draw_ib()
    table = _TABLES.setdefault(ini, [])
    for entry in table:
        if (
            entry["name"] == name
            and entry["type"] == _text(section_type)
            and entry["draw_ib"] == owner
        ):
            return
    table.append(_make_entry(name, _text(section_type), owner, len(table)))


def commit_ini(ini_path: str, sections) -> None:
    """整批覆盖某个 ini 的登记。

    ``sections`` 为 ``[(section_name, section_type, draw_ib), ...]``，按给定顺序登记，
    并**整批替换**该 ``ini_path`` 原来的全部登记（多轮导出只保留最后一次）。
    传空列表表示「这个 ini 没有段」，等价于清掉该 ini 的登记。

    空 ini_path 时回退到 :func:`get_current_ini`；仍然为空、或 ``sections`` 不是
    list/tuple 时静默忽略（不动已有登记，避免垃圾数据误清表）。
    """
    ini = _normalize_ini_path(ini_path) or _CURRENT_INI
    if not ini:
        return
    if not isinstance(sections, (list, tuple)):
        return

    entries = []
    seen = set()
    for item in sections:
        if not item:
            continue
        try:
            name = _text(item[0])
            section_type = _text(item[1])
            owner = _text(item[2])
        except (IndexError, TypeError, KeyError):
            continue
        if not name:
            continue
        key = (name, section_type, owner)
        if key in seen:
            continue
        seen.add(key)
        entries.append(_make_entry(name, section_type, owner, len(entries)))

    if entries:
        _TABLES[ini] = entries
    else:
        _TABLES.pop(ini, None)


def get_sections_for_draw_ib(draw_ib: str, ini_path: str = "") -> list[dict]:
    """按 draw_ib 反查段，返回登记记录的副本列表（按登记顺序）。

    - ``ini_path`` 为空时：遍历所有 ini（按 ini 首次登记的顺序），每个 ini 内按登记顺序；
    - ``ini_path`` 非空时：只查该 ini；
    - ``draw_ib`` 为空视为无效查询，返回空列表。
    """
    owner = _text(draw_ib)
    if not owner:
        return []
    ini = _normalize_ini_path(ini_path)
    if ini:
        tables = [_TABLES.get(ini, ())]
    else:
        tables = list(_TABLES.values())

    result = []
    for table in tables:
        for entry in table:
            if entry["draw_ib"] == owner:
                result.append(dict(entry))
    return result


def get_sections_for_draw_ibs(draw_ibs, ini_path: str = "") -> list[dict]:
    """批量版本：``draw_ibs`` 可以是 str 或可迭代对象（支持 set 去重）。

    结果按登记顺序返回（先 ini、后段），不会因为入参顺序打乱。
    """
    if draw_ibs is None:
        return []
    if isinstance(draw_ibs, str):
        wanted_items = [draw_ibs]
    else:
        try:
            wanted_items = list(draw_ibs)
        except TypeError:
            return []
    wanted = {_text(item) for item in wanted_items}
    wanted.discard("")
    if not wanted:
        return []

    ini = _normalize_ini_path(ini_path)
    if ini:
        tables = [_TABLES.get(ini, ())]
    else:
        tables = list(_TABLES.values())

    result = []
    for table in tables:
        for entry in table:
            if entry["draw_ib"] in wanted:
                result.append(dict(entry))
    return result


def get_ini_paths() -> list[str]:
    """返回所有已登记 ini 的路径（规范化后的绝对路径，按首次登记顺序）。"""
    return list(_TABLES.keys())


def get_all() -> dict:
    """返回全部登记表的深拷贝，便于调试 / 测试 / 快照对比。"""
    return {
        ini_path: [dict(entry) for entry in entries]
        for ini_path, entries in _TABLES.items()
    }


def summarize() -> str:
    """返回一行摘要：``N 个 ini / M 个段 / K 个 draw_ib``。"""
    total = sum(len(entries) for entries in _TABLES.values())
    owners = set()
    for entries in _TABLES.values():
        for entry in entries:
            owner = entry["draw_ib"]
            if owner:
                owners.add(owner)
    return f"{len(_TABLES)} 个 ini / {total} 个段 / {len(owners)} 个 draw_ib"

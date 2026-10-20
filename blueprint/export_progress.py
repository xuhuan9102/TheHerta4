"""后台导出的细粒度进度中心（worker 进程内使用）。

主进程只能通过会话目录里的 ``stage.json`` 观察后台实例，所以 worker 侧
"导出到哪一步了"都要落到这个文件里。本模块只做四件事：

1. 记录当前阶段、当前节点与节点状态表（pending / running / done）；
2. 原子写 ``stage.json``，写失败一律静默 —— 进度绝不能拖垮导出；
3. 提供 ``elapsed()`` 给主进程显示本次后台导出已经跑了多久；
4. 维护**步骤计数**（``begin_steps`` / ``complete_step``）：``stage.json`` 的
   ``done`` / ``total`` 是"已完成步数 / 总步数"，总步数由主进程按
   「顶层阶段数 + 后处理节点数 + 1(收尾)」预计算后写进 manifest。

步骤模型（谁推进计数）：

- ``advance_stage(name)``：``TimerUtils.start_stage`` 的统一入口 —— 进入新阶段时
  推进**上一个**阶段；第一个阶段不推进；同名阶段重入不推进；``Preprocess-*``
  这类子阶段只更新显示名、不参与计步（它们属于某个顶层阶段内部，计步会撑爆
  预计算的总步数）。
- ``report_node(name, "done")``：节点完成时推进一步；``"running"`` 不推进。
- ``finish_steps()``：收尾分支把计数补到 total（宁可在最后补满，也不早停在 99%）。

与 ``export_background._write_stage`` 的分工：那边写粗粒度阶段（第 N 轮 /
直出导出 / 完成 / 失败），这边写细粒度阶段与节点。两边写同一份文件，所以本
模块写文件时**先读现有内容再合并**，不会把 ``done`` / ``total`` / ``message``
等旧字段冲掉（旧字段在还没 ``begin_steps`` 时继续沿用）。
"""

import json
import os
import time


STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"

#: 模块级进度状态；``done`` / ``total`` 为 None 表示"沿用 stage.json 里的旧值"
_state = {
    "stage": "",
    "node": "",
    "nodes": {},
    "done": None,
    "total": None,
    "started_at": 0.0,
    #: 当前阶段是否还没被"下一步"推进过（防止同一阶段被重复计步）
    "stage_pending": False,
    #: 当前正在执行的节点内部是否启动过阶段（该节点完成时要额外算一步阶段）
    "node_started_stage": False,
}

_stage_path = ""

#: 参与步骤计数的阶段名前缀：只有直出/后台导出的**顶层阶段**计步。
#: ``Preprocess-*``（前处理子阶段）在直出与普通模式里都属于某个顶层阶段内部，
#: 计步会把计数撑过主进程预计算的 total，所以只更新显示名。
_STEP_STAGE_PREFIXES = ("Direct-", "直出-")


def set_stage_file(stage_path: str) -> None:
    """设置本 worker 的进度文件路径，并把状态重置为一次新的导出。"""
    global _stage_path
    _stage_path = str(stage_path or "")
    _state["stage"] = ""
    _state["node"] = ""
    _state["nodes"] = {}
    _state["done"] = None
    _state["total"] = None
    _state["started_at"] = time.time()
    _state["stage_pending"] = False
    _state["node_started_stage"] = False


def clear_stage_file() -> None:
    """解除进度文件（导出结束后调用）；不会删除磁盘上的 stage.json。"""
    global _stage_path
    _stage_path = ""
    _state["stage"] = ""
    _state["node"] = ""
    _state["nodes"] = {}
    _state["done"] = None
    _state["total"] = None
    _state["started_at"] = 0.0
    _state["stage_pending"] = False
    _state["node_started_stage"] = False


# ---------------------------------------------------------------------------
# 步骤计数（stage.json 的 done/total = 已完成步数 / 总步数）
# ---------------------------------------------------------------------------


def _as_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def begin_steps(total) -> None:
    """开始一次导出的步骤计数：``done=0``，``total`` 由主进程预计算（0 = 未知）。"""
    _state["done"] = 0
    _state["total"] = max(0, _as_int(total, 0))
    _write_stage_file()


def complete_step() -> int:
    """完成一步（已知 total 时封顶，未知时只递增）。返回当前 done。"""
    current = _as_int(_state["done"], 0) if _state["done"] is not None else 0
    total = _as_int(_state["total"], 0) if _state["total"] is not None else 0
    done = current + 1
    if total > 0:
        done = min(done, total)
    _state["done"] = done
    _write_stage_file()
    return done


def finish_steps() -> int:
    """收尾：把步骤补满（total 已知时直接置为 total，否则至少算一步）。"""
    total = _as_int(_state["total"], 0) if _state["total"] is not None else 0
    if total > 0:
        _state["done"] = total
    else:
        current = _as_int(_state["done"], 0) if _state["done"] is not None else 0
        _state["done"] = current + 1
    _write_stage_file()
    return _state["done"]


def step_done() -> int:
    """已完成的步骤数。"""
    return max(0, _as_int(_state["done"], 0) if _state["done"] is not None else 0)


def step_total() -> int:
    """总步骤数（0 表示主进程没提供，进度文本会省略分数）。"""
    return max(0, _as_int(_state["total"], 0) if _state["total"] is not None else 0)


def _should_count_stage(stage_name: str) -> bool:
    """只有顶层导出阶段参与计步（``Preprocess-*`` 子阶段只更新显示名）。"""
    name = str(stage_name or "")
    return bool(name) and name.startswith(_STEP_STAGE_PREFIXES)


def advance_stage(stage: str) -> None:
    """进入新阶段：推进上一个阶段（若有且可计步），再记录新阶段名。

    - 第一个阶段不推进（它才刚开始）；
    - 同名阶段重入不推进（直出节点循环里同一步可能多次 ``start_stage``）；
    - 已经推进过的阶段不会再被推进（``stage_pending``），避免"节点完成后
      下一次 ``start_stage`` 又把同一阶段算一遍"；
    - ``Preprocess-*`` 等子阶段只更新显示名、不参与计步；
    - 在某个节点内部启动的阶段会被标记，节点完成时由 ``report_node(done)``
      额外补一步（该阶段与节点各算一步）。
    """
    name = str(stage or "")
    if not name:
        return
    previous = str(_state.get("stage") or "")
    if (
        previous
        and previous != name
        and _state.get("stage_pending")
        and _should_count_stage(previous)
    ):
        complete_step()  # 内部已经写文件
    if _state.get("node"):
        _state["node_started_stage"] = True
    if name != previous:
        _state["stage"] = name
    _state["stage_pending"] = True
    _write_stage_file()


def report_stage(stage: str, done=None, total=None) -> None:
    """更新当前阶段名（**不**推进步骤）；显式传入的 done/total 直接设定计数。

    ``TimerUtils.start_stage`` 的进度桥走 ``advance_stage``，步骤推进由
    ``complete_step``（阶段切换 / 节点完成 / 收尾）控制。
    """
    name = str(stage or "")
    if name:
        _state["stage"] = name
    if done is not None:
        _state["done"] = max(0, _as_int(done, 0))
    if total is not None:
        _state["total"] = max(0, _as_int(total, 0))
    _write_stage_file()


def report_nodes(names) -> None:
    """登记一批节点为 pending（导出开始或某阶段开始时调用）。"""
    for name in names or []:
        key = str(name or "")
        if key:
            _state["nodes"][key] = STATE_PENDING
    _write_stage_file()


def report_node(name: str, status: str) -> None:
    """更新单个节点状态并写 stage.json；节点首次完成时推进一步。

    若该节点执行期间自己启动过导出阶段（例如直出形态键 / 多文件节点），
    完成时再补一步"阶段完成"，让 done 与主进程「阶段 + 节点」的预算对齐。
    """
    key = str(name or "")
    if not key:
        return
    value = str(status or "")
    if not value:
        return

    previous = _state["nodes"].get(key)
    _state["nodes"][key] = value
    if value == STATE_RUNNING:
        _state["node"] = key
    elif _state["node"] == key:
        # 当前节点跑完了：清掉"当前节点"，避免主进程把已完成节点当成正在执行
        _state["node"] = ""

    if value == STATE_DONE and previous != STATE_DONE:
        complete_step()  # 节点本身完成，内部已经写文件
        if _state.get("node_started_stage"):
            complete_step()  # 节点内部启动的那个阶段也完成
            _state["stage_pending"] = False
        _state["node_started_stage"] = False
    else:
        _write_stage_file()


def elapsed() -> float:
    """从本次导出开始（``set_stage_file``）到现在经过的秒数。"""
    started_at = _state.get("started_at") or 0.0
    if not started_at:
        return 0.0
    try:
        return max(0.0, time.time() - float(started_at))
    except (TypeError, ValueError):
        return 0.0


def get_state() -> dict:
    """返回状态快照（测试与调试用，调用方不要就地修改）。"""
    return {
        "stage": _state["stage"],
        "node": _state["node"],
        "nodes": dict(_state["nodes"]),
        "done": _state["done"],
        "total": _state["total"],
        "elapsed": elapsed(),
    }


# ---------------------------------------------------------------------------
# 写文件（全部静默）
# ---------------------------------------------------------------------------


def _write_stage_file() -> None:
    """合并写入 stage.json；任何异常都不向外抛。"""
    path = _stage_path
    if not path:
        return

    try:
        payload = {}
        try:
            with open(path, "r", encoding="utf-8") as file:
                existing = json.load(file)
            if isinstance(existing, dict):
                payload.update(existing)
        except (OSError, ValueError):
            pass

        if _state["stage"] or "stage" not in payload:
            payload["stage"] = str(_state["stage"] or "")

        try:
            payload["done"] = int(_state["done"]) if _state["done"] is not None else int(payload.get("done") or 0)
        except (TypeError, ValueError):
            payload["done"] = 0
        try:
            payload["total"] = int(_state["total"]) if _state["total"] is not None else int(payload.get("total") or 0)
        except (TypeError, ValueError):
            payload["total"] = 0

        payload["node"] = str(_state["node"] or "")
        payload["nodes"] = {str(key): str(value) for key, value in _state["nodes"].items()}
        payload["elapsed"] = round(elapsed(), 3)
        payload["started_at"] = float(_state["started_at"] or 0.0)

        _atomic_write(path, payload)
    except Exception:
        pass


def _atomic_write(file_path: str, payload) -> None:
    """先写 ``.tmp`` 再 os.replace，避免主进程读到半截文件；失败静默。"""
    temp_path = file_path + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, file_path)
        return
    except OSError:
        pass

    try:
        with open(file_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
    except OSError:
        pass
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass

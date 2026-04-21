from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from .log_utils import LOG


@dataclass
class StageTimer:
    stage_name: str
    start_time: datetime = None
    end_time: datetime = None
    duration_seconds: float = 0.0
    is_completed: bool = False
    _accumulated: float = field(default=0.0, repr=False)


@dataclass
class ExportTimerSession:
    session_name: str
    start_time: datetime = None
    end_time: datetime = None
    stages: dict[str, StageTimer] = field(default_factory=dict)
    stage_order: list[str] = field(default_factory=list)
    current_stage: Optional[str] = None


class TimerUtils:
    run_start = None
    run_end = None
    methodname_runstart_dict = {}

    _session: Optional[ExportTimerSession] = None

    @classmethod
    def Start(cls, func_name: str):
        cls.run_start = datetime.now()
        cls.run_end = None
        cls.methodname_runstart_dict[func_name] = cls.run_start

    @classmethod
    def End(cls, func_name: str = ""):
        if cls.run_start is None:
            print("Timer has not been started. Call Start() first.")
            return

        cls.run_end = datetime.now()

        if func_name == "":
            time_diff = cls.run_end - cls.run_start
            print(f"last function time elapsed: {time_diff} ")
        else:
            time_diff = cls.run_end - cls.methodname_runstart_dict.get(func_name, 0)
            print("[" + func_name + f"]已完成,总耗时: {time_diff} ")
        cls.run_start = cls.run_end

    @classmethod
    def start_session(cls, session_name: str):
        cls._session = ExportTimerSession(
            session_name=session_name,
            start_time=datetime.now()
        )
        LOG.info(f"⏱️ 计时会话开始: {session_name}")

    @classmethod
    def start_stage(cls, stage_name: str):
        if cls._session is None:
            cls.start_session("Default Session")

        existing = cls._session.stages.get(stage_name)
        if existing is not None and existing.is_completed:
            existing._accumulated = existing.duration_seconds
            existing.start_time = datetime.now()
            existing.end_time = None
            existing.duration_seconds = 0.0
            existing.is_completed = False
        else:
            stage_timer = StageTimer(
                stage_name=stage_name,
                start_time=datetime.now()
            )
            stage_timer._accumulated = 0.0
            cls._session.stages[stage_name] = stage_timer
            if stage_name not in cls._session.stage_order:
                cls._session.stage_order.append(stage_name)
        cls._session.current_stage = stage_name

    @classmethod
    def end_stage(cls, stage_name: str = None):
        if cls._session is None:
            return

        target_stage = stage_name or cls._session.current_stage
        if target_stage is None:
            return

        stage_timer = cls._session.stages.get(target_stage)
        if stage_timer is None or stage_timer.is_completed:
            return

        stage_timer.end_time = datetime.now()
        stage_timer.duration_seconds = (stage_timer.end_time - stage_timer.start_time).total_seconds() + getattr(stage_timer, '_accumulated', 0.0)
        stage_timer.is_completed = True

        if cls._session.current_stage == target_stage:
            cls._session.current_stage = None

    @classmethod
    def end_session(cls) -> dict:
        if cls._session is None:
            return {}

        if cls._session.current_stage:
            cls.end_stage(cls._session.current_stage)

        cls._session.end_time = datetime.now()
        total_duration = (cls._session.end_time - cls._session.start_time).total_seconds()

        result = {
            'session_name': cls._session.session_name,
            'total_duration': total_duration,
            'stages': {}
        }

        for stage_name in cls._session.stage_order:
            stage_timer = cls._session.stages.get(stage_name)
            if stage_timer:
                result['stages'][stage_name] = stage_timer.duration_seconds

        return result

    @classmethod
    def print_summary(cls):
        result = cls.end_session()
        if not result:
            return

        total = result['total_duration']
        stages = result['stages']

        LOG.info("")
        LOG.info("=" * 60)
        LOG.info("📊 导出耗时统计报告")
        LOG.info("=" * 60)
        LOG.info("")

        max_name_len = max(len(name) for name in stages.keys()) if stages else 10
        max_name_len = max(max_name_len, 10)

        header_format = f"  {{:<{max_name_len + 2}}} {{:>10}}  {{:>8}}"
        row_format = f"  {{:<{max_name_len + 2}}} {{:>10.3f}}s  {{:>7.1f}}%"

        LOG.info(header_format.format("阶段", "耗时", "占比"))
        LOG.info("  " + "-" * (max_name_len + 22))

        for stage_name in cls._session.stage_order:
            duration = stages.get(stage_name, 0)
            percentage = (duration / total * 100) if total > 0 else 0
            LOG.info(row_format.format(stage_name, duration, percentage))

        LOG.info("  " + "-" * (max_name_len + 22))
        LOG.info(f"  {'总计':<{max_name_len + 2}} {total:>10.3f}s  {'100.0':>7}%")
        LOG.info("")
        LOG.info("=" * 60)

        cls._session = None

    @classmethod
    def get_stage_duration(cls, stage_name: str) -> float:
        if cls._session is None:
            return 0.0
        stage_timer = cls._session.stages.get(stage_name)
        if stage_timer:
            return stage_timer.duration_seconds
        return 0.0

    @classmethod
    def get_total_duration(cls) -> float:
        if cls._session is None or cls._session.start_time is None:
            return 0.0
        end = cls._session.end_time or datetime.now()
        return (end - cls._session.start_time).total_seconds()

    @classmethod
    def get_formatted_summary(cls) -> str:
        result = cls.end_session()
        if not result:
            return ""

        total = result['total_duration']
        stages = result['stages']

        lines = []
        lines.append("=" * 50)
        lines.append("📊 导出耗时统计")
        lines.append("=" * 50)

        for stage_name in cls._session.stage_order:
            duration = stages.get(stage_name, 0)
            percentage = (duration / total * 100) if total > 0 else 0
            lines.append(f"  {stage_name}: {duration:.3f}s ({percentage:.1f}%)")

        lines.append("-" * 50)
        lines.append(f"  总耗时: {total:.3f}s")
        lines.append("=" * 50)

        return "\n".join(lines)

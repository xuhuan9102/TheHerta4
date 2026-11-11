"""Assert the deployed fluid_probe.ini matches the known-good parameter set.

Usage:  python tools/check-fluid-params.py [path-to-fluid_probe.ini] [--selftest]
Exit 0 = PASS, 1 = FAIL.
"""
import os
import re
import sys

#: 部署态 ini 的默认位置不再写死（机器各不相同）：优先命令行参数，其次环境变量
#: ``SSMT_FLUID_INI``；两者都没有就报错，别猜。
DEFAULT_INI = os.environ.get("SSMT_FLUID_INI")

WANT = {
    "particle_radius": "0.0028",
    "viscosity": "0",
    "launch_speed": "0.513999999",
    "strength_level": "3.0029",
    "gravity_scale": "0.25",
}
# 这些号段变量在"好状态"里必须不存在（染色/雾化探针关）
MUST_BE_ABSENT = ("x63", "x64")


def read(path):
    return open(path, encoding="utf-8-sig", errors="replace").read().replace("\r\n", "\n")


def check(text):
    problems = []
    for name, want in WANT.items():
        m = re.search(r"(?m)^\s*global\s+\$fluid_%s\s*=\s*([^\s;]+)\s*$" % re.escape(name), text)
        if not m:
            problems.append("MISSING  $fluid_%s（期望 %s）" % (name, want))
        else:
            try:
                got = float(m.group(1)); ref = float(want)
            except ValueError:
                problems.append("PARSE    $fluid_%s = %s（非数字，期望 %s）" % (name, m.group(1), want))
                continue
            # 数值比较，别拿字符串比：3.002899885 与 3.0029 是同一个数
            if abs(got - ref) > 1e-6 * max(1.0, abs(ref)):
                problems.append("DIFF     $fluid_%s = %s（期望 %s）" % (name, m.group(1), want))
    for lane in MUST_BE_ABSENT:
        m = re.search(r"(?m)^\s*%s\s*=" % re.escape(lane), text)
        if m:
            problems.append("PRESENT  %s 不应该被写（探针应关闭）" % lane)
    return problems


def main(argv):
    selftest = "--selftest" in argv
    path = next((a for a in argv[1:] if not a.startswith("--")), DEFAULT_INI)
    if not path:
        print("用法: python tools/check-fluid-params.py <fluid_probe.ini>  或设置 SSMT_FLUID_INI")
        return 2
    text = read(path)
    problems = check(text)

    if selftest:
        # 反例：把 radius 改掉一个字符，必须 FAIL
        bad = text.replace("global $fluid_particle_radius = ", "global $fluid_particle_radius = 9", 1)
        bad_problems = check(bad)
        ok = bool(bad_problems) and problems == []
        print("SELFTEST: %s（反例检出 %d 条）" % ("PASS" if ok else "FAIL", len(bad_problems)))
        return 0 if ok else 1

    print("checked:", path)
    if problems:
        for p in problems:
            print("  " + p)
        print("VERDICT: FAIL")
        return 1
    print("VERDICT: PASS（5 项全等，x63/x64 均未写）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

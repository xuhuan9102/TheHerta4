"""Compare an F8 particle buffer against the preview npz, by slot, in mm.

Buffer layout (FluidParticle, ParticleConfig.hlsl): 16 floats/particle =
  float4 positionAge      (xyz = position, w = age in seconds)
  float4 velocityAlive    (xyz = velocity, w = alive flag)
  float4 predictedLambda  (xyz = predicted position, w = lambda / contact mode)
  float4 densityBirth     (x = density, y = birth time, z = ???, w = spawn kind)
"""
import json
import sys

import numpy as np

STRIDE = 16
CAPACITY = 1024


def load_buffer(path):
    raw = np.fromfile(path, dtype=np.float32)
    if raw.size == 0:
        return None, "empty file (0 bytes)"
    if raw.size % STRIDE:
        return None, "size %d floats is not a multiple of %d" % (raw.size, STRIDE)
    rows = raw.reshape(-1, STRIDE)
    alive = rows[:, 7] > 0.5           # velocityAlive.w
    live = rows[alive]
    if live.size == 0:
        return None, "all-zero / no live particles (velocityAlive.w == 0 everywhere)"
    return live, None


def load_preview(path, frame=-1):
    d = np.load(path)
    pos = d["positions"][frame]
    counts = d["counts"][frame] if "counts" in d.files else len(pos)
    return pos[: int(counts)], d["slots"][frame][: int(counts)] if "slots" in d.files else None


def compare(game_xyz, preview_xyz, trim=0, root=None):
    """Pair by index order; align by root when given, else by centroid (shape-only)."""
    n = min(len(game_xyz), len(preview_xyz))
    if n == 0:
        return None
    g = game_xyz[:n].astype(float)
    p = preview_xyz[:n].astype(float)
    if trim:
        g, p = g[trim:-trim], p[trim:-trim]
    if root is not None:
        p = p + np.asarray(root, dtype=float)   # 游戏是发射器相对坐标 ⇒ 加 root 才是世界坐标
        d_centroid = np.zeros(3)
        aligned_by = "root"
    else:
        d_centroid = g.mean(0) - p.mean(0)
        p = p + d_centroid
        aligned_by = "centroid"
    err = np.linalg.norm(g - p, axis=1) * 1000.0  # mm
    return {
        "n": int(len(err)),
        "aligned_by": aligned_by,
        "centroid_offset_m": [round(float(v), 4) for v in d_centroid],
        "p50_mm": round(float(np.percentile(err, 50)), 3),
        "p95_mm": round(float(np.percentile(err, 95)), 3),
        "max_mm": round(float(err.max()), 3),
    }


def verdict_of(res):
    """质心对齐只能证明形状，绝不给出 SAME。"""
    if res["aligned_by"] == "root":
        return "SAME" if res["p95_mm"] <= 2.0 else (
            "SHAPE-OK-POS-OFF" if res["p95_mm"] <= 10.0 else "WRONG")
    return "SHAPE-ONLY" if res["p95_mm"] <= 2.0 else (
        "SHAPE-ONLY-FAR" if res["p95_mm"] <= 10.0 else "WRONG")


def main(buf, npz, frame=-1):
    live, why = load_buffer(buf)
    if live is None:
        print(json.dumps({"verdict": "NO-DATA", "reason": why}, ensure_ascii=False))
        return 2
    g = live[:, 0:3]
    p, slots = load_preview(npz, frame)
    res = compare(g, p)
    res["verdict"] = verdict_of(res)
    res["game_count"] = int(len(g))
    res["preview_count"] = int(len(p))
    print("CENTROID_OFFSET_M %s" % (res["centroid_offset_m"],))
    print(json.dumps(res, ensure_ascii=False))
    return 0


def selftest(npz, frame=-1):
    """自己跟自己比：必须 p95 = 0.000 mm，否则配对逻辑本身有错。"""
    p, _slots = load_preview(npz, frame)
    res = compare(p, p)
    res["verdict"] = verdict_of(res)
    res["self"] = True
    print(json.dumps(res, ensure_ascii=False))
    ok = res["p95_mm"] == 0.0 and res["max_mm"] == 0.0
    print("SELFTEST %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 3


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2]) if len(sys.argv) > 2 else 1)
    if len(sys.argv) < 3:
        print("usage: fluid-dump-compare.py <buffer.buf> <preview.npz> [frame]")
        print("       fluid-dump-compare.py --selftest <preview.npz> [frame]")
        sys.exit(1)
    sys.exit(main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else -1))

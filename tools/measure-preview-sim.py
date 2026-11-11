"""Measure preview sim for radius 0.0025 vs 0.02: NaN, max|v|, max|p|, neighbour count, cone width."""
import importlib.util as u
import numpy as np
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]

spec = u.spec_from_file_location("sim", str(REPO / "blueprint" / "fluid_preview_sim.py"))
sim = u.module_from_spec(spec)
spec.loader.exec_module(sim)

EM = {'origin': [0.0, 0.0, 1.0], 'direction': [0.0, 0.0, -1.0],
      'up': [1.0, 0.0, 0.0], 'speed': 1.5, 'speed_scale': 1.0,
      'width': 1.0, 'slot': 0, 'level': 4}


def run(radius, frames=180, fps=60.0):
    s = sim.Simulator(1024, [dict(EM)], ground_z=0.0, radius=radius)
    rng = np.random.default_rng(7)
    s.prime(rng)
    for _ in range(frames):
        s.step(1.0 / fps, rng)
    snap = s.snapshot()
    raw = np.asarray(snap[0] if isinstance(snap, tuple) else snap["pos"], dtype=np.float64)
    snap_pos_all = raw.reshape(-1, 3)
    p = snap_pos_all
    finite = np.isfinite(p).all(axis=1)
    p = p[finite]
    vel_all = np.asarray(getattr(s, "vel", np.zeros((0, 3))), dtype=np.float64).reshape(-1, 3)
    alive = np.asarray(getattr(s, "alive", np.zeros(len(vel_all))), dtype=bool)
    if len(vel_all) == len(alive) and alive.any():
        sp = np.linalg.norm(vel_all[alive], axis=1)
    else:
        sp = np.zeros(len(p))
    # 邻域数：H 内核里平均有多少粒子（粗算，随机抽样 200 个）
    idx = np.random.default_rng(1).choice(len(p), size=min(200, len(p)), replace=False)
    d = np.linalg.norm(p[idx][:, None, :] - p[None, :, :], axis=2)
    nb = (d < sim.H).sum(axis=1) - 1
    # 锥形：把高度分 3 段，看每段的横向标准差
    z = p[:, 2]
    lo, hi = np.percentile(z, [5, 95]) if len(p) else (0, 1)
    seg = np.linspace(lo, hi, 4)
    spreads = []
    for i in range(3):
        m = (z >= seg[i]) & (z <= seg[i + 1])
        if m.sum() > 5:
            spreads.append(float(np.std(p[m, :2])))
        else:
            spreads.append(float("nan"))
    return dict(
        radius=radius, n=int(len(p)), nonfinite=int((~finite).sum()),
        max_v=float(sp.max()) if len(sp) else 0.0,
        max_p=float(np.abs(p).max()) if len(p) else 0.0,
        nb_mean=float(nb.mean()) if len(nb) else 0.0,
        spread_bottom=spreads[0], spread_mid=spreads[1], spread_top=spreads[2],
    )


for r in (0.0025, 0.02):
    print(run(r))

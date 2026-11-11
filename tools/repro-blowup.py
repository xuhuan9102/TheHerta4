"""Read-only repro: game-equivalent stepping (2 substeps of dt/2), nu=0.01, sample every 30 frames."""
import importlib.util as u

import numpy as np
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]

SPEC = u.spec_from_file_location("sim", str(REPO / "blueprint" / "fluid_preview_sim.py"))
SIM = u.module_from_spec(SPEC)
SPEC.loader.exec_module(SIM)

EM = [dict(origin=[0.0, 0.0, 1.0], direction=[0.0, 0.0, -1.0], up=[1.0, 0.0, 0.0],
           speed=0.513999999, speed_scale=1.0, width=1.0, slot=0, level=3)]


def run(nu, frames=300, sub=2, radius=0.0025):
    SIM.set_physics(gravity_scale=0.25, viscosity=nu)
    sim = SIM.Simulator(1024, EM, radius=radius)
    rng = np.random.default_rng(7)
    sim.prime(rng)
    rows = []
    for f in range(frames):
        for _ in range(sub):
            sim.step(1.0 / 60.0 / sub, rng)
        if (f + 1) % 30 == 0:
            p, _s, _a = sim.snapshot()
            v = np.asarray(sim.vel)[np.asarray(sim.alive)]
            p = np.asarray(p)
            rows.append((f + 1, len(p),
                         float(np.abs(v).max()) if len(v) else 0.0,
                         float(np.abs(p).max()) if len(p) else 0.0,
                         int(np.isnan(p).sum() + np.isnan(v).sum())))
    return rows


for nu in (0.01, 0.0):
    print("nu =", nu)
    for f, n, vmax, pmax, nan in run(nu):
        print("  frame %3d  n=%4d  max|v|=%9.3f  max|p|=%9.3f  nan=%d" % (f, n, vmax, pmax, nan))

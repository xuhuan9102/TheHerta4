"""Parameterised direction comparison: product direction vs default vertical.

usage: python tools/dir-compare.py <tilt> <yaw> [roll]
       python tools/dir-compare.py --default
Prints, for each direction, the lateral/vertical ratio over 60 frames.
"""
import importlib.util as u
import math
import sys

import numpy as np
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]

SPEC = u.spec_from_file_location("sim", str(REPO / "blueprint" / "fluid_preview_sim.py"))
SIM = u.module_from_spec(SPEC)
SPEC.loader.exec_module(SIM)

MSPEC = u.spec_from_file_location("merge", str(REPO / "blueprint" / "fluid_ini_merge.py"))
MERGE = u.module_from_spec(MSPEC)
MSPEC.loader.exec_module(MERGE)


def run(direction, label):
    em = [dict(origin=[0.0, 0.0, 1.0], direction=list(direction), up=[1.0, 0.0, 0.0],
               speed=0.513999999, speed_scale=1.0, width=1.0, slot=0, level=3)]
    SIM.set_physics(gravity_scale=0.25, viscosity=0.0)
    sim = SIM.Simulator(1024, em, radius=0.0028)
    rng = np.random.default_rng(20261029)
    sim.prime(rng)
    for _ in range(60):
        for _ in range(2):
            sim.step(1.0 / 30.0 / 2.0, rng)
    pos, _s, _a = sim.snapshot()
    p = np.asarray(pos, float)
    lat = float(np.hypot(p[:, 0].max() - p[:, 0].min(), p[:, 1].max() - p[:, 1].min()))
    vert = float(p[:, 2].max() - p[:, 2].min())
    deg = math.degrees(math.atan2(lat, max(vert, 1e-9)))
    print("DIR %-10s lat=%.4f m vert=%.4f m ratio=%.2f deg  (game=5.00 deg)"
          % (label, lat, vert, deg))


if len(sys.argv) >= 3 and sys.argv[1] != "--default":
    tilt, yaw = float(sys.argv[1]), float(sys.argv[2])
    roll = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    fwd, up = MERGE.emitter_direction(tilt, yaw, roll)
    print("emitter_direction(tilt=%g, yaw=%g, roll=%g) -> fwd=(%.5f, %.5f, %.5f) up=(%.5f, %.5f, %.5f)"
          % (tilt, yaw, roll, fwd[0], fwd[1], fwd[2], up[0], up[1], up[2]))
    run(fwd, "product")
run([0.0, 0.0, -1.0], "default")

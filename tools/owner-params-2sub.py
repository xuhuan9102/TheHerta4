"""Owner params, forced 2 substeps, fps=30, 180 frames: is it stable or does it blow up?"""
import importlib.util as u

import numpy as np
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]

SPEC = u.spec_from_file_location("sim", str(REPO / "blueprint" / "fluid_preview_sim.py"))
SIM = u.module_from_spec(SPEC)
SPEC.loader.exec_module(SIM)

EM = [dict(origin=[0.0, 0.0, 1.0], direction=[0.0, 0.0, -1.0], up=[1.0, 0.0, 0.0],
           speed=0.513999999, speed_scale=1.0, width=1.0, slot=0, level=3)]

for nu in (0.01, 0.0):
    SIM.set_physics(gravity_scale=0.25, viscosity=nu)
    job = SIM.SimulationJob(EM, 180, fps=30.0, capacity=1024, radius=0.0028)
    print("nu=%s  sub=%d  subsource=%s  warmup_total=%d  frames=%d"
          % (nu, job.sub, job.substeps_source, job.warmup_total, job.total_frames))
    rows = []
    done = 0
    while done < job.warmup_total:
        job.advance(1)
        done += 1
    for f in range(180):
        job.advance(1)
        if (f + 1) % 30 == 0:
            p, _s, _a = job.sim.snapshot()
            v = np.asarray(job.sim.vel)[np.asarray(job.sim.alive)]
            p = np.asarray(p)
            rows.append((f + 1, len(p), float(np.abs(v).max()), float(np.abs(p).max()),
                         int(np.isnan(p).sum())))
    for r in rows:
        print("  frame %3d n=%4d max|v|=%8.3f max|p|=%8.3f nan=%d" % r)

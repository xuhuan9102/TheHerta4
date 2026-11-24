import ast
from pathlib import Path
from types import SimpleNamespace

import numpy as np

_SOURCE = Path(__file__).resolve().parents[1] / "common" / "zzmi_armature_reconstruction.py"


def _solver(name):
    source = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    function = next(
        node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == name
    )
    namespace = {"np": np}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(_SOURCE), "exec"), namespace)  # noqa: S102
    return namespace[name]


def _limbs():
    heads = np.zeros((15, 3))
    heads[2:6] = ((.2, 0., 1.4), (.4, 0., 1.2), (.6, 0., 1.), (.7, 0., .9))
    heads[7:11] = heads[2:6] * [-1, 1, 1]
    parents = {
        0: None, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 0, 7: 6, 8: 7,
        9: 8, 10: 9, 11: 3, 12: 8, 13: 3, 14: 8,
    }
    pairs = ((1, 6), (2, 7), (3, 8), (4, 9), (5, 10), (11, 12), (13, 14))
    mirrors = dict(pairs) | {b: a for a, b in pairs}
    return heads, parents, mirrors


def test_upper_arm_weights_reparent_only_the_proximal_helpers():
    heads, parents, mirrors = _limbs()
    slots = list(range(100, 115))
    part = SimpleNamespace(
        positions=np.array([[.30, 0., 1.30], [-.30, 0., 1.30], [.47, 0., 1.13], [-.47, 0., 1.13]]),
        indices=np.array([[111], [112], [113], [114]]),
        weights=np.ones((4, 1)),
    )
    corrected = _solver("_upper_arm_helpers")([part], slots, heads, parents, [0, 1, 2, 3, 4, 5], mirrors, 1.7)
    assert corrected == {11: 2, 12: 7}
    assert parents[3] == 2 and parents[8] == 7


def test_a_mirrored_pair_with_inconsistent_weight_support_is_left_alone():
    heads, parents, mirrors = _limbs()
    part = SimpleNamespace(
        positions=np.array([[.30, 0., 1.30], [-.47, 0., 1.13]]),
        indices=np.array([[11], [12]]),
        weights=np.ones((2, 1)),
    )
    assert _solver("_upper_arm_helpers")([part], list(range(15)), heads, parents, [0, 1, 2, 3, 4, 5], mirrors, 1.7) == {}


def _elbow_fixture(right_forearm_t):
    centers, _, mirrors = _limbs()
    axis = centers[4] - centers[2]
    reflected = np.array([-1, 1, 1])
    centers[3] = centers[2] + .40 * axis
    centers[8] = centers[3] * reflected
    arm = [0, 1, 2, 3, 4, 5]
    slots = list(range(100, 115))
    samples = [
        (3, .40, 1), (8, .40, -1), (11, .60, 1), (12, right_forearm_t, -1),
        (13, .46, 1), (13, .54, 1), (14, .46, -1), (14, .54, -1),
    ]
    part = SimpleNamespace(
        positions=np.array([
            (centers[2] + t * axis) * [side, 1, 1] for _, t, side in samples
        ]),
        indices=np.array([[slots[i]] for i, _, _ in samples]),
        weights=np.ones((len(samples), 1)),
    )
    masses = np.bincount(part.indices.ravel() - 100, minlength=len(slots))
    transitions = dict.fromkeys(((3, 11), (8, 12), (3, 13), (8, 14)), np.array([0., 0., 0., .4]))
    return arm, [], mirrors, centers, masses, transitions, [part], slots, 1.7


def test_elbow_keeps_the_upper_segment_and_adds_the_forearm_before_the_wrist():
    assert _solver("_split_elbow_landmark")(*_elbow_fixture(.60)) == [0, 1, 2, 3, 11, 4, 5]


def test_elbow_does_not_promote_a_segment_without_bilateral_forearm_weights():
    assert _solver("_split_elbow_landmark")(*_elbow_fixture(.46)) == [0, 1, 2, 3, 4, 5]

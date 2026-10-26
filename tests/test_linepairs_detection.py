"""Measurement-driven line-block detection: synthetic ground truth + impostors."""

import numpy as np
import pytest

from phantom_qa.analysis import linepairs
from phantom_qa.analysis.common import Ctx
from phantom_qa.phantom_def import load_default
from phantom_qa.registration import Transform


def build_phantom(px_per_mm=7.0, strip_angle=45.0, centers=None, freqs=None,
                  block_mm=12.6, add_impostors=True, seed=5, shift=(0.0, 0.0)):
    """Synthetic phantom: 5 gratings on a strip, plus square outlines and ruler
    marks that a naive detector would mistake for line patterns."""
    rng = np.random.default_rng(seed)
    n = int(300 * px_per_mm)
    img = np.full((n, n), 2400.0)
    T = Transform(A=np.array([[px_per_mm, 0.0], [0.0, -px_per_mm]]),
                  t=np.array([n / 2.0, n / 2.0]))
    yy, xx = np.mgrid[0:n, 0:n].astype(float)
    X = (xx - n / 2) / px_per_mm
    Y = (n / 2 - yy) / px_per_mm

    a = np.deg2rad(strip_angle)
    u = np.array([np.cos(a), np.sin(a)])
    v = np.array([-u[1], u[0]])
    for (cx, cy), f in zip(centers, freqs):
        cx, cy = cx + shift[0], cy + shift[1]
        du = (X - cx) * u[0] + (Y - cy) * u[1]
        dv = (X - cx) * v[0] + (Y - cy) * v[1]
        inside = (np.abs(du) <= block_mm / 2) & (np.abs(dv) <= block_mm / 2)
        # modulation ACROSS the strip, like the real phantom
        img += np.where(inside, 500.0 * np.cos(2 * np.pi * dv * f), 0.0)

    if add_impostors:
        # uniformity-square outlines (axis-aligned thin bright lines)
        for (sx, sy) in ((-95, 95), (95, 95), (95, -95), (-95, -95), (0, 0)):
            for edge in (-25.0, 25.0):
                img += np.where((np.abs(X - sx - edge) < 0.4)
                                & (np.abs(Y - sy) < 25), 900.0, 0.0)
                img += np.where((np.abs(Y - sy - edge) < 0.4)
                                & (np.abs(X - sx) < 25), 900.0, 0.0)
        # ruler marks near an edge (periodic, axis aligned)
        for k in range(6):
            img += np.where((np.abs(Y - (130 - 5.0 * k)) < 0.5)
                            & (np.abs(X) < 12), 1200.0, 0.0)
    img += rng.normal(0, 25.0, img.shape)

    pdef = load_default()
    ctx = Ctx(pixels=img, T=T, pdef=pdef)
    return ctx


@pytest.fixture(scope="module")
def nominal():
    p = load_default()
    centers = [g["center_mm"] for g in p.linepairs["groups"]]
    freqs = [g["freq_lp_mm"] for g in p.linepairs["groups"]]
    return centers, freqs


def test_detects_exactly_five_blocks(nominal):
    centers, freqs = nominal
    ctx = build_phantom(centers=centers, freqs=freqs)
    blocks, angle = linepairs.detect_line_blocks(ctx)
    assert len(blocks) == 5, f"got {len(blocks)} blocks"
    # the seeded layout runs at ~44 deg; the axis is fitted through the centres
    assert abs((angle - 44.0 + 90) % 180 - 90) < 6, f"strip angle {angle}"


def test_block_centers_accurate(nominal):
    centers, freqs = nominal
    ctx = build_phantom(centers=centers, freqs=freqs)
    blocks, angle = linepairs.detect_line_blocks(ctx)
    got = np.array([b["center_mm"] for b in blocks])
    for c in centers:
        d = np.min(np.linalg.norm(got - np.array(c), axis=1))
        assert d < 1.5, f"nearest detected block is {d:.2f} mm from {c}"


def test_impostors_rejected(nominal):
    """Square outlines and ruler marks must not be taken for line groups."""
    centers, freqs = nominal
    ctx = build_phantom(centers=centers, freqs=freqs, add_impostors=True)
    blocks, angle = linepairs.detect_line_blocks(ctx)
    got = np.array([b["center_mm"] for b in blocks])
    # nothing detected near the uniformity squares or the ruler
    for bad in ((-95, 95), (95, 95), (95, -95), (-95, -95), (0, 0), (0, 120)):
        d = np.min(np.linalg.norm(got - np.array(bad), axis=1))
        assert d > 12.0, f"impostor at {bad} was detected as a line group"


def test_tracks_a_shifted_strip(nominal):
    """A phantom whose strip sits 8 mm off the stored nominal is still found —
    this is what makes the app work across phantom variants."""
    centers, freqs = nominal
    shift = (8.0, -6.0)
    ctx = build_phantom(centers=centers, freqs=freqs, shift=shift)
    geom = linepairs.propose(ctx)
    assert all(g["detected"] for g in geom["groups"])
    for g in geom["groups"]:
        nom = np.array(g["nominal_center_mm"])
        got = np.array(g["roi"]["center_mm"])
        expected = nom + np.array(shift)
        assert np.linalg.norm(got - expected) < 2.0, \
            f"{g['id']} landed {got}, expected ~{expected}"


def test_frequencies_recovered(nominal):
    centers, freqs = nominal
    ctx = build_phantom(centers=centers, freqs=freqs)
    geom = linepairs.propose(ctx)
    res = linepairs.compute(ctx, geom)
    for row in res["rows"]:
        lin = row["linearity"]
        assert lin.get("measured_pitch_mm") is not None, row["id"]
        nom_pitch = 1.0 / row["freq_lp_mm"]
        dev = abs(lin["measured_pitch_mm"] - nom_pitch) / nom_pitch
        assert dev < 0.03, f"{row['id']}: pitch off by {100*dev:.1f}%"


def test_modulation_direction_is_measured_in_phantom_coordinates(nominal):
    """The grating direction must be reported in the phantom frame.

    The periodicity map works on an array whose rows run along DECREASING y, so
    a missing sign flip mirrors every angle about the x axis — invisible at
    exactly 45 deg, but wrong everywhere else."""
    centers, freqs = nominal
    for grating_deg in (30.0, 60.0, 120.0):
        ctx = build_phantom(centers=centers, freqs=freqs,
                            strip_angle=grating_deg - 90.0, add_impostors=False)
        blocks, angle = linepairs.detect_line_blocks(ctx)
        assert len(blocks) == 5, f"{grating_deg}: got {len(blocks)} blocks"
        got = np.median([b["mod_dir_deg"] for b in blocks])
        err = abs((got - grating_deg + 90) % 180 - 90)
        assert err < 8, (f"grating at {grating_deg} deg reported as {got:.1f} "
                         f"deg (error {err:.1f})")


def test_strip_axis_follows_block_layout(nominal):
    """Strip axis is fitted through the detected block centres, so it is right
    regardless of whether the printed lines run along or across the strip."""
    centers, freqs = nominal
    ctx = build_phantom(centers=centers, freqs=freqs, add_impostors=False)
    blocks, angle = linepairs.detect_line_blocks(ctx)
    P = np.array(centers, float)
    C = P - P.mean(axis=0)
    _, _, vt = np.linalg.svd(C, full_matrices=False)
    truth = np.degrees(np.arctan2(vt[0][1], vt[0][0])) % 180.0
    assert abs((angle - truth + 90) % 180 - 90) < 4, \
        f"strip angle {angle:.2f} vs layout {truth:.2f}"

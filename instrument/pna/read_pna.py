"""PNA measurement data reader.

Locates representative frequency/power rows in a CSV produced by the PNA
service. Supports two entry modes:

  1. HTTP — registered as ``POST /read_pna`` on the PNA service (:8002).
  2. CLI  — ``python -m instrument.pna.read_pna <csv> [--freqs 1,10,100]``.

Agent traffic should use HTTP (mode 1). The CLI is for ad-hoc human
inspection only.
"""

import argparse
import csv
import sys
from pathlib import Path
from typing import List, Tuple

from instrument.pna.config import PNA_DATA_DIR


def read_key_points(
    csv_path_str: str,
    target_freqs: List[float],
    tolerance_factor: float = 0.05,
) -> Tuple[List[Tuple[float, float]], List[float], str]:
    """Locate the row closest to each target frequency, within a tolerance band.

    Args:
        csv_path_str: Absolute path, or relative path (resolved against
            ``PNA_DATA_DIR``).
        target_freqs: Frequencies in Hz to extract.
        tolerance_factor: Per-frequency tolerance as a fraction of target.
            Default 0.05 matches the legacy ``tmp/read_pna.py`` heuristic
            (tolerance = 0.05 * target).

    Returns:
        Tuple ``(points, missing, resolved_path)`` where:

        - ``points``: list of ``(frequency_hz, power_dbm)`` in target order,
          only including matched targets.
        - ``missing``: target frequencies that had no row within tolerance.
        - ``resolved_path``: absolute path of the CSV actually read.

    Raises:
        FileNotFoundError: if the CSV does not exist.
        ValueError: if ``csv_path_str`` is empty or ``target_freqs`` is empty.
    """
    if not csv_path_str:
        raise ValueError("csv_path is required")
    if not target_freqs:
        raise ValueError("target_freqs must be non-empty")

    csv_path = Path(csv_path_str)
    if not csv_path.is_absolute():
        csv_path = PNA_DATA_DIR / csv_path_str

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    points: List[Tuple[float, float]] = []
    missing: List[float] = []
    for target in target_freqs:
        tol = tolerance_factor * target
        best: dict | None = None
        best_err: float | None = None
        for row in rows:
            freq = float(row["Frequency_Hz"])
            err = abs(freq - target)
            if err <= tol:
                if best is None or err < best_err:
                    best = row
                    best_err = err
        if best is not None:
            points.append((float(best["Frequency_Hz"]), float(best["Power_dBm"])))
        else:
            missing.append(target)

    return points, missing, str(csv_path)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Read key frequency points from a PNA measurement CSV",
    )
    p.add_argument(
        "csv_path",
        help="CSV file (absolute, or relative to data/PNA_data/)",
    )
    p.add_argument(
        "--freqs",
        default="1,10,100,1000,10000,100000,1000000",
        help="Comma-separated target frequencies in Hz "
        "(default: per-decade 1Hz to 1MHz)",
    )
    p.add_argument(
        "--tolerance-factor",
        type=float,
        default=0.05,
        help="Per-frequency tolerance as fraction of target (default 0.05 = 5%)",
    )
    args = p.parse_args()

    targets = [float(x) for x in args.freqs.split(",")]

    try:
        points, missing, resolved = read_key_points(
            args.csv_path, targets, tolerance_factor=args.tolerance_factor
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Key points from {resolved}:")
    print("=" * 40)
    for freq, pwr in points:
        print(f"  {freq:.2f} Hz: {pwr:.2f} dBm")
    if missing:
        print(f"Missing (no row within tolerance): {missing}")


if __name__ == "__main__":
    main()

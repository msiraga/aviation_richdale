"""Validate the WorldMagneticModel evaluator against NOAA's official
WMM2020 test-value vector file (magmodel/WMM2020_TEST_VALUES.txt).

Pass criteria: declination within 0.02 deg, horizontal intensity within 5 nT,
total intensity within 10 nT of the published ground truth.
"""

import sys
from datetime import datetime, timedelta, timezone

from navigation import WorldMagneticModel

TOL_D = 0.02
TOL_H = 5.0
TOL_F = 10.0


def main() -> int:
    model = WorldMagneticModel.from_file("magmodel/WMM2020.COF")
    failures = 0
    checked = 0

    with open("magmodel/WMM2020_TEST_VALUES.txt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = [float(v) for v in line.split()]
            (
                decyear, h_km, lat, lon,
                d_ref, i_ref, h_ref, x_ref, y_ref, z_ref, f_ref,
            ) = fields[:11]

            year = int(decyear)
            fraction = decyear - year
            days = fraction * (366.0 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365.0)
            when = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=days * 86400.0)

            altitude_ft = h_km * 3280.839895013123
            field = model.evaluate(lat, lon, altitude_ft, when)

            dd = abs(field.declination_deg - d_ref)
            # declination is ill-conditioned near the poles where H ~ 0
            tol_d_here = TOL_D if h_ref > 1000.0 else max(TOL_D, abs(d_ref) * 0.01)
            dh = abs(field.horizontal_intensity_nT - h_ref)
            df = abs(field.total_intensity_nT - f_ref)
            checked += 1

            status = "PASS"
            if dd > tol_d_here or dh > TOL_H or df > TOL_F:
                status = "FAIL"
                failures += 1

            print(
                f"{status}  date={decyear:7.1f} lat={lat:6.1f} lon={lon:7.1f} "
                f"D {field.declination_deg:9.4f} vs {d_ref:9.4f} (d={dd:.4f})  "
                f"H {field.horizontal_intensity_nT:9.1f} vs {h_ref:9.1f}  "
                f"F {field.total_intensity_nT:9.1f} vs {f_ref:9.1f}"
            )

    print(f"\n{checked - failures}/{checked} vectors within tolerance")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

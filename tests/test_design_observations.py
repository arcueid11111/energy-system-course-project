import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from backend.models import default_document
from backend.physics import simulate_design
from backend.observations import calibrate_design, parse_observations


def test_custom_sensor_mapping_and_synthetic_calibration_recovery():
    truth = default_document()
    for p in truth.links[1].curve:
        p.head_m *= 0.9
        p.shaft_kw *= 0.9
    r = simulate_design(truth)
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(["timestamp", "pressure_m", "flow_m3h", "power_kw"])
    for p in r["points"]:
        writer.writerow(
            [
                (
                    datetime(2026, 11, 3, tzinfo=timezone.utc) + timedelta(seconds=p["seconds"])
                ).isoformat(),
                p["nodes"]["J3"]["pressure_m"],
                p["links"]["P1"]["flow_m3h"],
                p["power_kw"],
            ]
        )
    result = calibrate_design(
        default_document(),
        {
            "observation_csv": text.getvalue(),
            "measurement_node": "J3",
            "measurement_link": "P1",
            "calibration_pump": "P1",
            "observation_source": "synthetic",
            "source_note": "无噪声同求解器恢复",
        },
    )
    assert result["curve_factor"] == pytest.approx(0.9, abs=0.002)
    assert result["holdout_mse_after"] < result["holdout_mse_before"] * 0.01
    assert result["source"] == "synthetic"
    assert result["holdout_rows"] >= 3


def test_bad_observation_time_and_nonfinite_data():
    with pytest.raises(ValueError):
        parse_observations("timestamp,pressure_m\n2026-11-03T00:00:00,1\n2026-11-03T00:05:00,1")
    with pytest.raises(ValueError):
        parse_observations("timestamp,pressure_m\n2026-11-03T00:00:00Z,nan\n2026-11-03T00:05:00Z,1")

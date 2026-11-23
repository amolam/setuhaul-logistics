"""R3 (release follow-up, 23 Nov 2026): the model's free-text fields are bounded, in the schema the model sees and in
what the code accepts. An over-long value is an off-contract answer: the turn falls back to the rules reader."""
from __future__ import annotations

import pydantic
import pytest

from setuhaul.agent.extraction import RECORD_DRIVER_REPORT, Extraction


def schema_props():
    return RECORD_DRIVER_REPORT["input_schema"]["properties"]


@pytest.mark.parametrize("field,value", [("shipment_ref_mentioned", "S" * 65), ("declared_eta_local", "1" * 17),
                                         ("earliest_start_local", "x" * 17), ("latest_finish_local", "x" * 17),
                                         ("selected_option_id", "o" * 81), ("language", "e" * 17),
                                         ("declared_eta_range_local", ["19:00", "x" * 17])])
def test_over_long_text_is_refused(field, value):
    with pytest.raises(pydantic.ValidationError):
        Extraction(intent="OTHER", **{field: value})


def test_normal_values_still_pass_and_a_malformed_range_is_still_just_dropped():
    e = Extraction(intent="REPORT_DELAY", shipment_ref_mentioned="SYN-SHP-B4F81FA6", declared_eta_local="19:30",
                   declared_eta_range_local=["19:00", "20:00"], selected_option_id="o-" + "a" * 36, language="hi-Latn")
    assert e.declared_eta_range_local == ["19:00", "20:00"]
    assert Extraction(intent="OTHER", declared_eta_range_local=["1", "2", "3"]).declared_eta_range_local is None


def test_the_schema_the_model_sees_carries_the_same_bounds():
    p = schema_props()
    assert p["shipment_ref_mentioned"]["maxLength"] == 64 and p["selected_option_id"]["maxLength"] == 80
    assert p["language"]["maxLength"] == 16 and p["declared_eta_range_local"]["maxItems"] == 2
    assert all(p[k]["maxLength"] == 16 for k in ("declared_eta_local", "earliest_start_local", "latest_finish_local"))

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import sys

import pytest

from backend.wti import oil_d_shadow_store as audit


def test_shadow_store_persists_separate_stages_and_full_evidence(tmp_path):
    path = audit.oil_d_shadow_path(tmp_path)
    store = audit.OilDShadowStore(path)
    store.record("wti-one", 100, "signal", 100.4, {
        "outcome": "candidate", "original_limit": Decimal("0.35"), "original_fair_up": .61,
        "shadow_limit": Decimal("0.36"), "source": "stream", "best_ask": Decimal("0.36"),
        "net_edge": Decimal("0.043"), "ready_at": 102, "received_at": 100.3,
    })
    store.record("wti-one", 100, "execution", 103, {
        "outcome": "skip", "reason": "insufficient_depth", "max_quantity": 2,
        "eligible_quantity": 0, "entry_cap": Decimal("1.00"),
    })
    audit.OilDShadowStore(path)

    report = audit.report(path, 99, 104)
    assert report["signals"] == 1
    assert report["observations"] == 2
    assert report["stages"]["execution"] == {
        "observations": 1, "outcomes": {"skip": 1}, "reasons": {"insufficient_depth": 1},
    }
    assert report["recent"][0]["payload"]["entry_cap"] == "1.00"
    signal = report["recent"][1]["payload"]
    assert signal["original_limit"] == "0.35" and signal["net_edge"] == "0.043"
    assert signal["received_at"] == 100.3


def test_shadow_stage_upsert_is_deterministic_and_cannot_regress(tmp_path):
    path = audit.oil_d_shadow_path(tmp_path)
    store = audit.OilDShadowStore(path)
    parallel = audit.OilDShadowStore(path)
    observations = [103, 108, 104, 102, 106]

    def record(at):
        parallel.record("wti-one", 100, "execution", at, {"sequence": at})

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(record, observations))
    store.record("wti-one", 100, "execution", 108, {"replacement_at_same_time": True})

    assert audit.report(path, 0, 110)["recent"] == [{
        "slug": "wti-one", "signal_at": 100, "stage": "execution", "at": 108, "payload": {"sequence": 108},
    }]


def test_shadow_fills_are_unique_per_contract_across_restarts(tmp_path):
    path = audit.oil_d_shadow_path(tmp_path)
    first = audit.OilDShadowStore(path)
    second = audit.OilDShadowStore(path)
    assert not first.has_fill("wti-one")
    with ThreadPoolExecutor(max_workers=2) as pool:
        racers = [pool.submit(store.record, "wti-one", signal, "execution", 104, {"outcome": "filled"})
                  for store, signal in ((first, 100), (second, 101))]
        assert sorted(task.result() for task in racers) == [False, True]

    restarted = audit.OilDShadowStore(path)
    assert restarted.has_fill("wti-one")
    assert not restarted.has_fill("wti-two")
    assert not restarted.record("wti-one", 102, "execution", 105, {"outcome": "filled"})
    filled = audit.report(path, 100, 105)["recent"][0]
    assert not restarted.record("wti-one", filled["signal_at"], "execution", 106, {"outcome": "skip"})
    assert restarted.has_fill("wti-one")
    assert restarted.record("wti-two", 102, "execution", 105, {"outcome": "filled"})


def test_reporting_bounds_rows_without_truncating_summary_counts(tmp_path):
    path = audit.oil_d_shadow_path(tmp_path)
    store = audit.OilDShadowStore(path)
    for slug in ("wti-one", "wti-two"):
        for stage in ("signal", "execution"):
            store.record(slug, 100, stage, 101, {"outcome": "skip", "reason": "stale"})
    store.record("before-report", 0, "signal", 90, {})
    store.record("after-report", 120, "signal", 121, {})

    result = audit.report(path, 100, 120, limit=1)
    assert result["signals"] == 2 and result["observations"] == 4
    assert len(result["recent"]) == 1
    assert result["stages"]["signal"]["reasons"] == {"stale": 2}


def test_shadow_evidence_rejects_nonfinite_and_pre_signal_times(tmp_path):
    store = audit.OilDShadowStore(audit.oil_d_shadow_path(tmp_path))
    with pytest.raises(ValueError, match="chronological"):
        store.record("wti-one", 100, "signal", 99, {})
    with pytest.raises(ValueError, match="compliant"):
        store.record("wti-one", 100, "signal", 100, {"edge": float("nan")})


def test_read_only_cli_does_not_create_missing_db(tmp_path, monkeypatch, capsys):
    path = audit.oil_d_shadow_path(tmp_path)
    monkeypatch.setattr(sys, "argv", ["oil_d_shadow_store", "--hours", "24", "--json", "--journal", str(path)])
    audit.main()

    result = json.loads(capsys.readouterr().out)
    assert result["signals"] == 0
    assert result["recent"] == []
    assert not path.parent.exists()


def test_text_output_uses_shadow_labels_and_exposes_offer_delta(tmp_path, capsys):
    path = audit.oil_d_shadow_path(tmp_path)
    store = audit.OilDShadowStore(path)
    store.record("wti-one", 100, "evaluation", 100.5, {
        "original_side": "UP", "side": "DOWN", "original_limit": Decimal("0.35"),
        "original_best_ask": Decimal("0.375"), "outcome": "candidate",
    })
    audit._print_report(audit.report(path, 0, 101), 24)

    output = capsys.readouterr().out
    assert "evaluation | candidate 1" in output
    assert "markets 1 | signals 0 | observations 1" in output
    assert "paper side UP | side DOWN | paper limit 0.35 | ask for paper side 0.375 | ask gap ¢ 2.5" in output
    assert "confirmed fills" in output

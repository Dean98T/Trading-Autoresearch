from priority_zero_cutover import rehearse_fixture, run_preflight
from research_lifecycle import ResearchLifecycle


def test_fixture_cutover_rehearsal_completes_without_activating_production(tmp_path):
    report = rehearse_fixture(tmp_path)
    assert report["ready"] is True
    assert report["final_holdout_readiness"]["ready"] is False
    assert report["production_activation"] == "not performed by Priority 0 preflight"
    assert report["rehearsal_record"]["mode"] == "rehearsal"
    assert report["restoration"]["restored"] is True


def test_cutover_preflight_refuses_unquiesced_or_incompatible_writers(tmp_path):
    lifecycle = ResearchLifecycle(tmp_path / "ledger.sqlite3", tmp_path / "artifacts")
    lifecycle.enter_maintenance("preflight fixture")
    report = run_preflight(
        lifecycle=lifecycle,
        snapshot_dir=tmp_path / "missing-snapshot",
        export_path=tmp_path / "current.tsv",
        maintenance_flag=tmp_path / "maintenance.flag",
        process_checker=lambda: [{"pid": "123", "command": "agent_research.py"}],
        compatibility_checker=lambda: {"watchdog.sh": False},
    )
    assert report["ready"] is False
    assert any("writer process" in item for item in report["blockers"])
    assert any("unsupported writer" in item for item in report["blockers"])
    assert any("baseline" in item for item in report["blockers"])

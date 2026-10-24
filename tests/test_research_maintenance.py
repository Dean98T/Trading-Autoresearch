import pytest

from research_maintenance import (
    MaintenanceActiveError,
    assert_research_enabled,
    supported_writer_compatibility,
    write_maintenance_flag,
)


def test_shared_maintenance_flag_blocks_regular_writer_calls(tmp_path, monkeypatch):
    flag = tmp_path / "maintenance.flag"
    write_maintenance_flag("fixture", "fixture-id", flag)
    monkeypatch.setenv("AUTORESEARCH_MAINTENANCE_FLAG", str(flag))

    with pytest.raises(MaintenanceActiveError, match="maintenance flag"):
        assert_research_enabled(flag)


def test_supported_entrypoints_and_supervisors_expose_maintenance_guard():
    status = supported_writer_compatibility()
    assert status
    assert all(status.values()), status

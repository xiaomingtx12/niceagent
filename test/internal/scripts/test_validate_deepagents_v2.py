"""Read-only DeepAgents V2 readiness validator tests."""

from scripts.validate_deepagents_v2 import (
    EXPECTED_HEAD,
    check_database_schema,
    check_migration_graph,
    run_checks,
)


def test_native_migration_graph_has_expected_head():
    """The validator loads the framework's real revision graph."""
    result = check_migration_graph()

    assert result.status == "pass"
    assert EXPECTED_HEAD in result.detail


def test_database_check_is_opt_in():
    """Offline validation never connects to a configured database implicitly."""
    result = check_database_schema(None, required=False)

    assert result.status == "skip"


def test_required_database_without_url_fails():
    """A production database gate cannot silently skip its evidence."""
    result = check_database_schema(None, required=True)

    assert result.status == "fail"


def test_offline_report_contains_all_readiness_dimensions():
    """Offline output still reports database and sandbox evidence gaps."""
    results = run_checks()

    names = {result.name for result in results}
    assert names == {
        "migration_graph",
        "workspace_config",
        "builtin_skills",
        "postgres_schema",
        "sandbox_provider",
    }
    assert all(result.status != "fail" for result in results)

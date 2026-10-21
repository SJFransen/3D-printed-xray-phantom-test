"""Shared revision persistence remains independent of the optional Auto UI."""
import sqlite3

import pytest

from phantom_qa.store import Store
from test_store_labels import fake_scan, results_for


@pytest.fixture
def stored(tmp_path):
    store = Store(str(tmp_path))
    aid = store.new_analysis(fake_scan(), b"source", "signature", "old-version", "definition")
    store.update(aid, geometry={"position": 0}, results=results_for(), status="pass")
    revision = store.audit(aid, "E", "computed", user="Operator")
    return store, aid, revision


def test_revisions_survive_undo_history_replacement(stored):
    store, aid, revision = stored
    before = store.revision(aid, revision)["record"]
    store.set_geometry_baseline(aid, {"position": 10})
    for value in range(40):
        store.mutate_geometry(aid, lambda geometry, value=value: geometry.update(position=value), action="move")
    reopened = Store(store.root)
    assert reopened.revision(aid, revision)["record"] == before
    assert reopened.get(aid)["results"] is None
    assert len(reopened.list_all()) == 1


def test_archived_report_is_not_replaced(stored):
    store, aid, revision = stored
    store.save_revision_report(aid, revision, "Original report")
    store.save_revision_report(aid, revision, "Replacement report")
    assert store.revision(aid, revision)["report_html"] == "Original report"


def test_existing_database_adds_revisions_without_inventing_history(stored):
    store, aid, _ = stored
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TABLE analysis_revisions")
    upgraded = Store(store.root)
    assert upgraded.revisions(aid) == []
    original = upgraded.get(aid)
    upgraded.update(aid, results=None)
    revision = upgraded.revisions(aid)[0]["revision"]
    assert upgraded.revision(aid, revision)["record"] == original


def test_signed_results_cannot_be_overwritten_during_compute(stored):
    store, aid, revision = stored
    expected = store.get(aid)
    store.update(aid, validation_status="validated", validated_by="Technician")
    with pytest.raises(ValueError, match="signed off"):
        store.save_computation(aid, expected, {"new": True}, "fail", 1000)
    assert store.get(aid)["results"] == expected["results"]
    assert store.revision(aid, revision)["record"]["results"] == expected["results"]


def test_analysis_deletion_also_deletes_its_revisions(stored):
    store, aid, revision = stored
    store.delete(aid)
    assert store.revisions(aid) == []
    assert store.revision(aid, revision) is None

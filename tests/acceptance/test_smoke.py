from conftest import HUMAN, fix
from daedalus.core.state_machine import Disposition


def test_happy_path_accepts(ari, repo):
    rid = ari.start({"objective": "fix app"}, actor=HUMAN)
    fix(repo)
    recs = ari.verify(rid)
    assert [r.result for r in recs] == ["PASS"], recs
    d = ari.finish(rid)
    assert d.disposition is Disposition.ACCEPTED, d.to_dict()

"""覆盖率历史入库：快照 / 归档 / 断代的读写，以及从旧 state.json + manifest 导入。"""
import json
import os
import shutil

from covhub.db import importer, repo
from covhub.schemas import ServiceSpec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "data", "covprobe")

SUMMARY = {"INSTRUCTION": {"covered": 18, "total": 22, "pct": 81.818},
           "BRANCH": {"covered": 2, "total": 2, "pct": 100.0},
           "CLASS": {"covered": 1, "total": 1, "pct": 100.0}}


def _svc(name="probe"):
    fields = ServiceSpec(name=name, address="127.0.0.1", port=6399).to_fields()
    repo.add_service(fields)
    return fields


def _entry(at, kind="dump", version="1.0", **extra):
    e = {"at": at, "kind": kind, "version": version, "instruction": 81.82, "branch": 100.0,
         "covered": 18, "total": 22, "classesHit": 1, "classesTotal": 1}
    e.update(extra)
    return e


def test_snapshot_latest_history_order(db):
    _svc()
    for i in range(5):
        repo.add_snapshot("probe", _entry("2026-09-13T10:00:0%d" % i))
    assert repo.latest("probe")["at"] == "2026-09-13T10:00:04"
    hist = repo.history("probe", 3)
    assert [h["at"][-1] for h in hist] == ["2", "3", "4"]      # 最近 3 条、时间正序
    assert "reason" not in hist[0] and "matchRate" not in hist[0]


def test_archive_sets_match_rate_and_clears_session(db):
    _svc()
    repo.set_session_start("probe", "Sun Sep 13 13:11:30 JST 2026")
    entry = repo.add_snapshot("probe", _entry("2026-09-13T11:00:00", kind="predeploy"))
    repo.finish_archive("probe", snapshot_id=entry["id"], version="1.0",
                        archive_dir="versions/1.0", sealed_by="predeploy",
                        sealed_at=entry["at"], match_rate=97.5, exec_count=3, merged="merged.exec")
    assert repo.latest("probe")["matchRate"] == 97.5
    assert repo.get_state("probe")["sessionStart"] is None
    vs = repo.versions("probe")
    assert len(vs) == 1 and vs[0]["dir"] == "1.0" and vs[0]["matchRate"] == 97.5

    # 重启封存不算「已结算版本」，但 sealed_by=None 能看到；断代能对上归档目录
    seal = repo.add_snapshot("probe", _entry("2026-09-13T12:00:00", kind="seal",
                                              reason="restart-detected", sessionStart="A"))
    repo.finish_archive("probe", snapshot_id=seal["id"], version="1.0",
                        archive_dir="versions/1.0-2", sealed_by="restart-detected",
                        sealed_at=seal["at"])
    assert [v["dir"] for v in repo.versions("probe")] == ["1.0"]
    assert [v["dir"] for v in repo.versions("probe", sealed_by=None)] == ["1.0", "1.0-2"]
    b = repo.add_break("probe", at=seal["at"], from_session="A", to_session="B", sealed_as="1.0-2")
    assert b == {"at": "2026-09-13T12:00:00", "from": "A", "to": "B", "sealedAs": "1.0-2"}
    assert repo.breaks("probe", 1) == [b]

    # 按归档目录取历史版本：重启封存的那份也能按目录找到；找不到返回 None 而不是抛
    a = repo.archive_by_dir("probe", "versions/1.0-2")
    assert a["dir"] == "1.0-2" and a["sealedBy"] == "restart-detected" and a["kind"] == "seal"
    assert repo.archive_by_dir("probe", "versions/nope") is None
    # 报表按时间范围取：since 之后的才算，默认只要 predeploy 结算的
    from datetime import datetime
    assert [v["dir"] for v in repo.versions_since("probe")] == ["1.0"]
    assert repo.versions_since("probe", since=datetime(2026, 9, 13, 11, 30)) == []
    assert [v["dir"] for v in repo.versions_since("probe", since=datetime(2026, 9, 13, 11, 30), sealed_by=None)] == ["1.0-2"]


def test_push_mixed_flips_and_records_once(db):
    _svc()
    assert repo.get_state("probe")["pushMixed"] is False
    b = repo.record_push_mixed("probe", True, 3)
    assert b["reason"] == "mixed-versions" and b["instances"] == 3
    assert repo.get_state("probe")["pushMixed"] is True
    assert repo.record_push_mixed("probe", False, 2) is None
    assert repo.get_state("probe")["pushMixed"] is False
    assert len(repo.breaks("probe")) == 1


def test_import_state_from_sample_is_idempotent(db, tmp_path):
    if not os.path.isfile(os.path.join(SAMPLE, "state.json")):
        import pytest
        pytest.skip("data/covprobe 样本不存在")
    data = tmp_path / "data"
    shutil.copytree(SAMPLE, data / "covprobe")
    # 再造一个带 breaks 的 state.json，覆盖旧样本里没有的分支
    state = json.load(open(data / "covprobe" / "state.json", encoding="utf-8"))
    state["breaks"] = [{"at": "2026-09-02T18:53:00", "from": "X", "to": "Y", "sealedAs": "0.1.0-rc1"},
                       {"at": "2026-09-02T18:54:00", "reason": "mixed-versions", "instances": 2}]
    state["sessionStart"] = "Y"
    json.dump(state, open(data / "covprobe" / "state.json", "w", encoding="utf-8"))
    cfg = {"dataDir": str(data), "baseDir": str(tmp_path)}
    _svc("covprobe")

    manifests = [d for d in os.listdir(data / "covprobe" / "versions")
                 if os.path.isfile(data / "covprobe" / "versions" / d / "manifest.json")]
    first = importer.import_state(cfg, "covprobe")
    # 样本目录是活的（本机 hub 还在往里结算），只断言下界与归档数
    assert first["snapshots"] >= len(state["history"]) and first["archives"] == len(manifests)
    assert first["breaks"] == 2
    again = importer.import_state(cfg, "covprobe")
    assert again["snapshots"] == again["archives"] == again["breaks"] == 0
    assert again["skipped"] == first["snapshots"] + first["archives"] + first["breaks"]

    # 最新一条可能是归档 manifest 里比 state.json 更新的 summary，只要求 history 的最后一条被导进来了
    assert any(h["at"] == state["latest"]["at"] for h in repo.history("covprobe", 100))
    assert "0.1.0-rc1" in [v["dir"] for v in repo.versions("covprobe")]
    assert repo.breaks("covprobe")[0]["sealedAs"] == "0.1.0-rc1"
    assert repo.get_state("covprobe")["sessionStart"] == "Y"

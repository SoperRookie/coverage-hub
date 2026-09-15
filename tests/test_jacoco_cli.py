"""jacococli 的命令行分批：一个周期攒下几百个 exec 时，一条命令放不下。

Windows 的 CreateProcess 上限是 32767 字符，实测一天多的 5 分钟轮询就能撞上
（WinError 206）。这里用假的 run_cli 记录每次调用，验证 merge / execinfo / report
在超长时会分批、且每批都在阈值内；有 java 时再用真实 exec 验一次分批合并与一次
合并的结果字节一致。
"""
import glob
import os
import shutil
import subprocess

import pytest

from covhub import jacoco

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = {"jacocoCli": os.path.join(ROOT, "lib", "jacococli.jar")}


def _cmdline(args):
    return len(subprocess.list2cmdline(["java", "-jar", CFG["jacocoCli"]] + args + ["--quiet"]))


@pytest.fixture
def fake_cli(monkeypatch):
    calls = []

    def run_cli(cfg, args, quiet=True):
        assert _cmdline(list(args)) <= jacoco.MAX_CMDLINE, "分批后每条命令都不能超长"
        calls.append(list(args))
        if args[0] == "merge":
            dest = args[args.index("--destfile") + 1]
            with open(dest, "w") as fh:
                fh.write("merged")
        if args[0] == "execinfo":
            return "".join('Session "%s": t0 - t1\n' % os.path.basename(p) for p in args[1:])
        return ""

    monkeypatch.setattr(jacoco, "run_cli", run_cli)
    monkeypatch.setattr(jacoco, "MAX_CMDLINE", 1500)
    return calls


def _many(tmp_path, n=40):
    paths = []
    for i in range(n):
        p = tmp_path / ("snapshot-%03d-with-a-deliberately-long-file-name-to-fill-the-command-line.exec" % i)
        p.write_bytes(b"")
        paths.append(str(p))
    return paths


def test_merge_rolls_over_batches(tmp_path, fake_cli):
    execs = _many(tmp_path)
    dest = str(tmp_path / "merged.exec")
    jacoco.merge_execs(CFG, execs, dest)
    merges = [c for c in fake_cli if c[0] == "merge"]
    assert len(merges) > 1
    # 第一批只有原始文件，后面每批都把上一轮结果带上；所有输入合起来正好覆盖全部文件一次
    assert dest not in merges[0]
    assert all(c[1] == dest for c in merges[1:])
    seen = [p for c in merges for p in c[1:c.index("--destfile")] if p != dest]
    assert seen == execs
    assert open(dest).read() == "merged" and not os.path.exists(dest + ".part")


def test_execinfo_batches_and_concatenates(tmp_path, fake_cli):
    execs = _many(tmp_path)
    sessions = jacoco.exec_sessions(CFG, execs)
    assert [s["id"] for s in sessions] == [os.path.basename(p) for p in execs]
    assert len([c for c in fake_cli if c[0] == "execinfo"]) > 1


def test_report_merges_first_when_too_long(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setattr(jacoco, "summarize", lambda path: {"ok": True})
    execs = _many(tmp_path)
    svc = {"name": "svc", "classfiles": [str(tmp_path / "classes")]}
    jacoco.make_report(CFG, svc, execs, str(tmp_path / "current"), "svc")
    reports = [c for c in fake_cli if c[0] == "report"]
    assert len(reports) == 1 and len(reports[0][1:reports[0].index("--classfiles")]) == 1
    tmp_exec = reports[0][1]
    assert not os.path.exists(tmp_exec), "合并出来的临时 exec 用完要删"


def test_report_untouched_when_short(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setattr(jacoco, "summarize", lambda path: {"ok": True})
    execs = _many(tmp_path, 2)
    svc = {"name": "svc", "classfiles": [str(tmp_path / "classes")]}
    jacoco.make_report(CFG, svc, execs, str(tmp_path / "current"), "svc")
    assert [c[0] for c in fake_cli] == ["report"] and fake_cli[0][1:3] == execs


SAMPLES = sorted(glob.glob(os.path.join(ROOT, "data", "*", "exec", "*.exec")))[:6]


@pytest.mark.skipif(not shutil.which("java") or len(SAMPLES) < 3, reason="需要 java 与 data/ 下的 exec 样本")
def test_batched_merge_matches_single_merge(tmp_path, monkeypatch):
    whole = str(tmp_path / "whole.exec")
    jacoco.run_cli(CFG, ["merge"] + SAMPLES + ["--destfile", whole])
    # 阈值压到刚好一次只放得下一两个文件，逼它走滚动合并
    monkeypatch.setattr(jacoco, "MAX_CMDLINE", _cmdline(["merge", whole, SAMPLES[0], SAMPLES[1], "--destfile", whole + ".part"]))
    rolled = str(tmp_path / "rolled.exec")
    jacoco.merge_execs(CFG, SAMPLES, rolled)
    assert open(rolled, "rb").read() == open(whole, "rb").read()


"""covhub-agent.jar：起真的 JVM，对着真的 PushCollector 验它存在的两个理由。

  1. hub 不在时被测 JVM 照常启动（JaCoCo 的 output=tcpclient 会让它起不来）；
  2. hub 重启后 agent 自己连回来（tcpclient 断了就不再连）。

再加一条 idle：收不到 FIN 的断线靠「多久没指令就重连」兜住。

起 JVM 的两条要 java + javac（现编一个只会睡觉的被测程序），没有就跳过 —— hub 机器通常只有 JRE。
"""
import os
import shutil
import socket
import subprocess
import time

import pytest

from covhub.agent import agent_opts
from covhub.collector import PushCollector
from covhub.exec_format import read_exec_file

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JACOCO_AGENT = os.path.join(ROOT, "lib", "jacocoagent.jar")
COVHUB_AGENT = os.path.join(ROOT, "lib", "covhub-agent.jar")

needs_jdk = pytest.mark.skipif(
    not (shutil.which("java") and shutil.which("javac")),
    reason="需要 JDK（java + javac）来起被测 JVM")

APP = """
public class SleepyApp {
    public static void main(String[] args) throws Exception {
        System.out.println("ready");
        System.out.flush();
        Thread.sleep(120000);
    }
}
"""


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(cond, timeout=20.0, what="条件"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError("等了 %ss，%s 仍未满足" % (timeout, what))


@pytest.fixture
def env(tmp_path):
    """编好被测程序，给出 cfg 与「起一个挂着两个 agent 的 JVM」的函数；收尾时杀干净。"""
    (tmp_path / "SleepyApp.java").write_text(APP, encoding="ascii")
    subprocess.run(["javac", "-d", str(tmp_path), str(tmp_path / "SleepyApp.java")],
                   check=True, capture_output=True)
    port = _free_port()
    cfg = {"dataDir": str(tmp_path / "data"),
           "jacocoAgent": JACOCO_AGENT, "covhubAgent": COVHUB_AGENT,
           "collect": {"port": port, "advertiseAddress": "127.0.0.1"},
           "watch": {"intervalSeconds": 300},
           "services": [{"name": "svc", "channel": "push", "includes": ["SleepyApp"]}]}
    procs, collectors = [], []

    def launch(opts=None):
        # 参数串就用 agent_opts 生成的那一份 —— 测的正是交给用户的东西
        opts = opts or agent_opts(cfg, cfg["services"][0])
        err = open(tmp_path / ("jvm%d.err" % len(procs)), "wb")
        proc = subprocess.Popen(["java"] + opts.split(" ") + ["-cp", str(tmp_path), "SleepyApp"],
                                stdout=subprocess.PIPE, stderr=err)
        procs.append((proc, err))
        return proc

    def collector():
        c = PushCollector(lambda: cfg)
        c.start(port, "127.0.0.1")
        collectors.append(c)
        return c

    def stderr_of(index=0):
        with open(tmp_path / ("jvm%d.err" % index), "rb") as f:
            return f.read().decode("utf-8", "replace")

    yield cfg, launch, collector, stderr_of
    for proc, err in procs:
        proc.kill()
        proc.wait()
        proc.stdout.close()
        err.close()
    for c in collectors:
        c.stop()


@needs_jdk
def test_jvm_starts_without_hub_then_agent_reconnects(env):
    cfg, launch, collector, stderr_of = env
    svc = cfg["services"][0]

    # hub 还没起：JVM 必须照常跑起来
    proc = launch()
    assert proc.stdout.readline().strip() == b"ready"
    assert proc.poll() is None

    # hub 起来了：agent 自己连上，握手那一次的数据直接落盘
    first = collector()
    _wait(lambda: len(first.instances("svc")) == 1, what="agent 连上先起的收集端")
    exec_dir = os.path.join(cfg["dataDir"], "svc", "exec")
    _wait(lambda: os.path.isdir(exec_dir) and os.listdir(exec_dir), what="握手数据落盘")
    assert "cannot reach" in stderr_of() and "connected to" in stderr_of()

    # hub 重启：旧连接全断，新收集端起在同一个端口，agent 要自己连回来
    first.stop()
    second = collector()
    _wait(lambda: len(second.instances("svc")) == 1, what="hub 重启后 agent 重连")
    assert proc.poll() is None

    # 重连后的连接是能取数的：协议没变，数据里有被测类，没有 agent 自己
    assert second.dump_service(cfg, svc) == 1
    names = set()
    for name in os.listdir(exec_dir):
        sessions, execdata = read_exec_file(os.path.join(exec_dir, name))
        assert sessions and sessions[0][0] == "svc"
        names.update(n for _, n, _ in execdata)
    assert names == {"SleepyApp"}


@needs_jdk
def test_idle_timeout_reconnects(env):
    """没指令超过 idle 秒就重连 —— hub 那头掉电、NAT 回收空闲连接时读会永远阻塞。"""
    cfg, launch, collector, _ = env
    c = collector()
    opts = agent_opts(cfg, cfg["services"][0])
    assert "idle=900" in opts                      # 3 × watch.intervalSeconds
    proc = launch(opts.replace("idle=900", "idle=1"))
    assert proc.stdout.readline().strip() == b"ready"
    # seq 每认领一条连接加一：到 2 说明它断开重连过
    _wait(lambda: c.seq >= 2, what="idle 到点后重连")
    assert proc.poll() is None


def test_agent_opts_with_and_without_covhub_agent():
    svc = {"name": "svc", "channel": "push", "includes": ["SleepyApp"]}
    cfg = {"jacocoAgent": "/opt/jacoco/jacocoagent.jar", "covhubAgent": "/opt/jacoco/covhub-agent.jar",
           "collect": {"port": 6400, "advertiseAddress": "covhub.internal"},
           "watch": {"intervalSeconds": 60}}
    jacoco, covhub = agent_opts(cfg, svc).split(" ")
    # JaCoCo 只插桩不联网；连 hub 的事归 covhub-agent。agent 自己和它调的 RT 排除在插桩之外
    assert jacoco == ("-javaagent:/opt/jacoco/jacocoagent.jar=output=none,includes=SleepyApp,"
                      "excludes=covhub.agent.*:org.jacoco.agent.rt.*,sessionid=svc")
    # idle 是三轮轮询，但不低于 180 秒
    assert covhub == "-javaagent:/opt/jacoco/covhub-agent.jar=address=covhub.internal,port=6400,idle=180"
    # 服务自己的 excludes 在前，agent 的两条追加在后
    mine = agent_opts(cfg, dict(svc, excludes=["a.b.*"]))
    assert "excludes=a.b.*:covhub.agent.*:org.jacoco.agent.rt.*," in mine
    # 没配 covhubAgent 的老部署原样不动：仍是一个 -javaagent、output=tcpclient
    legacy = agent_opts({k: v for k, v in cfg.items() if k != "covhubAgent"}, svc)
    assert legacy == ("-javaagent:/opt/jacoco/jacocoagent.jar=output=tcpclient,address=covhub.internal,"
                      "port=6400,includes=SleepyApp,sessionid=svc")
    # pull 通道与 covhub-agent 无关
    pull = agent_opts(cfg, {"name": "p", "address": "10.0.0.1", "port": 6300})
    assert pull == "-javaagent:/opt/jacoco/jacocoagent.jar=output=tcpserver,address=0.0.0.0,port=6300,sessionid=p"

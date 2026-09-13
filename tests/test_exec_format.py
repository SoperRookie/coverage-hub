"""exec 二进制格式：读写往返必须与真实文件字节级一致。

格式常量（magic、版本、块类型、varint / 布尔数组的位序）都是从真实 exec 文件头
实测出来的。jacococli 对某些畸形输入是宽容的，「没报错」不等于「写对了」，
所以这里直接比字节。
"""
import glob
import os

import pytest

from covhub.exec_format import read_exec_file, write_exec_file

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = sorted(glob.glob(os.path.join(ROOT, "data", "*", "exec", "*.exec"))
                 + glob.glob(os.path.join(ROOT, "data", "*", "versions", "*", "exec", "*.exec")))


@pytest.mark.skipif(not SAMPLES, reason="data/ 下没有 exec 样本")
@pytest.mark.parametrize("path", SAMPLES, ids=[os.path.relpath(p, ROOT) for p in SAMPLES])
def test_roundtrip_is_byte_identical(path, tmp_path):
    sessions, execdata = read_exec_file(path)
    assert sessions, "样本里应至少有一条 SessionInfo"
    out = tmp_path / "rt.exec"
    write_exec_file(str(out), sessions, execdata)
    assert out.read_bytes() == open(path, "rb").read()


def test_empty_file_roundtrip(tmp_path):
    out = tmp_path / "empty.exec"
    write_exec_file(str(out), [], [])
    assert read_exec_file(str(out)) == ([], [])
    # 只有 5 字节的头：块类型 + magic + 版本
    assert out.read_bytes() == bytes.fromhex("01c0c01007")

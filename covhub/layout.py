"""数据目录布局。"""

import os
import re

from .errors import CovhubError

# --------------------------------------------------------------------------
# 目录布局
# --------------------------------------------------------------------------
#   <dataDir>/
#     <service>/
#       current/                最新报告（html + jacoco.xml + jacoco.csv + incremental.json）
#       exec/<ts>.exec          历次快照原始数据
#       versions/<version>/     发版结算归档（报告 + exec + manifest）
#       artifacts/<version>/    经 upload-classes 传上来的 class 产物
#       classes/                按 reportExcludes 过滤后的 class 副本
#       unit/<version>/         构建流水线传上来的单测 jacoco.xml + incremental.json
#       diff/<version>.diff     流水线传上来的 git diff 原文 + .lines.json 行号明细
#   历史统计、归档元数据、断代记录在数据库里（db/models.py）
# --------------------------------------------------------------------------

def svc_dir(cfg, svc):
    return os.path.join(cfg["dataDir"], svc["name"])


def ensure_dirs(cfg, svc):
    root = svc_dir(cfg, svc)
    for sub in ("current", "exec", "versions", "classes", "artifacts", "unit", "diff"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def safe_segment(value, what="版本"):
    """要当目录名用的外部输入（版本号等）必须是单段、字符集保守 —— 别让 ../ 溜进路径。"""
    text = str(value or "")
    if not _SEGMENT_RE.match(text) or text in (".", ".."):
        raise CovhubError("%s %r 不能用作目录名：只能用字母、数字、. _ -，且不能以 . 或 - 开头"
                          % (what, text))
    return text

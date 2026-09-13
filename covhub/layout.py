"""数据目录布局。"""

import os

# --------------------------------------------------------------------------
# 目录布局
# --------------------------------------------------------------------------
#   <dataDir>/
#     index.html                看板首页（watch / dump 后自动刷新）
#     <service>/
#       current/                最新报告，看板直接指向这里
#       exec/<ts>.exec          历次快照原始数据
#       versions/<version>/     发版结算归档（报告 + exec + manifest）
#       artifacts/<version>/    经 upload-classes 传上来的 class 产物
#       classes/                按 reportExcludes 过滤后的 class 副本
#       state.json              历史统计，用于趋势
# --------------------------------------------------------------------------

def svc_dir(cfg, svc):
    return os.path.join(cfg["dataDir"], svc["name"])


def ensure_dirs(cfg, svc):
    root = svc_dir(cfg, svc)
    for sub in ("current", "exec", "versions", "classes", "artifacts"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root

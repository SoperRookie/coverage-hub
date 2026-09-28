"""class 产物的上传、存放与打包。"""

import json
import os
import shutil
import tarfile

from .layout import ensure_dirs, safe_segment, svc_dir
from .logbuf import log
from .sources import unpack


def store_classes(cfg, svc, version, blob):
    """把上传的 class 产物解包到 <dataDir>/<service>/artifacts/<version>/。

    有了它，被测服务、发版节点都不必和 hub 共享文件系统：产物 POST 过来即可。
    报告是 hub 出的，class 就必须在 hub 上 —— 且必须是线上跑的那一份。
    """
    ensure_dirs(cfg, svc)
    dest = os.path.join(svc_dir(cfg, svc), "artifacts", safe_segment(version))
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)

    unpack(blob, dest)

    count = sum(len([f for f in files if f.endswith(".class")])
                for _, _, files in os.walk(dest))
    log("%s：已接收 %s 的 class 产物 %d 个 -> %s" % (svc["name"], version, count, dest))
    if not count:
        log("  ! 包里一个 .class 都没有，检查打包方式")
    return dest, count


def classes_sources(cfg, svc, version):
    """找出某个版本的 class 产物在 hub 上的位置，返回 [(打包时的顶层名, 目录)]。

    两个来源，按可信度排序：
      1. artifacts/<版本>/ —— 经 upload-classes 传上来的，一定是那次发版的产物
      2. versions/<版本>/manifest.json 里记的 classfiles —— 结算时实际用来出报告的路径

    配置里当前的 classfiles 不算数：它早就跟着新版本改掉了。
    """
    root = svc_dir(cfg, svc)
    version = safe_segment(version)
    uploaded = os.path.join(root, "artifacts", version)
    if os.path.isdir(uploaded) and os.listdir(uploaded):
        return [("", uploaded)]

    manifest = os.path.join(root, "versions", version, "manifest.json")
    if os.path.isfile(manifest):
        try:
            with open(manifest, encoding="utf-8") as f:
                paths = json.load(f).get("classfiles") or []
        except (ValueError, OSError):
            paths = []
        found = [(("cp%d" % i), path) for i, path in enumerate(paths) if os.path.isdir(path)]
        if found:
            # 只有一份时不套目录，解出来直接就是包结构
            return [("", found[0][1])] if len(found) == 1 else found
    return []


def pack_classes(cfg, svc, version, dest):
    """把该版本的 class 产物打成 tar.gz 写到 dest，返回 (class 数, 字节数)。

    发版节点因此不必自己留一份 class 产物：要用时从 hub 取回即可。
    """
    sources = classes_sources(cfg, svc, version)
    if not sources:
        raise RuntimeError(
            "hub 上没有 %s 版本 %s 的 class 产物。"
            "该版本发版时没跑过 upload-classes，或结算时用的 classfiles 已经不在了。"
            % (svc["name"], version))

    count = 0
    with tarfile.open(dest, "w:gz") as tf:
        for top, path in sources:
            for dirpath, _, files in os.walk(path):
                for name in files:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, path).replace("\\", "/")
                    tf.add(full, arcname=("%s/%s" % (top, rel)) if top else rel)
                    if name.endswith(".class"):
                        count += 1
    return count, os.path.getsize(dest)

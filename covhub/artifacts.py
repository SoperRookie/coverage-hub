"""class 产物的上传、存放与打包。"""

import json
import os
import shutil
import tarfile
import zipfile

from .layout import ensure_dirs, safe_segment, svc_dir
from .logbuf import log

def _members_ok(names):
    """压缩包来自流水线，仍按不可信输入处理：绝对路径、跳出目录一律拒绝。"""
    for name in names:
        clean = name.replace("\\", "/")
        if clean.startswith("/") or ".." in clean.split("/") or ":" in clean.split("/")[0][1:2]:
            raise RuntimeError("压缩包里有不安全的路径：%s" % name)


def _common_prefix(names):
    """构建期打包习惯上会带一层顶层目录（coverage-artifacts/），自动剥掉。"""
    tops = {n.replace("\\", "/").split("/")[0] for n in names if n.strip("/")}
    if len(tops) != 1:
        return ""
    top = tops.pop()
    return top + "/" if any(n.replace("\\", "/").startswith(top + "/") for n in names) else ""


def store_classes(cfg, svc, version, blob):
    """把上传的 class 产物解包到 <dataDir>/<service>/artifacts/<version>/。

    有了它，被测服务、发版节点都不必和 hub 共享文件系统：产物 POST 过来即可。
    报告是 hub 出的，class 就必须在 hub 上 —— 且必须是线上跑的那一份。
    """
    ensure_dirs(cfg, svc)
    dest = os.path.join(svc_dir(cfg, svc), "artifacts", safe_segment(version))
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)

    if zipfile.is_zipfile(blob):
        with zipfile.ZipFile(blob) as zf:
            names = zf.namelist()
            _members_ok(names)
            prefix = _common_prefix(names)
            for name in names:
                if name.endswith("/"):
                    continue
                rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
                target = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    else:
        with tarfile.open(blob, "r:*") as tf:
            members = [m for m in tf.getmembers() if m.isfile() or m.isdir()]
            _members_ok([m.name for m in members])
            prefix = _common_prefix([m.name for m in members])
            for m in members:
                if not m.isfile():
                    continue
                rel = m.name[len(prefix):] if prefix and m.name.startswith(prefix) else m.name
                target = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    continue
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)

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

    发版节点因此不必自己留一份 class 产物：推 Sonar 时从 hub 取回即可。
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

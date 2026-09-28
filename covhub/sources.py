"""按版本存放的源码：流水线传上来，出报告、算新增代码、看源码都按版本取。

hub 独立部署后，本机 checkout 源码（服务配置里的 sourcefiles）既要 git 权限又只能对一个版本；
源码跟着版本走才能让历史版本的报告和新增代码视图一直是对的。

    sources/<version>/            仓库相对路径原样保留（diff 里的路径可直接命中）
    sources/<version>/.roots.json 识别出的源码根（jacococli --sourcefiles 要的是包路径的根）

压缩包的解包工具也在这里 —— class 产物（artifacts.py）共用，这一层比 jacoco 低，jacoco 要用它。
"""

import json
import os
import re
import shutil
import tarfile
import zipfile
from datetime import datetime

from .errors import CovhubError
from .layout import safe_segment, svc_dir
from .logbuf import log

# 只收源码。有人把整个仓库传上来时，配置文件里的密码不能跟着落到 hub
SOURCE_EXTS = (".java", ".kt", ".groovy", ".scala")
ROOTS_FILE = ".roots.json"

_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)", re.MULTILINE)


# ---- 解包（class 产物与源码共用） ----

def members_ok(names):
    """压缩包来自流水线，仍按不可信输入处理：绝对路径、跳出目录一律拒绝。"""
    for name in names:
        clean = name.replace("\\", "/")
        if clean.startswith("/") or ".." in clean.split("/") or ":" in clean.split("/")[0][1:2]:
            raise RuntimeError("压缩包里有不安全的路径：%s" % name)


def common_prefix(names):
    """构建期打包习惯上会带一层顶层目录（coverage-artifacts/），自动剥掉。"""
    tops = {n.replace("\\", "/").split("/")[0] for n in names if n.strip("/")}
    if len(tops) != 1:
        return ""
    top = tops.pop()
    return top + "/" if any(n.replace("\\", "/").startswith(top + "/") for n in names) else ""


def unpack(blob, dest, keep=None, strip=True):
    """把 zip / tar(.gz) 解到 dest；keep(相对路径) 为假的成员跳过。

    strip 时剥掉唯一的顶层目录（class 产物的习惯）。源码不剥：从仓库根打的包里只有一个
    模块时，那一层正是 diff 路径的一部分，剥了就和 diff 对不上了。

    返回写出的文件数。
    """
    written = 0

    def target_of(name, prefix):
        rel = name.replace("\\", "/")
        rel = rel[len(prefix):] if prefix and rel.startswith(prefix) else rel
        if not rel or (keep is not None and not keep(rel)):
            return None
        return os.path.join(dest, rel)

    if zipfile.is_zipfile(blob):
        with zipfile.ZipFile(blob) as zf:
            names = zf.namelist()
            members_ok(names)
            prefix = common_prefix(names) if strip else ""
            for name in names:
                if name.endswith("/"):
                    continue
                target = target_of(name, prefix)
                if target is None:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                written += 1
    else:
        try:
            tf = tarfile.open(blob, "r:*")
        except tarfile.TarError:
            raise RuntimeError("不是 zip 也不是 tar / tar.gz 压缩包")
        with tf:
            members = [m for m in tf.getmembers() if m.isfile() or m.isdir()]
            members_ok([m.name for m in members])
            prefix = common_prefix([m.name for m in members]) if strip else ""
            for m in members:
                if not m.isfile():
                    continue
                target = target_of(m.name, prefix)
                if target is None:
                    continue
                src = tf.extractfile(m)
                if src is None:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                written += 1
    return written


# ---- 存 ----

def _is_main_source(rel):
    """源码扩展名，且不是测试代码 —— 测试代码不进覆盖率统计，diff 解析也同样丢掉它。"""
    rel = rel.replace("\\", "/")
    if not rel.lower().endswith(SOURCE_EXTS):
        return False
    return not (rel.startswith("src/test/") or "/src/test/" in rel)


def _copy_tree(src_dir, dest):
    written = 0
    for dirpath, dirnames, files in os.walk(src_dir):
        dirnames[:] = [d for d in dirnames if d not in (".git", "target", "build", "node_modules")]
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, src_dir).replace("\\", "/")
            if not _is_main_source(rel):
                continue
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(full, target)
            written += 1
    return written


def detect_roots(base, encoding="UTF-8"):
    """按每个文件声明的 package 反推源码根（相对 base，"" 表示 base 本身）。

    不按 src/main/java 这种约定猜：非标准布局、sources.jar 平铺、多模块都能对上。
    声明的包和目录对不上的文件（Kotlin 允许）jacococli 本来也找不到，跳过。
    """
    roots = set()
    for dirpath, _, files in os.walk(base):
        for name in files:
            if not name.lower().endswith(SOURCE_EXTS):
                continue
            full = os.path.join(dirpath, name)
            rel_dir = os.path.relpath(dirpath, base).replace("\\", "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            try:
                with open(full, encoding=encoding, errors="replace") as f:
                    head = f.read(16384)
            except OSError:
                continue
            m = _PACKAGE_RE.search(head)
            if not m:
                roots.add(rel_dir)              # 默认包
                continue
            pkg_path = m.group(1).replace(".", "/")
            if rel_dir == pkg_path:
                roots.add("")
            elif rel_dir.endswith("/" + pkg_path):
                roots.add(rel_dir[:-len(pkg_path) - 1])
    return sorted(roots)


def store_sources(cfg, svc, version, src):
    """把某个版本的源码（压缩包或目录）存到 sources/<version>/，整份替换。

    先解到临时目录再换上去：解到一半失败时，旧的那份还在。
    """
    version = safe_segment(version)
    parent = os.path.join(svc_dir(cfg, svc), "sources")
    os.makedirs(parent, exist_ok=True)
    dest = os.path.join(parent, version)
    tmp = os.path.join(parent, ".%s.tmp" % version)      # safe_segment 不允许 . 开头，撞不上版本目录
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        if os.path.isdir(src):
            count = _copy_tree(src, tmp)
        else:
            count = unpack(src, tmp, keep=_is_main_source, strip=False)
        if not count:
            raise CovhubError("包里一个源码文件（%s，测试代码除外）都没有，检查打包方式"
                              % " / ".join(SOURCE_EXTS))
        roots = detect_roots(tmp, svc.get("sourceEncoding", "UTF-8"))
        with open(os.path.join(tmp, ROOTS_FILE), "w", encoding="utf-8") as f:
            json.dump({"version": version, "files": count, "roots": roots,
                       "uploadedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False, indent=2)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    log("%s：已接收版本 %s 的源码 %d 个文件，识别出 %d 个源码根 -> %s"
        % (svc["name"], version, count, len(roots), dest))
    for r in roots[:20]:
        log("  %s" % (r or "."))
    if len(roots) > 20:
        log("  …共 %d 个" % len(roots))
    return {"path": dest, "files": count, "roots": roots}


# ---- 取 ----

def sources_dir(cfg, svc, version):
    """该版本上传过源码就返回目录，否则 None。版本串拼不成目录名（auto_version 之类）也是 None。"""
    if not version:
        return None
    try:
        v = safe_segment(version)
    except CovhubError:
        return None
    path = os.path.join(svc_dir(cfg, svc), "sources", v)
    return path if os.path.isfile(os.path.join(path, ROOTS_FILE)) else None


def _uploaded_roots(path):
    try:
        with open(os.path.join(path, ROOTS_FILE), encoding="utf-8") as f:
            roots = json.load(f).get("roots") or []
    except (OSError, ValueError):
        roots = []
    return [os.path.normpath(os.path.join(path, r)) if r else path for r in roots]


def _config_trusted(svc, version):
    """服务配置里的 sourcefiles 只对「当前版本」可信：它是一个会跟着发版改掉的目录，
    拿它去对旧版本的行号只会错位 —— 错的代码比没有代码更糟。"""
    return not version or svc.get("version") in (None, version)


def source_roots(cfg, svc, version):
    """出报告用的源码根：该版本上传过就用上传的，否则回落服务配置里的 sourcefiles。"""
    path = sources_dir(cfg, svc, version)
    if path:
        return _uploaded_roots(path)
    return list(svc.get("sourcefiles") or []) if _config_trusted(svc, version) else []


def _inside(root, rel):
    full = os.path.realpath(os.path.join(root, rel))
    base = os.path.realpath(root)
    return full if full == base or full.startswith(base + os.sep) else None


def find_source(cfg, svc, version, path, report_file=None):
    """找某个版本的某个源码文件，返回 (按行拆开的文本, 来源描述)；找不到返回 (None, None)。

    path 是 diff 里的仓库相对路径，report_file 是 JaCoCo 报告里的 包/文件名。
    """
    candidates = []
    uploaded = sources_dir(cfg, svc, version)
    if uploaded:
        candidates.append((uploaded, path, "sources/%s" % os.path.basename(uploaded)))
        for root in _uploaded_roots(uploaded):
            candidates.append((root, report_file or path, "sources/%s" % os.path.basename(uploaded)))
    if _config_trusted(svc, version):
        for root in svc.get("sourcefiles") or []:
            candidates.append((root, report_file or path, None))
        if cfg.get("baseDir"):
            candidates.append((cfg["baseDir"], path, None))
    for root, rel, label in candidates:
        if not rel:
            continue
        full = _inside(root, rel)
        if full and os.path.isfile(full):
            with open(full, encoding=svc.get("sourceEncoding", "UTF-8"), errors="replace") as f:
                return f.read().splitlines(), label or full
    return None, None

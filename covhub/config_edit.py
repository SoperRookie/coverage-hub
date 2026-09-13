"""YAML / JSON 配置的行级改写（阶段②服务入库后删除）。"""

import json
import os
import re

# --------------------------------------------------------------------------
# YAML 行级改写
#
# retarget 每次发版都会回写配置。「解析成 dict 再整体 dump」的写法会把注释和排版
# 一起抹掉，而能写注释正是配置换成 YAML 的理由 —— 所以这里只定位目标服务的那几
# 行做替换，其余原文逐字不动。代价是只认缩进块写法，流式 {a: 1} 会直接报错。
# --------------------------------------------------------------------------

_YAML_PLAIN = re.compile(r"^[A-Za-z0-9_./][A-Za-z0-9_./+@=~-]*$")
_YAML_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_.\-]*)\s*:(\s|$)")
_YAML_ITEM = re.compile(r"^(\s*)-(\s|$)")


def _yaml_scalar(value):
    """把标量渲染成 YAML。拿不准就加引号 —— 引号从不会解析错，裸值会。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if not _YAML_PLAIN.match(text):
        return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')
    if text.lower() in ("true", "false", "null", "yes", "no", "on", "off", "~"):
        return '"%s"' % text
    if text[0].isdigit():
        # 版本号裸写会被读成数字（1.4 → float，1 → int），一律引起来
        return '"%s"' % text
    return text


def _yaml_split_comment(text):
    """切成 (正文, 行尾注释, 注释起始列)。# 在引号里不算注释。"""
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i], text[i:].strip(), i
    return text, "", 0


def _yaml_append_comment(line, comment, col):
    """把行尾注释接回去，尽量还原它原来的列，读起来才不会错位。"""
    if not comment:
        return line
    return line + " " * max(2, col - len(line)) + comment


def _yaml_value(line):
    """取 `key: value` 里的 value，剥掉行尾注释与引号。"""
    body = _yaml_split_comment(line.rstrip("\r\n"))[0]
    raw = body.partition(":")[2].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _yaml_blank(line):
    body = line.strip()
    return not body or body.startswith("#")


def _yaml_key_col(lines, start, stop):
    """这一项里各 key 的起始列。`-` 单独占一行时键从下一行的缩进算起。"""
    m = re.match(r"^(\s*)-(\s*)", lines[start])
    col = len(m.group(1)) + 1 + len(m.group(2))
    if col >= len(lines[start].rstrip("\r\n")):
        for i in range(start + 1, stop):
            if not _yaml_blank(lines[i]):
                return len(lines[i]) - len(lines[i].lstrip())
    return col


def _yaml_item_keys(lines, start, stop, key_col):
    """列出这一项里的顶层 key，返回 [(key, 起始行, 尾后行)]。"""
    hits = []
    for i in range(start, stop):
        if _yaml_blank(lines[i]):
            continue
        text = lines[i].rstrip("\r\n")
        if i != start and len(text) - len(text.lstrip()) != key_col:
            continue          # 嵌套在某个 key 底下的行，不是这一项的 key
        m = _YAML_KEY.match(text[key_col:])
        if m:
            hits.append((m.group(1), i))
    out = []
    for n, (key, ks) in enumerate(hits):
        ke = hits[n + 1][1] if n + 1 < len(hits) else stop
        while ke > ks + 1 and _yaml_blank(lines[ke - 1]):
            ke -= 1           # 块尾的空行/注释留给下一个 key
        out.append((key, ks, ke))
    return out


def _yaml_service_item(lines, service):
    """定位 services 下 name == service 的那一项，返回 (起始行, 尾后行, key 列)。"""
    top = next((i for i, line in enumerate(lines)
                if re.match(r"^services\s*:", line)), None)
    if top is None:
        raise RuntimeError("配置里找不到顶层的 services:")
    end = len(lines)
    for i in range(top + 1, len(lines)):
        if not _yaml_blank(lines[i]) and not lines[i][:1].isspace():
            end = i
            break
    while end > top + 1 and _yaml_blank(lines[end - 1]):
        end -= 1

    starts, item_indent = [], None
    for i in range(top + 1, end):
        m = _YAML_ITEM.match(lines[i])
        if not m:
            continue
        if item_indent is None:
            item_indent = len(m.group(1))
        if len(m.group(1)) == item_indent:
            starts.append(i)
    if not starts:
        raise RuntimeError("services 下没有缩进块写法的列表项，请手工修改配置")

    for n, start in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else end
        while stop > start + 1 and _yaml_blank(lines[stop - 1]):
            stop -= 1
        key_col = _yaml_key_col(lines, start, stop)
        for key, ks, _ in _yaml_item_keys(lines, start, stop, key_col):
            if key == "name" and _yaml_value(lines[ks]) == service:
                return start, stop, key_col
    raise RuntimeError("配置里没有名为 %r 的服务" % service)


def yaml_update_service(path, service, updates):
    """就地改写 YAML 配置里某个服务的若干字段，只动这几行。"""
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)
    nl = "\r\n" if "\r\n" in text else "\n"
    start, stop, key_col = _yaml_service_item(lines, service)
    known = {k: (ks, ke) for k, ks, ke in _yaml_item_keys(lines, start, stop, key_col)}

    plan = []
    for key, value in updates.items():
        ks, ke = known.get(key, (stop, stop))   # 没配过的字段追加到这一项末尾
        plan.append((ks, ke, key, value))
    # 从后往前改，前面几处的行号才不会被前一次替换挪动
    for ks, ke, key, value in sorted(plan, key=lambda p: (p[0], p[2]), reverse=True):
        exists = ks < ke
        prefix = lines[ks][:key_col] if exists and ks == start else " " * key_col
        _, comment, col = (_yaml_split_comment(lines[ks].rstrip("\r\n"))
                           if exists else ("", "", 0))
        list_indent = key_col + 2
        for i in range(ks + 1, ke):
            m = _YAML_ITEM.match(lines[i])
            if m:
                list_indent = len(m.group(1))   # 沿用原有的列表缩进风格
                break
        if isinstance(value, (list, tuple)):
            block = [_yaml_append_comment("%s%s:" % (prefix, key), comment, col)]
            block += ["%s- %s" % (" " * list_indent, _yaml_scalar(v)) for v in value]
        else:
            block = [_yaml_append_comment(
                "%s%s: %s" % (prefix, key, _yaml_scalar(value)), comment, col)]
        lines[ks:ke] = [b + nl for b in block]

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write("".join(lines))
    os.replace(tmp, path)


def json_update_service(path, service, updates):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    hit = next((s for s in raw.get("services", []) if s["name"] == service), None)
    if hit is None:
        raise RuntimeError("配置里没有名为 %r 的服务" % service)
    hit.update(updates)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

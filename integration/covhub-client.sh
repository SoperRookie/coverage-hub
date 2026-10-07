#!/bin/sh
# covhub 远程客户端 —— 发版节点 / 被测机器上用这个，不需要装 Python、java，
# 也不需要配置文件。全部动作由 hub 那一个服务端完成，这里只发 HTTP 请求。
#
# 依赖：curl。就这一个（fetch-classes 另需 tar；upload-sources 不给包时另需 git 与 tar）。
#
# 配置（环境变量）：
#   COVHUB_URL       hub 地址，如 http://covhub.internal:8900   （必填）
#   COVHUB_TOKEN     hub 上配了 serve.token 时必填
#   COVHUB_TIMEOUT   单个请求的最长秒数，默认 600（predeploy 要 dump + merge + 出报告，大服务要几分钟）
#   COVHUB_CONNECT_TIMEOUT   连接超时秒数，默认 10
#   COVHUB_POLL_INTERVAL     wait-online 的轮询间隔秒数，默认 5
#
# 用法：
#   covhub-client.sh health
#   covhub-client.sh status [service]
#   covhub-client.sh agent-opts <service>             打印 -javaagent 参数串（push 通道可能是空格隔开的两个）
#   covhub-client.sh fetch-agent [目标路径]            下载 jacocoagent.jar
#   covhub-client.sh fetch-covhub-agent [目标路径]     下载 covhub-agent.jar（push 通道连 hub、断线重连用）
#   covhub-client.sh dump <service>
#   covhub-client.sh predeploy <service> [version] [--allow-missing]
#   covhub-client.sh report <service>
#   covhub-client.sh diagnose <service> [version]
#   covhub-client.sh retarget <service> <version> [classfiles[,更多]]
#   covhub-client.sh upload-classes <service> <version> <包路径> [--retarget]
#   covhub-client.sh fetch-classes <service> <version> <目标目录>
#   covhub-client.sh upload-sources <service> <version> [包路径] [--base <基线版本>] [--no-diff]
#                                                      不给包就在当前 git 仓库里打；hub 顺带比对基线版本的源码生成 diff
#   covhub-client.sh wait-online <service> [超时秒数，默认 120]
#   covhub-client.sh unit-coverage <service> <version> <jacoco.xml> [--group 模块名]
#   covhub-client.sh diff <service> <version> --from-sources [--base <基线版本>]   hub 比对两版已上传的源码
#   covhub-client.sh diff <service> <version> <diff文件> --base <基线commit> [--head <本次>]   上传流水线算好的 git diff
#   covhub-client.sh last-version <service> [--plain]  最近结算的版本与其 diff 的 head（自己算 git diff 时定基线用）
#   covhub-client.sh recompute <service> [version]     按已有 diff 重算新增代码覆盖
#
# 退出码：0 成功；1 hub 返回非 2xx（业务失败，响应体里的 log 有原因）或 wait-online 超时；2 连不上 hub / 参数错。
# 非零一律让部署脚本停下来，而不是静默丢数据。

set -e

# 用法就是文件头的注释：从第 2 行起，到第一个非注释行为止
usage() { awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^#+ ?/, ""); print }' "$0"; }

die() { echo "[covhub] $1" >&2; exit "${2:-1}"; }

[ -n "${COVHUB_URL:-}" ] || {
    echo "[covhub] 请设置 COVHUB_URL，例如 http://covhub.internal:8900" >&2
    usage >&2
    exit 2
}
URL=$(printf '%s' "$COVHUB_URL" | sed 's#/*$##')

# 令牌走请求头而不是 URL —— 免得被 access log 和 ps 输出记下来。
# 没配令牌时头是空的，hub 那边没开鉴权就直接放行。
AUTH="X-Covhub-Token: ${COVHUB_TOKEN:-}"
# 连接超时短、总超时长：hub 挂了要很快失败，hub 在干活（predeploy）要能等
CURL="curl -sS --connect-timeout ${COVHUB_CONNECT_TIMEOUT:-10} --max-time ${COVHUB_TIMEOUT:-600}"

# 查询串里可能出现的字符，逐个转义（服务名、版本号、路径、git ref）。
# 非 ASCII 原样发出去，curl 会按 UTF-8 送，hub 能收。
enc() {
    printf '%s' "$1" | sed 's/%/%25/g; s/ /%20/g; s/#/%23/g; s/&/%26/g; s/?/%3F/g; s/+/%2B/g; s/=/%3D/g; s/;/%3B/g'
}

need() { [ -n "${1:-}" ] || die "$2" 2; }

# request <METHOD> <路径带查询串> [上传文件]：打印响应体；HTTP 非 2xx 退出 1，连不上退出 2。
# 上传时正文是原始文件（--data-binary，-d 会吃掉换行）。只有 GET 才自动重试 —— POST 的
# dump / predeploy 会改状态，网络抖动重发一次可能把一段数据算两遍。
request() {
    _method=$1; _path=$2; _file=${3:-}
    if [ -n "$_file" ]; then
        [ -f "$_file" ] || die "找不到文件：$_file" 2
        set -- -X "$_method" --data-binary "@$_file"
    elif [ "$_method" = GET ]; then
        set -- -X GET --retry 2 --retry-delay 2
    else
        set -- -X "$_method"
    fi
    _resp=$($CURL "$@" -H "$AUTH" -w '\n%{http_code}' "$URL$_path") || die "连不上 hub：$URL" 2
    _code=$(printf '%s\n' "$_resp" | tail -n 1)
    printf '%s\n' "$_resp" | sed '$d'
    case "$_code" in
        2*) return 0 ;;
        000) die "连不上 hub：$URL" 2 ;;    # 某些 curl 带 --retry 时连接失败也返回 0，得看状态码
        401) die "hub 返回 HTTP 401：令牌不对，检查 COVHUB_TOKEN" ;;
        404) die "hub 返回 HTTP 404：服务名不存在，或 hub 地址 / 版本不对" ;;
        409) die "hub 返回 HTTP 409：业务上失败了，原因见上面的 log" ;;
        *)   die "hub 返回 HTTP $_code" ;;
    esac
}

# download <路径带查询串> <目标文件>：二进制下载；非 2xx 时把响应体（JSON 错误）打到 stderr
download() {
    _tmp="$2.part"
    _code=$($CURL --retry 2 --retry-delay 2 -H "$AUTH" -o "$_tmp" -w '%{http_code}' "$URL$1") || {
        rm -f "$_tmp"; die "连不上 hub：$URL" 2; }
    case "$_code" in
        2*)  mv "$_tmp" "$2" ;;
        000) rm -f "$_tmp"; die "连不上 hub：$URL" 2 ;;
        *)   cat "$_tmp" >&2; echo >&2; rm -f "$_tmp"; die "hub 返回 HTTP $_code" ;;
    esac
}

CMD=${1:-}
[ -n "$CMD" ] || { usage; exit 2; }
shift

case "$CMD" in

health)
    request GET "/api/health"
    ;;

status)
    if [ -n "${1:-}" ]; then
        request GET "/api/status?service=$(enc "$1")"
    else
        request GET "/api/status"
    fi
    ;;

agent-opts)
    need "${1:-}" "用法：$0 agent-opts <service>"
    # format=text 直接吐参数串：export JAVA_TOOL_OPTIONS="$(covhub-client.sh agent-opts svc)"
    request GET "/api/agent-opts?service=$(enc "$1")&format=text"
    ;;

fetch-agent)
    DEST=${1:-jacocoagent.jar}
    download "/api/agent.jar" "$DEST"
    echo "[covhub] 已下载 agent -> $DEST"
    ;;

fetch-covhub-agent)
    # push 通道的薄 agent，和 jacocoagent.jar 并列挂在被测 JVM 上（agent-opts 给出的参数串里
    # 有两个 -javaagent 时才需要它）。放到 hub 配置 covhubAgent 写的那个被测端路径。
    DEST=${1:-covhub-agent.jar}
    download "/api/covhub-agent.jar" "$DEST"
    echo "[covhub] 已下载 covhub-agent -> $DEST"
    ;;

diagnose)
    # 回答「为什么我的报告是全红的」：比对 exec 与 classfiles 的 class 指纹。
    need "${1:-}" "用法：$0 diagnose <service> [version]"
    QS="service=$(enc "$1")"
    [ -n "${2:-}" ] && QS="$QS&version=$(enc "$2")"
    request GET "/api/diagnose?$QS"
    ;;

dump|report)
    need "${1:-}" "用法：$0 $CMD <service>"
    request POST "/api/$CMD?service=$(enc "$1")"
    ;;

recompute)
    # diff 晚到、或版本串改对了之后，按已有 diff 重算这一版的新增代码覆盖（运行时 + 单测）
    need "${1:-}" "用法：$0 recompute <service> [version]"
    QS="service=$(enc "$1")"
    [ -n "${2:-}" ] && QS="$QS&version=$(enc "$2")"
    request POST "/api/recompute?$QS"
    ;;

predeploy)
    # 发版 / 重启前结算。必须在停服之前调用 —— 服务一停 agent 随进程消失，
    # 那段覆盖率永久丢失，没有任何补救手段。
    need "${1:-}" "用法：$0 predeploy <service> [version] [--allow-missing]"
    QS="service=$(enc "$1")"
    shift
    for arg in "$@"; do
        case "$arg" in
            --allow-missing) QS="$QS&allowMissing=1" ;;
            --*)             die "未知参数：$arg" 2 ;;
            *)               QS="$QS&version=$(enc "$arg")" ;;
        esac
    done
    request POST "/api/predeploy?$QS"
    ;;

retarget)
    # 发版后把 hub 的配置指向新版本的 class 产物。漏了这一步，新周期采到的 exec
    # 会和旧 class 对不上（JaCoCo 按 CRC64 class id 匹配），报告全是"未覆盖"。
    need "${2:-}" "用法：$0 retarget <service> <version> [classfiles,...]"
    QS="service=$(enc "$1")&version=$(enc "$2")"
    [ -n "${3:-}" ] && QS="$QS&classfiles=$(enc "$3")"
    request POST "/api/retarget?$QS"
    ;;

upload-classes)
    # 把该版本的 class 产物（tar.gz / zip）传给 hub —— 报告是 hub 出的，
    # class 必须在 hub 上，且必须是线上跑的那一份。
    # 加 --retarget 则上传完顺手把配置指向这份产物。
    need "${3:-}" "用法：$0 upload-classes <service> <version> <包路径> [--retarget]"
    QS="service=$(enc "$1")&version=$(enc "$2")"
    case "${4:-}" in
        "")         ;;
        --retarget) QS="$QS&retarget=1" ;;
        *)          die "未知参数：$4" 2 ;;
    esac
    request POST "/api/upload-classes?$QS" "$3"
    ;;

upload-sources)
    # 按版本把源码传给 hub —— hub 独立部署、本机没有源码时，JaCoCo 报告靠它下钻到行，
    # 看板的「新增代码」靠它看整个文件，历史版本也对得上。
    # 存好之后 hub 顺带比对基线版本的源码生成这一版的 diff（新增代码覆盖率的依据）——
    # 本机不需要 git 历史，浅克隆、清过的工作区都无所谓。基线不给由 hub 自动定
    # （服务当前 version → 最近结算的版本 → 最近上传过源码的版本）；--no-diff 只存源码。
    # 不给包路径就在当前 git 仓库里现打：受版本控制的 .java/.kt/.groovy/.scala、去掉 src/test/，
    # 路径相对仓库根。打的是工作区，流水线里 checkout 完就跑即可。
    # hub 那边也只收源码文件，配置文件之类即便混进包里也不落盘。
    need "${2:-}" "用法：$0 upload-sources <service> <version> [包路径] [--base <基线版本>] [--no-diff]"
    QS="service=$(enc "$1")&version=$(enc "$2")"
    shift 2
    PKG=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --base) need "${2:-}" "--base 后面要跟基线版本"; QS="$QS&base=$(enc "$2")"; shift 2 ;;
            --no-diff) QS="$QS&diff=skip"; shift ;;
            -*) die "未知参数：$1" 2 ;;
            *) PKG=$1; shift ;;
        esac
    done
    if [ -n "$PKG" ]; then
        request POST "/api/upload-sources?$QS" "$PKG"
    else
        command -v git > /dev/null 2>&1 || die "不给包路径时要用 git 打包，找不到 git" 2
        _top=$(git rev-parse --show-toplevel 2> /dev/null) || die "当前目录不在 git 仓库里：先 cd 进仓库，或给出源码包路径" 2
        _list="${TMPDIR:-/tmp}/covhub-sources-$$.lst"
        _pkg="${TMPDIR:-/tmp}/covhub-sources-$$.tar.gz"
        trap 'rm -f "$_list" "$_pkg"' EXIT
        git -C "$_top" -c core.quotepath=false ls-files -- '*.java' '*.kt' '*.groovy' '*.scala' \
            | grep -v -e '^src/test/' -e '/src/test/' > "$_list" || true
        [ -s "$_list" ] || die "仓库里没有受版本控制的源码文件（$_top）" 2
        tar czf "$_pkg" -C "$_top" -T "$_list" || die "打包失败" 2
        echo "[covhub] 打包 $(wc -l < "$_list" | tr -d ' ') 个源码文件（$_top）" >&2
        request POST "/api/upload-sources?$QS" "$_pkg"
    fi
    ;;

unit-coverage)
    # 构建流水线在 mvn verify 之后把 jacoco.xml（jacoco-aggregate 的也行）传给 hub：
    # 单测覆盖率入库；该版本已有 diff 时顺手算出单测的新增代码覆盖率。
    need "${3:-}" "用法：$0 unit-coverage <service> <version> <jacoco.xml> [--group 模块名]"
    QS="service=$(enc "$1")&version=$(enc "$2")"
    case "${4:-}" in
        "")      ;;
        --group) need "${5:-}" "--group 后面要跟模块名"; QS="$QS&group=$(enc "$5")" ;;
        *)       die "未知参数：$4" 2 ;;
    esac
    request POST "/api/unit-coverage?$QS" "$3"
    ;;

diff)
    # 这一版的 diff，之后每次采集都能算出「本版本新增代码」的覆盖率。两种来源：
    #   --from-sources   hub 比对该版本与基线版本经 upload-sources 传上来的源码（推荐；
    #                    upload-sources 默认已经做了，这里用于指定基线或重做）
    #   <diff文件>       流水线自己算的 git diff，推荐：
    #                    git -c core.quotepath=false diff --no-color --no-ext-diff -M --unified=0 \
    #                        --diff-filter=AMR "$BASE".."$HEAD" -- '*.java' '*.kt' > v.diff
    #                    空 diff（这一版没改 Java 代码）也照传：hub 会记成「无新增」。
    need "${2:-}" "用法：$0 diff <service> <version> --from-sources [--base <基线版本>] | <diff文件> --base <基线commit> [--head <本次>]"
    QS="service=$(enc "$1")&version=$(enc "$2")"
    shift 2
    FILE=""; BASE=""; FROM=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --from-sources) FROM=1; shift ;;
            --base) need "${2:-}" "--base 后面要跟基线"; BASE=$2; shift 2 ;;
            --head) need "${2:-}" "--head 后面要跟本次的 commit"; QS="$QS&head=$(enc "$2")"; shift 2 ;;
            -*) die "未知参数：$1" 2 ;;
            *) FILE=$1; shift ;;
        esac
    done
    if [ -n "$FROM" ]; then
        [ -z "$FILE" ] || die "--from-sources 不接 diff 文件：hub 自己比对已上传的源码" 2
        [ -n "$BASE" ] && QS="$QS&base=$(enc "$BASE")"
        request POST "/api/diff?$QS&from=sources"
    else
        need "$FILE" "要么给 diff 文件（配 --base <commit>），要么 --from-sources 让 hub 比对源码"
        need "$BASE" "上传 git diff 需要 --base <基线的 commit / tag>"
        request POST "/api/diff?$QS&base=$(enc "$BASE")" "$FILE"
    fi
    ;;

last-version)
    # 流水线自己算 git diff 时定基线：问 hub「上一次结算的是哪个版本、它的 diff 到哪个 commit」。
    # 让 hub 比对源码生成 diff（默认）的话用不着这一步。
    # --plain 只打一行 "<version>\t<head>"（没有归档时打空行，退出码仍为 0），
    # 方便 shell 里 read：  read -r LAST_VER LAST_HEAD <<EOF ... 或 cut -f2
    need "${1:-}" "用法：$0 last-version <service> [--plain]"
    if [ "${2:-}" = --plain ]; then
        _json=$(request GET "/api/services/$(enc "$1")/versions")
        printf '%s
' "$_json" | awk '
            /"latest": null/ { print ""; done = 1; exit }
            /"latest": \{/ { inlatest = 1; next }
            inlatest && /"version":/ { v = $0; sub(/.*"version": "/, "", v); sub(/".*/, "", v) }
            inlatest && /"head":/ { h = $0; sub(/.*"head": /, "", h); sub(/,?$/, "", h); gsub(/"/, "", h); if (h == "null") h = "" }
            inlatest && /^  \}/ { print v "\t" h; done = 1; exit }
            END { if (!done) print "" }'
    else
        request GET "/api/services/$(enc "$1")/versions"
    fi
    ;;

fetch-classes)
    # 取回某个版本的 class 产物解到目标目录（要在别处重出报告或比对时用）——
    # 本机不必囤历史产物。
    need "${3:-}" "用法：$0 fetch-classes <service> <version> <目标目录>"
    DEST=$3
    # 目标目录会被清空重建：拦住显然不该清的路径
    case "$DEST" in
        /|.|..|"$HOME"|"${HOME%/}/"|"") die "拒绝清空目录：$DEST" 2 ;;
    esac
    _tmp="${TMPDIR:-/tmp}/covhub-classes-$$.tar.gz"
    download "/api/classes?service=$(enc "$1")&version=$(enc "$2")" "$_tmp"
    rm -rf "$DEST" && mkdir -p "$DEST"
    tar xzf "$_tmp" -C "$DEST"
    rm -f "$_tmp"
    echo "[covhub] $1 $2 的 class 产物已解到 $DEST"
    ;;

wait-online)
    # 部署完轮询确认新实例的 agent 已就绪（pull：端口能连上；push：有实例连着）
    need "${1:-}" "用法：$0 wait-online <service> [超时秒数]"
    SVC=$(enc "$1")
    TIMEOUT=${2:-120}
    INTERVAL=${COVHUB_POLL_INTERVAL:-5}
    request GET "/api/status?service=$SVC" > /dev/null      # 服务名写错就地报错
    ELAPSED=0
    while [ "$ELAPSED" -lt "$TIMEOUT" ]; do
        # 轮询期间 hub 短暂不可达不算失败，继续等
        if $CURL -H "$AUTH" "$URL/api/status?service=$SVC" 2>/dev/null | grep -q '"online": true'; then
            echo "[covhub] $1 的 agent 已就绪（等待 ${ELAPSED}s）"
            exit 0
        fi
        sleep "$INTERVAL"
        ELAPSED=$((ELAPSED + INTERVAL))
    done
    die "等待 ${TIMEOUT}s 后 $1 仍未就绪"
    ;;

help|-h|--help)
    usage
    ;;

*)
    echo "[covhub] 未知命令：$CMD" >&2
    usage >&2
    exit 2
    ;;
esac

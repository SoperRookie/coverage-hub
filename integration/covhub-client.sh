#!/bin/sh
# covhub 远程客户端 —— 发版节点 / 被测机器上用这个，不需要装 Python、java，
# 也不需要配置文件。全部动作由 hub 那一个服务端完成，这里只发 HTTP 请求。
#
# 依赖：curl。就这一个。
#
# 配置（环境变量）：
#   COVHUB_URL     hub 地址，如 http://covhub.internal:8900   （必填）
#   COVHUB_TOKEN   hub 上配了 serve.token 时必填
#
# 用法：
#   covhub-client.sh health
#   covhub-client.sh status [service]
#   covhub-client.sh agent-opts <service>             打印 -javaagent 参数串
#   covhub-client.sh fetch-agent [目标路径]            下载 jacocoagent.jar
#   covhub-client.sh dump <service>
#   covhub-client.sh predeploy <service> [version] [--allow-missing]
#   covhub-client.sh report <service>
#   covhub-client.sh diagnose <service> [version]
#   covhub-client.sh retarget <service> <version> [classfiles[,更多]]
#   covhub-client.sh upload-classes <service> <version> <包路径> [--retarget]
#   covhub-client.sh fetch-classes <service> <version> <目标目录>
#   covhub-client.sh wait-online <service> [超时秒数，默认 120]
#   covhub-client.sh unit-coverage <service> <version> <jacoco.xml> [--group 模块名]
#   covhub-client.sh diff <service> <version> <diff文件> --base <基线> [--head <本次>]
#
# hub 返回非 2xx 时一律非零码退出 —— 让部署脚本停下来，而不是静默丢数据。

set -e

usage() { sed -n '2,28p' "$0" | sed 's/^#\{1,\} \{0,1\}//'; }

[ -n "${COVHUB_URL:-}" ] || {
    echo "[covhub] 请设置 COVHUB_URL，例如 http://covhub.internal:8900" >&2
    usage >&2
    exit 2
}
URL=$(printf '%s' "$COVHUB_URL" | sed 's#/*$##')

# 令牌走请求头而不是 URL —— 免得被 access log 和 ps 输出记下来。
# 没配令牌时头是空的，hub 那边没开鉴权就直接放行。
AUTH="X-Covhub-Token: ${COVHUB_TOKEN:-}"

# call <METHOD> <路径带查询串>：打印响应体，HTTP 非 2xx 时退出
call() {
    _resp=$(curl -sS -X "$1" -H "$AUTH" -w '\n%{http_code}' "$URL$2") || {
        echo "[covhub] 连不上 hub：$URL" >&2
        exit 2
    }
    _code=$(printf '%s\n' "$_resp" | tail -n 1)
    printf '%s\n' "$_resp" | sed '$d'
    case "$_code" in
        2*) return 0 ;;
        *)  echo "[covhub] hub 返回 HTTP $_code" >&2; exit 1 ;;
    esac
}

# 查询串里可能出现的字符，简单转义即可（服务名、版本号、路径）
enc() { printf '%s' "$1" | sed 's/%/%25/g; s/ /%20/g; s/#/%23/g; s/&/%26/g; s/?/%3F/g; s/+/%2B/g'; }

need() { [ -n "${1:-}" ] || { echo "$2" >&2; exit 1; }; }

# upload <路径带查询串> <文件>：正文是原始文件（--data-binary，-d 会吃掉换行），非 2xx 退出
upload() {
    [ -f "$2" ] || { echo "[covhub] 找不到文件：$2" >&2; exit 1; }
    _out=$(curl -sS -X POST -H "$AUTH" --data-binary "@$2" \
                -w '\n%{http_code}' "$URL$1") || {
        echo "[covhub] 连不上 hub：$URL" >&2; exit 2; }
    _code=$(printf '%s\n' "$_out" | tail -n 1)
    printf '%s\n' "$_out" | sed '$d'
    case "$_code" in
        2*) ;;
        *)  echo "[covhub] hub 返回 HTTP $_code" >&2; exit 1 ;;
    esac
}

CMD=${1:-}
[ -n "$CMD" ] || { usage; exit 1; }
shift

case "$CMD" in

health)
    call GET "/api/health"
    ;;

status)
    if [ -n "${1:-}" ]; then
        call GET "/api/status?service=$(enc "$1")"
    else
        call GET "/api/status"
    fi
    ;;

agent-opts)
    need "${1:-}" "用法：$0 agent-opts <service>"
    # format=text 直接吐参数串：export JAVA_TOOL_OPTIONS="$(covhub-client.sh agent-opts svc)"
    call GET "/api/agent-opts?service=$(enc "$1")&format=text"
    ;;

fetch-agent)
    DEST=${1:-jacocoagent.jar}
    curl -sSf -H "$AUTH" -o "$DEST" "$URL/api/agent.jar"
    echo "[covhub] 已下载 agent -> $DEST"
    ;;

diagnose)
    # 回答「为什么我的报告是全红的」：比对 exec 与 classfiles 的 class 指纹。
    need "${1:-}" "用法：$0 diagnose <service> [version]"
    QS="service=$(enc "$1")"
    if [ -n "${2:-}" ]; then
        QS="$QS&version=$(enc "$2")"
    fi
    call GET "/api/diagnose?$QS"
    ;;

dump|report)
    need "${1:-}" "用法：$0 $CMD <service>"
    call POST "/api/$CMD?service=$(enc "$1")"
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
            *)               QS="$QS&version=$(enc "$arg")" ;;
        esac
    done
    call POST "/api/predeploy?$QS"
    ;;

retarget)
    # 发版后把 hub 的配置指向新版本的 class 产物。漏了这一步，新周期采到的 exec
    # 会和旧 class 对不上（JaCoCo 按 CRC64 class id 匹配），报告全是"未覆盖"。
    need "${2:-}" "用法：$0 retarget <service> <version> [classfiles,...]"
    QS="service=$(enc "$1")&version=$(enc "$2")"
    if [ -n "${3:-}" ]; then
        QS="$QS&classfiles=$(enc "$3")"
    fi
    call POST "/api/retarget?$QS"
    ;;

upload-classes)
    # 把该版本的 class 产物（tar.gz / zip）传给 hub —— 报告是 hub 出的，
    # class 必须在 hub 上，且必须是线上跑的那一份。
    # 加 --retarget 则上传完顺手把配置指向这份产物。
    need "${3:-}" "用法：$0 upload-classes <service> <version> <包路径> [--retarget]"
    QS="service=$(enc "$1")&version=$(enc "$2")"
    case "${4:-}" in --retarget) QS="$QS&retarget=1" ;; esac
    upload "/api/upload-classes?$QS" "$3"
    ;;

unit-coverage)
    # 构建流水线在 mvn verify 之后把 jacoco.xml（jacoco-aggregate 的也行）传给 hub：
    # 单测覆盖率入库；该版本已有 diff 时顺手算出单测的新增代码覆盖率。
    need "${3:-}" "用法：$0 unit-coverage <service> <version> <jacoco.xml> [--group 模块名]"
    QS="service=$(enc "$1")&version=$(enc "$2")"
    case "${4:-}" in --group) need "${5:-}" "--group 后面要跟模块名"; QS="$QS&group=$(enc "$5")" ;; esac
    upload "/api/unit-coverage?$QS" "$3"
    ;;

diff)
    # 把 git diff 传给 hub，之后每次采集都能算出「本版本新增代码」的覆盖率。推荐：
    #   git -c core.quotepath=false diff --no-color --no-ext-diff -M --unified=0 \
    #       --diff-filter=AMR "$BASE".."$HEAD" -- '*.java' '*.kt' > v.diff
    need "${3:-}" "用法：$0 diff <service> <version> <diff文件> --base <基线> [--head <本次>]"
    QS="service=$(enc "$1")&version=$(enc "$2")"
    FILE=$3; shift 3
    BASE=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --base) BASE=$2; shift 2 ;;
            --head) QS="$QS&head=$(enc "$2")"; shift 2 ;;
            *) echo "[covhub] 未知参数：$1" >&2; exit 1 ;;
        esac
    done
    need "$BASE" "diff 需要 --base <基线的 commit / tag>"
    upload "/api/diff?$QS&base=$(enc "$BASE")" "$FILE"
    ;;

fetch-classes)
    # 取回某个版本的 class 产物解到目标目录。推 Sonar 时
    # -Dsonar.java.binaries 要的就是它 —— 本机不必囤历史产物。
    need "${3:-}" "用法：$0 fetch-classes <service> <version> <目标目录>"
    _tmp="${TMPDIR:-/tmp}/covhub-classes-$1-$2.tar.gz"
    _code=$(curl -sS -H "$AUTH" -o "$_tmp" -w '%{http_code}' \
                 "$URL/api/classes?service=$(enc "$1")&version=$(enc "$2")") || {
        echo "[covhub] 连不上 hub：$URL" >&2; exit 2; }
    case "$_code" in
        2*) ;;
        *)  cat "$_tmp" >&2; echo >&2
            echo "[covhub] hub 返回 HTTP $_code" >&2; rm -f "$_tmp"; exit 1 ;;
    esac
    rm -rf "$3" && mkdir -p "$3"
    tar xzf "$_tmp" -C "$3"
    rm -f "$_tmp"
    echo "[covhub] $1 $2 的 class 产物已解到 $3"
    ;;

wait-online)
    # 部署完轮询确认新实例的 agent 已就绪
    need "${1:-}" "用法：$0 wait-online <service> [超时秒数]"
    SVC=$(enc "$1")
    TIMEOUT=${2:-120}
    call GET "/api/status?service=$SVC" > /dev/null      # 服务名写错就地报错
    ELAPSED=0
    while [ "$ELAPSED" -lt "$TIMEOUT" ]; do
        if curl -sS -H "$AUTH" "$URL/api/status?service=$SVC" | grep -q '"online": true'; then
            echo "[covhub] $1 的 agent 已就绪（等待 ${ELAPSED}s）"
            exit 0
        fi
        sleep 5
        ELAPSED=$((ELAPSED + 5))
    done
    echo "[covhub] 等待 ${TIMEOUT}s 后 $1 仍未就绪" >&2
    exit 1
    ;;

*)
    echo "未知命令：$CMD" >&2
    usage >&2
    exit 1
    ;;
esac

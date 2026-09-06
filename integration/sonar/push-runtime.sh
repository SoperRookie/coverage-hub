#!/usr/bin/env bash
# 把某个版本的运行期覆盖率推到 SonarQube。
#
#   ./push-runtime.sh <service> <version> [sources-dir]
#
# 依赖：sonar-scanner 在 PATH 上；SONAR_HOST_URL 与 SONAR_TOKEN 已设置。
#
# 报告和 class 产物都在 covhub 那台机器上。设了 COVHUB_URL 就自动取回来
# （本机不需要装 covhub，也不必囤历史 class 产物）；本脚本恰好就跑在 hub 上时，
# 设 COVHUB_HOME / ARTIFACTS 走本地文件即可。
set -euo pipefail

SERVICE="${1:?用法: push-runtime.sh <service> <version> [sources-dir]}"
VERSION="${2:?缺少 version}"
SOURCES="${3:-/opt/src/${SERVICE}/src/main/java}"

COVHUB_HOME="${COVHUB_HOME:-/opt/coverage-hub}"
ARTIFACTS="${ARTIFACTS:-/opt/artifacts}"

BINARIES="${ARTIFACTS}/${SERVICE}/${VERSION}"
XML="${COVHUB_HOME}/data/${SERVICE}/versions/${VERSION}/jacoco.xml"

if [ -n "${COVHUB_URL:-}" ]; then
    # 远程模式：报告和 class 都从 hub 取，本机什么都不用留
    HUB="${COVHUB_URL%/}"
    WORK="${TMPDIR:-/tmp}/covhub-${SERVICE}-${VERSION}"
    mkdir -p "$WORK"

    XML="${WORK}/jacoco.xml"
    if ! curl -sSf -o "$XML" "${HUB}/${SERVICE}/versions/${VERSION}/jacoco.xml"; then
        echo "从 hub 取报告失败：${HUB}/${SERVICE}/versions/${VERSION}/jacoco.xml" >&2
        echo "  该版本可能没有跑过 covhub predeploy 结算" >&2
        exit 1
    fi

    # class 必须是采集时运行的那一份，对不上 Sonar 上就是 0%
    BINARIES="${WORK}/classes"
    rm -rf "$BINARIES" && mkdir -p "$BINARIES"
    if ! curl -sS -f -H "X-Covhub-Token: ${COVHUB_TOKEN:-}" \
              -o "${WORK}/classes.tar.gz" \
              "${HUB}/api/classes?service=${SERVICE}&version=${VERSION}"; then
        echo "从 hub 取 class 产物失败：${HUB}/api/classes?service=${SERVICE}&version=${VERSION}" >&2
        echo "  该版本发版时可能没跑过 upload-classes" >&2
        exit 1
    fi
    tar xzf "${WORK}/classes.tar.gz" -C "$BINARIES"
fi

# --- 前置校验 -------------------------------------------------------------
# 这三条任一不满足，推上去的都是错的数据（0% 或行号错位），
# 与其事后在 Sonar 上排查，不如在这里直接失败。
fail=0
if [ ! -f "$XML" ]; then
    echo "找不到覆盖率报告：$XML" >&2
    echo "  该版本可能没有跑过 covhub predeploy 结算" >&2
    fail=1
fi
if [ ! -d "$BINARIES" ]; then
    echo "找不到 class 产物：$BINARIES" >&2
    echo "  构建流水线是否归档了 class？没有它 Sonar 会显示 0%" >&2
    echo "  设 COVHUB_URL 可以直接从 hub 取回该版本的产物" >&2
    fail=1
fi
if [ ! -d "$SOURCES" ]; then
    echo "找不到源码目录：$SOURCES" >&2
    echo "  源码需 checkout 到 $VERSION 对应的版本，否则覆盖标记会打到错行" >&2
    fail=1
fi
[ "$fail" -eq 0 ] || exit 1

echo "推送 ${SERVICE} ${VERSION} 的运行期覆盖率"
echo "  报告   : $XML"
echo "  class  : $BINARIES"
echo "  源码   : $SOURCES"

sonar-scanner \
  -Dsonar.projectKey="${SERVICE}-runtime" \
  -Dsonar.projectName="${SERVICE} (runtime coverage)" \
  -Dsonar.projectVersion="${VERSION}" \
  -Dsonar.sources="${SOURCES}" \
  -Dsonar.java.binaries="${BINARIES}" \
  -Dsonar.coverage.jacoco.xmlReportPaths="${XML}" \
  -Dsonar.sourceEncoding=UTF-8

echo "完成。"

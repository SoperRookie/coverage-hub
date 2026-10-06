#!/bin/sh
# 构建 lib/covhub-agent.jar（被测 JVM 上挂的薄 agent，源码就一个类）。
#
# 产物**进版本库**，和另外两个 jar 一样克隆下来就能用 —— 只有改了 agent/src 才需要
# 重跑这个脚本，hub 机器和被测机器都不需要 JDK。
#
# 依赖：JDK 9+（要 javac --release）。目标字节码是 Java 8，与 jacocoagent.jar 的下限一致。
#
# Windows 上没有 sh 的话用同目录的 build.cmd —— 两个脚本做的事完全一样，改一个记得改另一个。

set -e

here=$(cd "$(dirname "$0")" && pwd)
build="$here/build"
dest="$here/../lib/covhub-agent.jar"

rm -rf "$build"
mkdir -p "$build/classes"

# -Xlint:-options：新 JDK 会提示「source 8 已过时」，那正是我们要的下限。
# 显式给 -cp：源码零依赖，别让构建机上的 CLASSPATH 环境变量掺进来（它里头一条失效路径
# 就是一个告警，配上 -Werror 直接编不过）
javac --release 8 -encoding UTF-8 -Xlint:all -Xlint:-options -Werror \
    -cp "$build/classes" -d "$build/classes" "$here"/src/covhub/agent/*.java

version=$(sed -n 's/.*String VERSION = "\(.*\)";.*/\1/p' "$here/src/covhub/agent/CovhubAgent.java")
[ -n "$version" ] || { echo "读不出 CovhubAgent.VERSION" >&2; exit 1; }

cat > "$build/MANIFEST.MF" <<EOF
Premain-Class: covhub.agent.CovhubAgent
Main-Class: covhub.agent.CovhubAgent
Implementation-Title: covhub agent
Implementation-Version: $version
EOF

jar cfm "$dest" "$build/MANIFEST.MF" -C "$build/classes" .
rm -rf "$build"
echo "已生成 $dest（版本 $version）"

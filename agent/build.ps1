# 构建 lib\covhub-agent.jar —— agent/build.sh 的 Windows 版，两个脚本做的事完全一样，改一个记得改另一个。
# 每一步为什么这么做见 build.sh 里的注释，这里只记 PowerShell 特有的坑。
#
# 用法：    powershell -ExecutionPolicy Bypass -File agent\build.ps1   （任意目录，cmd / PowerShell 都行）
#           已放开执行策略的话直接 .\agent\build.ps1 也可以。
# 依赖：    PATH 上有 JDK 9+（要 javac --release）。目标字节码是 Java 8。
#
# 文件带 UTF-8 BOM —— Windows PowerShell 5.1 读无 BOM 的脚本按系统代码页解码，中文注释会变乱码；
# 有 BOM 两个版本的 PowerShell 都按 UTF-8 读。pwsh 7 无所谓，5.1 必须有。

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here = $PSScriptRoot
$build = Join-Path $here 'build'
$classes = Join-Path $build 'classes'
$dest = Join-Path $here '..\lib\covhub-agent.jar'
$main = Join-Path $here 'src\covhub\agent\CovhubAgent.java'

if (-not (Get-Command javac -ErrorAction SilentlyContinue)) {
    Write-Error '[build] 找不到 javac：装 JDK 9+ 并把 bin 目录加进 PATH'
}

if (Test-Path $build) { Remove-Item -Recurse -Force $build }
New-Item -ItemType Directory -Force $classes | Out-Null

# 源文件列表自己枚举后逐个传绝对路径：PowerShell 不替原生命令展开通配符，javac 在 Windows 上
# 虽然自己会展开，但带空格的路径一加引号就展不开了（之前的批处理版本就是这么栽的）。
$sources = Get-ChildItem (Join-Path $here 'src\covhub\agent') -Filter '*.java' | ForEach-Object { $_.FullName }
if (-not $sources) { Write-Error '[build] agent/src/covhub/agent 下没有 .java 文件' }

# -Xlint:-options：新 JDK 会提示「source 8 已过时」，那正是我们要的下限。
# -cp 显式给：源码零依赖，别让构建机上残留的 CLASSPATH 环境变量掺进来（它里头一条失效路径
#            就是一个告警，配上 -Werror 直接编不过）。
& javac --release 8 -encoding UTF-8 -Xlint:all '-Xlint:-options' -Werror -cp $classes -d $classes $sources
if ($LASTEXITCODE -ne 0) { Write-Error "[build] javac 失败（退出码 $LASTEXITCODE）" }

# 版本号取源码里的 VERSION 常量：那一行双引号中间的内容。
$match = Select-String -Path $main -Pattern 'String VERSION = "([^"]*)"' | Select-Object -First 1
if (-not $match) { Write-Error '[build] 读不出 CovhubAgent.VERSION' }
$version = $match.Matches[0].Groups[1].Value

# manifest 必须以换行结尾，否则 jar 会丢掉最后一行；用 ASCII 写，别让 PowerShell 带上 BOM。
$manifest = Join-Path $build 'MANIFEST.MF'
@(
    'Premain-Class: covhub.agent.CovhubAgent'
    'Main-Class: covhub.agent.CovhubAgent'
    'Implementation-Title: covhub agent'
    "Implementation-Version: $version"
) | Set-Content -Path $manifest -Encoding Ascii

& jar cfm $dest $manifest -C $classes .
if ($LASTEXITCODE -ne 0) { Write-Error "[build] jar 失败（退出码 $LASTEXITCODE）" }

Remove-Item -Recurse -Force $build
Write-Host "[build] 已生成 $((Resolve-Path $dest).Path)（版本 $version）"

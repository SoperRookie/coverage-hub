#!groovy
/**
 * covhub —— Jenkins Shared Library
 *
 * 安装：把 integration/jenkins 目录作为一个 Shared Library 仓库配置到
 * Manage Jenkins → System → Global Pipeline Libraries，名字建议就叫 covhub。
 * 之后在 Jenkinsfile 顶部 @Library('covhub') _ 即可使用。
 *
 * 两种工作模式，优先用第一种：
 *
 *   ① 远程模式（推荐）—— 节点上只要有 curl。
 *      hub    covhub 服务端地址，如 http://covhub.internal:8900
 *             也可以不传，改设环境变量 COVHUB_URL
 *      tokenCredentialsId   hub 配了 serve.token 时，存令牌的 Secret text 凭据 ID
 *             也可以不传，改设环境变量 COVHUB_TOKEN
 *
 *      整套方案只需要一个服务端。发版节点不装 Python、不装 java、不放
 *      targets.json —— dump / 结算 / 出报告全在 hub 上完成。
 *
 *   ② 本地模式（兜底）—— 节点上装了 covhub 本体时才可用，即 Jenkins agent 就
 *      跑在 hub 那台机器上。此时节点需要 Python 3、java、covhub.py、targets.json。
 *      home    covhub 安装目录，默认 /opt/coverage-hub
 *      config  targets.json 路径，默认 <home>/targets.json
 *      python  python 可执行文件，默认 python3
 *
 * 没传 hub 也没设 COVHUB_URL 时自动落到本地模式。
 */

// --------------------------------------------------------------------------
// 内部
// --------------------------------------------------------------------------

/** hub 地址；返回空串表示走本地模式 */
private String hubUrl(Map args) {
    String url = args.hub ?: env.COVHUB_URL ?: ''
    return url.replaceAll('/+$', '')
}

/** 本地模式的命令前缀 */
private String cli(Map args) {
    String home = args.home ?: '/opt/coverage-hub'
    String config = args.config ?: "${home}/targets.json"
    String python = args.python ?: 'python3'
    return "${python} ${home}/covhub.py -c ${config}"
}

/**
 * 发一次请求，返回响应体。HTTP 非 2xx 直接让这一步失败 ——
 * 覆盖率结算失败必须停住流水线，而不是带着丢失的数据继续往下走。
 */
private String http(Map args, String method, String path) {
    String url = hubUrl(args) + path
    String script = """
        set -e
        code=\$(curl -sS -X ${method} -H "X-Covhub-Token: \${COVHUB_TOKEN:-}" \\
                 -o .covhub-resp -w '%{http_code}' '${url}')
        cat .covhub-resp
        rm -f .covhub-resp
        case "\$code" in
            2*) ;;
            *) echo "[covhub] hub 返回 HTTP \$code" >&2; exit 1 ;;
        esac
    """
    if (args.tokenCredentialsId) {
        String out = null
        withCredentials([string(credentialsId: args.tokenCredentialsId, variable: 'COVHUB_TOKEN')]) {
            out = sh(script: script, returnStdout: true)
        }
        return out
    }
    return sh(script: script, returnStdout: true)
}

/** 查询串转义：服务名、版本号、路径里可能出现的字符 */
private String enc(Object value) {
    return java.net.URLEncoder.encode(value as String, 'UTF-8')
}

// --------------------------------------------------------------------------
// 步骤
// --------------------------------------------------------------------------

/**
 * 取该服务应注入的 -javaagent 参数串。
 * 用法：env.JAVA_TOOL_OPTIONS = covhub.agentOpts(service: 'my-service')
 */
String agentOpts(Map args) {
    assert args.service : 'agentOpts 需要 service'
    if (hubUrl(args)) {
        return http(args, 'GET', "/api/agent-opts?service=${enc(args.service)}&format=text").trim()
    }
    return sh(script: "${cli(args)} agent-opts ${args.service}", returnStdout: true).trim()
}

/**
 * 发版 / 重启前结算覆盖率。
 *
 * 必须在停服之前调用 —— 服务一停 agent 就没了，那段数据永久丢失。
 * 默认目标不可达时让流水线失败；确实允许跳过时传 allowMissing: true。
 */
void predeploy(Map args) {
    assert args.service : 'predeploy 需要 service'
    echo "[covhub] 结算 ${args.service} 版本 ${args.version ?: '(取配置)'} 的运行期覆盖率"
    if (hubUrl(args)) {
        String qs = "service=${enc(args.service)}"
        if (args.version) { qs += "&version=${enc(args.version)}" }
        if (args.allowMissing) { qs += '&allowMissing=1' }
        echo http(args, 'POST', "/api/predeploy?${qs}")
        return
    }
    String extra = ''
    if (args.version) { extra += " --version ${args.version}" }
    if (args.allowMissing) { extra += ' --allow-missing' }
    sh "${cli(args)} predeploy ${args.service}${extra}"
}

/** 拉一次快照（累加，不清零），用于流水线中途取样 */
void dump(Map args) {
    assert args.service : 'dump 需要 service'
    if (hubUrl(args)) {
        echo http(args, 'POST', "/api/dump?service=${enc(args.service)}")
        return
    }
    sh "${cli(args)} dump ${args.service}"
}

/** 打印目标连通性与最新覆盖率；部署后用它确认新实例的 agent 已就绪 */
void status(Map args = [:]) {
    if (hubUrl(args)) {
        String qs = args.service ? "?service=${enc(args.service)}" : ''
        echo http(args, 'GET', "/api/status${qs}")
        return
    }
    sh "${cli(args)} status${args.service ? ' ' + args.service : ''}"
}

/**
 * 判断目标 agent 是否可连通。部署后轮询用。
 */
boolean online(Map args) {
    assert args.service : 'online 需要 service'
    if (hubUrl(args)) {
        String out = http(args, 'GET', "/api/status?service=${enc(args.service)}")
        echo out
        return out.contains('"online": true')
    }
    String out = sh(script: "${cli(args)} status ${args.service}", returnStdout: true)
    echo out
    return out.readLines().any { line ->
        line.trim().startsWith(args.service as String) && line =~ /\bok\b/
    }
}

/**
 * 诊断 exec 与 class 产物是否对得上，返回结构化结果。
 *
 * 回答的是「报告为什么全红」——JaCoCo 按类的 CRC64 指纹匹配数据，class 对不上时
 * 报告只会显示「全部未覆盖」，**不会报错**。这一步把它变成可判定的数字。
 *
 * 返回的 Map 主要字段：matchRate（指纹匹配率，无执行数据时为 null）、
 * execClasses / classFileClasses / matched、verdict（判定文案）、breaks（断代记录）。
 */
Map diagnose(Map args) {
    assert args.service : 'diagnose 需要 service'
    String raw
    if (hubUrl(args)) {
        String qs = "service=${enc(args.service)}"
        if (args.version) { qs += "&version=${enc(args.version)}" }
        raw = http(args, 'GET', "/api/diagnose?${qs}")
    } else {
        String extra = args.version ? " --version '${args.version}'" : ''
        raw = sh(script: "${cli(args)} diagnose ${args.service}${extra} --json", returnStdout: true)
    }
    // readJSON 来自 Pipeline Utility Steps（节点前置条件里已经要求了），
    // 比 JsonSlurper 更适合流水线沙箱
    def parsed = readJSON(text: raw)
    Map d = (parsed.diagnose ?: parsed) as Map

    echo("[covhub] ${args.service}${args.version ? ' ' + args.version : ''}  " +
         "exec ${d.execFiles} 个快照 / ${d.execClasses} 个类，" +
         "classfiles ${d.classFileClasses} 个类，" +
         "指纹匹配 ${d.matchRate == null ? '不适用' : d.matchRate + '%'}\n" +
         "[covhub] 判定：${d.verdict}")
    return d
}

/**
 * 断言这个服务采到的数据是有效的，不达标就让流水线失败。
 *
 * 放在部署后的验证阶段：class 产物没跟着版本换（最容易漏的一步）会在这里当场
 * 暴露，而不是等一个月后才发现归档的全是废数据。
 *
 * min 默认 90（%）。刚起的服务还没有执行数据时 matchRate 是 null ——
 * 那不是「对不上」，只是「还没得可比」，此时告警而不失败。
 */
void requireMatch(Map args) {
    int min = (args.min ?: 90) as int
    Map d = diagnose(args)
    if (d.matchRate == null) {
        echo "[covhub] 警告：还没有执行数据，无法判断 class 是否对得上。${d.verdict}"
        return
    }
    if ((d.matchRate as BigDecimal) < min) {
        error("[covhub] ${args.service} 的 class 产物对不上：指纹匹配 ${d.matchRate}%，低于阈值 ${min}%。\n" +
              "${d.verdict}\n" +
              "多半是第 4 步没把新版本的 class 传给 hub，或 classfiles 还指着上一版。")
    }
    echo "[covhub] 指纹匹配 ${d.matchRate}%，class 产物对得上"
}

/**
 * 发版后更新 hub 的配置：版本号 + class 产物路径。
 *
 * 这一步最容易被漏掉。JaCoCo 按 CRC64 class id 匹配数据，class 产物没跟着换，
 * 新版本采到的 exec 会和旧 class 对不上，报告全是"未覆盖"。
 *
 * 注意 classfiles 是 **hub 那台机器上** 的路径 —— 报告是 hub 出的。
 */
void retarget(Map args) {
    assert args.service : 'retarget 需要 service'
    assert args.version : 'retarget 需要 version'
    List paths = (args.classfiles instanceof List) ? args.classfiles
                 : (args.classfiles ? [args.classfiles] : [])

    if (hubUrl(args)) {
        String qs = "service=${enc(args.service)}&version=${enc(args.version)}"
        if (paths) { qs += "&classfiles=${enc(paths.join(','))}" }
        echo http(args, 'POST', "/api/retarget?${qs}")
        return
    }
    String extra = paths ? ' --classfiles ' + paths.collect { "'${it}'" }.join(' ') : ''
    sh "${cli(args)} retarget ${args.service} --version '${args.version}'${extra}"
}

/**
 * 把该版本的 class 产物压缩包上传给 hub。
 *
 * 报告是 hub 出的，class 就必须在 hub 上 —— 有了这一步，发版节点和 hub 之间
 * 不需要共享目录、不需要 NFS，一条 HTTP 就够。
 * 传 retarget: true 则上传完顺手把配置指向这份产物（省掉单独一次 retarget）。
 */
String uploadClasses(Map args) {
    assert args.service : 'uploadClasses 需要 service'
    assert args.version : 'uploadClasses 需要 version'
    assert args.archive : 'uploadClasses 需要 archive（tar.gz / zip 路径）'
    assert hubUrl(args) : 'uploadClasses 需要 hub（或环境变量 COVHUB_URL）'

    String qs = "service=${enc(args.service)}&version=${enc(args.version)}"
    if (args.retarget) { qs += '&retarget=1' }
    String script = """
        set -e
        code=\$(curl -sS -X POST -H "X-Covhub-Token: \${COVHUB_TOKEN:-}" \\
                 --data-binary '@${args.archive}' \\
                 -o .covhub-resp -w '%{http_code}' '${hubUrl(args)}/api/upload-classes?${qs}')
        cat .covhub-resp
        rm -f .covhub-resp
        case "\$code" in
            2*) ;;
            *) echo "[covhub] 上传 class 产物失败，HTTP \$code" >&2; exit 1 ;;
        esac
    """
    String out
    if (args.tokenCredentialsId) {
        withCredentials([string(credentialsId: args.tokenCredentialsId, variable: 'COVHUB_TOKEN')]) {
            out = sh(script: script, returnStdout: true)
        }
    } else {
        out = sh(script: script, returnStdout: true)
    }
    echo out
    return out
}

/**
 * 从 hub 下载 jacocoagent.jar 到本节点。
 *
 * 有了它，被测服务所在的机器不必预先铺一份 agent，也不用跟 hub 共享目录 ——
 * 部署前 curl 一次即可。
 */
String fetchAgent(Map args) {
    String dest = args.dest ?: "${pwd()}/jacocoagent.jar"
    assert hubUrl(args) : 'fetchAgent 需要 hub（或环境变量 COVHUB_URL）'
    String script = """
        set -e
        curl -sSf -H "X-Covhub-Token: \${COVHUB_TOKEN:-}" -o '${dest}' '${hubUrl(args)}/api/agent.jar'
    """
    if (args.tokenCredentialsId) {
        withCredentials([string(credentialsId: args.tokenCredentialsId, variable: 'COVHUB_TOKEN')]) {
            sh script
        }
    } else {
        sh script
    }
    echo "[covhub] agent 已下载到 ${dest}"
    return dest
}

/**
 * 从 hub 取回某个版本的 class 产物，解到 dest 目录，返回该目录。
 *
 * 推 Sonar 时 -Dsonar.java.binaries 必须是**采集时运行的那份 class**（JaCoCo 按
 * CRC64 class id 匹配，对不上就是 0%）。有了它，发版节点不必自己囤历史产物 ——
 * hub 上有 upload-classes 传上去的那一份，或结算时 manifest 记下的路径。
 */
String fetchClasses(Map args) {
    assert args.service : 'fetchClasses 需要 service'
    assert args.version : 'fetchClasses 需要 version'
    assert hubUrl(args) : 'fetchClasses 需要 hub（或环境变量 COVHUB_URL）'
    String dest = args.dest ?: "${pwd()}/.covhub-classes/${args.version}"
    String url = "${hubUrl(args)}/api/classes?service=${enc(args.service)}&version=${enc(args.version)}"
    String script = """
        set -e
        tmp=\$(mktemp)
        code=\$(curl -sS -H "X-Covhub-Token: \${COVHUB_TOKEN:-}" -o "\$tmp" -w '%{http_code}' '${url}')
        case "\$code" in
            2*) ;;
            *) cat "\$tmp" >&2; echo >&2
               echo "[covhub] 取 class 产物失败，HTTP \$code" >&2; rm -f "\$tmp"; exit 1 ;;
        esac
        rm -rf '${dest}' && mkdir -p '${dest}'
        tar xzf "\$tmp" -C '${dest}'
        rm -f "\$tmp"
    """
    if (args.tokenCredentialsId) {
        withCredentials([string(credentialsId: args.tokenCredentialsId, variable: 'COVHUB_TOKEN')]) {
            sh script
        }
    } else {
        sh script
    }
    echo "[covhub] ${args.service} ${args.version} 的 class 产物已解到 ${dest}"
    return dest
}

/**
 * 从 hub 下载某个已结算版本的 jacoco.xml，用于在本节点推 Sonar。
 * hub 的看板本身就是静态文件服务，报告直接按路径取。
 */
String fetchReport(Map args) {
    assert args.service : 'fetchReport 需要 service'
    assert args.version : 'fetchReport 需要 version'
    String dest = args.dest ?: "${pwd()}/jacoco-runtime.xml"
    assert hubUrl(args) : 'fetchReport 需要 hub（或环境变量 COVHUB_URL）'
    sh """
        set -e
        curl -sSf -o '${dest}' \\
             '${hubUrl(args)}/${enc(args.service)}/versions/${enc(args.version)}/jacoco.xml'
    """
    echo "[covhub] ${args.service} ${args.version} 的报告已下载到 ${dest}"
    return dest
}

/**
 * 把运行期覆盖率推 SonarQube。
 *
 * 强烈建议用与单元测试不同的 projectKey（如 my-service-runtime），
 * 两个 project 并列才能横向对比：既无单测、线上也没人跑的代码可以考虑删除；
 * 线上频繁执行却没有单测保护的，是补测试的最高优先级。
 */
void pushSonar(Map args) {
    assert args.projectKey : 'pushSonar 需要 projectKey'
    assert args.xmlReport : 'pushSonar 需要 xmlReport（jacoco.xml 路径）'
    assert args.binaries : 'pushSonar 需要 binaries（与该 xml 对应的 class 目录）'
    String sources = args.sources ?: 'src/main/java'
    String name = args.projectName ?: args.projectKey
    String serverName = args.sonarEnv ?: 'SonarQube'

    withSonarQubeEnv(serverName) {
        sh """
            sonar-scanner \
              -Dsonar.projectKey=${args.projectKey} \
              -Dsonar.projectName='${name}' \
              -Dsonar.sources=${sources} \
              -Dsonar.java.binaries=${args.binaries} \
              -Dsonar.coverage.jacoco.xmlReportPaths=${args.xmlReport}
        """
    }
}

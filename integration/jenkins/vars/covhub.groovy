#!groovy
/**
 * covhub —— Jenkins Shared Library
 *
 * 安装：把 integration/jenkins 目录作为一个 Shared Library 仓库配置到
 * Manage Jenkins → System → Global Pipeline Libraries，名字建议就叫 covhub。
 * 之后在 Jenkinsfile 顶部 @Library('covhub') _ 即可使用。
 *
 * 所有方法都接受一个 Map，公共参数：
 *   home    covhub 安装目录，默认 /opt/coverage-hub
 *   config  targets.json 路径，默认 <home>/targets.json
 *   python  python 可执行文件，默认 python3
 */

/** 内部：拼出 covhub 命令前缀 */
private String cli(Map args) {
    String home = args.home ?: '/opt/coverage-hub'
    String config = args.config ?: "${home}/targets.json"
    String python = args.python ?: 'python3'
    return "${python} ${home}/covhub.py -c ${config}"
}

/**
 * 取该服务应注入的 -javaagent 参数串。
 * 用法：env.JAVA_TOOL_OPTIONS = covhub.agentOpts(service: 'my-service')
 */
String agentOpts(Map args) {
    assert args.service : 'agentOpts 需要 service'
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
    String extra = ''
    if (args.version) {
        extra += " --version ${args.version}"
    }
    if (args.allowMissing) {
        extra += ' --allow-missing'
    }
    echo "[covhub] 结算 ${args.service} 版本 ${args.version ?: '(取配置)'} 的运行期覆盖率"
    sh "${cli(args)} predeploy ${args.service}${extra}"
}

/** 拉一次快照（累加，不清零），用于流水线中途取样 */
void dump(Map args) {
    assert args.service : 'dump 需要 service'
    sh "${cli(args)} dump ${args.service}"
}

/** 打印目标连通性与最新覆盖率；部署后用它确认新实例的 agent 已就绪 */
void status(Map args = [:]) {
    sh "${cli(args)} status${args.service ? ' ' + args.service : ''}"
}

/**
 * 判断目标 agent 是否可连通。部署后轮询用。
 * status 输出里该服务那一行的「连通」列为 ok 即视为就绪。
 */
boolean online(Map args) {
    assert args.service : 'online 需要 service'
    String out = sh(script: "${cli(args)} status ${args.service}", returnStdout: true)
    echo out
    return out.readLines().any { line ->
        line.trim().startsWith(args.service as String) && line =~ /\bok\b/
    }
}

/**
 * 发版后更新 targets.json：版本号 + class 产物路径。
 *
 * 这一步最容易被漏掉。JaCoCo 按 CRC64 class id 匹配数据，class 产物没跟着换，
 * 新版本采到的 exec 会和旧 class 对不上，报告全是"未覆盖"。
 */
void retarget(Map args) {
    assert args.service : 'retarget 需要 service'
    assert args.version : 'retarget 需要 version'
    String home = args.home ?: '/opt/coverage-hub'
    String config = args.config ?: "${home}/targets.json"
    String python = args.python ?: 'python3'
    String classfiles = args.classfiles ? groovy.json.JsonOutput.toJson(args.classfiles) : 'null'

    writeFile file: '.covhub-retarget.py', text: """
import json, sys
config, service, version = sys.argv[1], sys.argv[2], sys.argv[3]
classfiles = json.loads(sys.argv[4])
with open(config, encoding='utf-8') as f:
    cfg = json.load(f)
hit = False
for svc in cfg['services']:
    if svc['name'] == service:
        svc['version'] = version
        if classfiles:
            svc['classfiles'] = classfiles if isinstance(classfiles, list) else [classfiles]
        hit = True
if not hit:
    raise SystemExit('targets.json 里没有服务 ' + service)
with open(config, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print('[covhub] %s -> version=%s classfiles=%s' % (service, version, classfiles))
"""
    sh "${python} .covhub-retarget.py '${config}' '${args.service}' '${args.version}' '${classfiles}'"
    sh 'rm -f .covhub-retarget.py'
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

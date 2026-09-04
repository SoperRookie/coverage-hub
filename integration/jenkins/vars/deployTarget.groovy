#!groovy
/**
 * deployTarget —— 把被测服务部署起来，并注入 JaCoCo agent。
 *
 * 覆盖五种常见方式：docker / compose / k8s / systemd / script。
 * 共同点只有一个：把 agent 参数塞进目标 JVM 的 JAVA_TOOL_OPTIONS，
 * 被测服务本身不需要任何改动。
 *
 * 统一入口：
 *   deployTarget.run(mode: 'docker', javaToolOptions: opts, ...)
 *
 * 每种方式各自需要的参数见对应方法的注释。
 */

/** 按 mode 分发 */
void run(Map args) {
    String mode = args.mode ?: 'script'
    echo "[deploy] 方式=${mode}"
    switch (mode) {
        case 'docker':   docker(args);  break
        case 'compose':  compose(args); break
        case 'k8s':      k8s(args);     break
        case 'systemd':  systemd(args); break
        case 'script':   script(args);  break
        default: error("未知的部署方式 '${mode}'，可选：docker / compose / k8s / systemd / script")
    }
}

// ---------------------------------------------------------------- docker

/**
 * docker run 直接起容器。
 *
 * 必填：image, containerName, javaToolOptions
 * 选填：appPort（如 '8080:8080'）、agentPort（默认 '6300:6300'）、
 *      agentLibDir（宿主机上 jacocoagent.jar 所在目录，挂进容器）、
 *      agentMountPath（容器内挂载点，默认 /opt/jacoco）、
 *      extraArgs（追加到 docker run 的其他参数）、network、envs（Map）
 *
 * 注意：targets.json 里该服务的 jacocoAgent 必须写成【容器内】路径，
 * 也就是 agentMountPath 下的 jacocoagent.jar —— agent 是在容器里加载的。
 */
void docker(Map args) {
    assert args.image : 'docker 方式需要 image'
    assert args.containerName : 'docker 方式需要 containerName'
    assert args.javaToolOptions : 'docker 方式需要 javaToolOptions'

    String mountPath = args.agentMountPath ?: '/opt/jacoco'
    List<String> opts = []
    opts << "-d --name ${args.containerName}"
    if (args.appPort)   { opts << "-p ${args.appPort}" }
    opts << "-p ${args.agentPort ?: '6300:6300'}"
    if (args.agentLibDir) { opts << "-v ${args.agentLibDir}:${mountPath}:ro" }
    if (args.network)   { opts << "--network ${args.network}" }
    (args.envs ?: [:]).each { k, v -> opts << "-e ${k}='${v}'" }
    opts << "-e JAVA_TOOL_OPTIONS='${args.javaToolOptions}'"
    if (args.extraArgs) { opts << args.extraArgs }

    sh """
        set -e
        # 停旧实例。此时旧版本覆盖率必须已经结算过（流水线第 1 步）
        docker rm -f '${args.containerName}' 2>/dev/null || true
        docker pull '${args.image}'
        docker run ${opts.join(' ')} '${args.image}'
        docker ps --filter 'name=${args.containerName}' --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
    """
}

// --------------------------------------------------------------- compose

/**
 * docker compose，用 override 文件注入 agent，不改原始 compose 文件。
 *
 * 必填：composeFile, serviceName, javaToolOptions
 * 选填：image（覆盖镜像）、agentPort（默认 '6300:6300'）、
 *      agentLibDir + agentMountPath、projectName
 */
void compose(Map args) {
    assert args.composeFile : 'compose 方式需要 composeFile'
    assert args.serviceName : 'compose 方式需要 serviceName'
    assert args.javaToolOptions : 'compose 方式需要 javaToolOptions'

    String mountPath = args.agentMountPath ?: '/opt/jacoco'
    String override = 'docker-compose.covhub.yml'

    List<String> lines = []
    lines << 'services:'
    lines << "  ${args.serviceName}:"
    if (args.image) {
        lines << "    image: ${args.image}"
    }
    lines << '    environment:'
    lines << "      JAVA_TOOL_OPTIONS: \"${args.javaToolOptions}\""
    lines << '    ports:'
    lines << "      - \"${args.agentPort ?: '6300:6300'}\""
    if (args.agentLibDir) {
        lines << '    volumes:'
        lines << "      - ${args.agentLibDir}:${mountPath}:ro"
    }
    writeFile file: override, text: lines.join('\n') + '\n'
    echo "[deploy] 生成 override：\n" + lines.join('\n')

    String project = args.projectName ? "-p ${args.projectName}" : ''
    sh """
        set -e
        docker compose ${project} -f '${args.composeFile}' -f '${override}' pull '${args.serviceName}' || true
        docker compose ${project} -f '${args.composeFile}' -f '${override}' up -d '${args.serviceName}'
        docker compose ${project} -f '${args.composeFile}' -f '${override}' ps '${args.serviceName}'
    """
}

// ------------------------------------------------------------------- k8s

/**
 * Kubernetes 滚动更新。
 *
 * 必填：deployment, javaToolOptions
 * 选填：namespace（默认 default）、container（默认取 deployment 名）、
 *      image、rolloutTimeout（默认 5m）、kubeconfig
 *
 * 前置：agent jar 必须在 Pod 里可达。业务镜像不方便改时，标准做法是给
 * Deployment 加一个 initContainer 把 jacocoagent.jar 拷进 emptyDir 共享卷，
 * 见 integration/deployment-snippets.md。这一步属于 Deployment 清单的一次性
 * 改造，不在本流水线范围内。
 *
 * 另外：滚动更新会直接杀掉旧 Pod，preStop 钩子来不及做完整的 dump + 归档，
 * 所以结算必须在调用本方法【之前】完成（流水线第 1 步就是干这个的）。
 */
void k8s(Map args) {
    assert args.deployment : 'k8s 方式需要 deployment'
    assert args.javaToolOptions : 'k8s 方式需要 javaToolOptions'

    String ns = args.namespace ?: 'default'
    String container = args.container ?: args.deployment
    String kc = args.kubeconfig ? "--kubeconfig='${args.kubeconfig}'" : ''
    String timeout = args.rolloutTimeout ?: '5m'

    List<String> steps = []
    if (args.image) {
        steps << "kubectl ${kc} -n ${ns} set image deployment/${args.deployment} ${container}='${args.image}'"
    }
    // set env 本身会触发一次滚动更新；与 set image 合并成一次更新，避免连滚两轮
    steps << "kubectl ${kc} -n ${ns} set env deployment/${args.deployment} " +
             "-c ${container} JAVA_TOOL_OPTIONS='${args.javaToolOptions}'"
    steps << "kubectl ${kc} -n ${ns} rollout status deployment/${args.deployment} --timeout=${timeout}"
    steps << "kubectl ${kc} -n ${ns} get pods -l app=${args.deployment} -o wide"

    sh "set -e\n" + steps.join('\n')
}

// --------------------------------------------------------------- systemd

/**
 * systemd 管理的裸机 / 虚拟机服务。
 *
 * 必填：unit（如 my-service.service）、javaToolOptions
 * 选填：artifactSrc + artifactDest（先投放新版本制品再重启）、sudo（默认 true）
 *
 * 用 drop-in 片段注入环境变量，不改原始 unit 文件 —— 撤下监控时删掉该文件即可。
 */
void systemd(Map args) {
    assert args.unit : 'systemd 方式需要 unit'
    assert args.javaToolOptions : 'systemd 方式需要 javaToolOptions'

    String sudo = (args.sudo == false) ? '' : 'sudo '
    String unit = args.unit.endsWith('.service') ? args.unit : "${args.unit}.service"
    String dropinDir = "/etc/systemd/system/${unit}.d"

    String deployArtifact = ''
    if (args.artifactSrc && args.artifactDest) {
        deployArtifact = """
        ${sudo}install -D -m 0644 '${args.artifactSrc}' '${args.artifactDest}'
        """
    }

    sh """
        set -e
        ${sudo}mkdir -p '${dropinDir}'
        ${sudo}tee '${dropinDir}/covhub.conf' > /dev/null <<'DROPIN'
[Service]
Environment="JAVA_TOOL_OPTIONS=${args.javaToolOptions}"
DROPIN
        ${deployArtifact}
        ${sudo}systemctl daemon-reload
        ${sudo}systemctl restart '${unit}'
        ${sudo}systemctl --no-pager --lines=0 status '${unit}'
    """
}

// ---------------------------------------------------------------- script

/**
 * 自定义脚本。参数以环境变量形式传入，避免命令行拼接引发的转义问题。
 *
 * 必填：deployScript, javaToolOptions
 * 选填：service、version、envs（Map，额外环境变量）
 */
void script(Map args) {
    assert args.deployScript : 'script 方式需要 deployScript'
    assert args.javaToolOptions : 'script 方式需要 javaToolOptions'

    withEnv([
        "COVHUB_JAVA_TOOL_OPTIONS=${args.javaToolOptions}",
        "COVHUB_SERVICE=${args.service ?: ''}",
        "COVHUB_VERSION=${args.version ?: ''}",
    ] + (args.envs ?: [:]).collect { k, v -> "${k}=${v}" }) {
        sh """
            set -e
            # 脚本内用 \$COVHUB_JAVA_TOOL_OPTIONS 取 agent 参数，
            # 把它设成目标 JVM 的 JAVA_TOOL_OPTIONS 即可
            ${args.deployScript}
        """
    }
}

# syntax=docker/dockerfile:1
#
# dragon-agent 的生产镜像。
#
# 四个硬约束，都在下面这一份文件里落地：
#
#   1. **镜像分层**：构建阶段与运行阶段分开——运行镜像里没有编译器、
#      没有构建缓存、没有 pip 的下载缓存。改一行代码不用重装依赖。
#   2. **只读根**：运行阶段不写文件系统（``PYTHONDONTWRITEBYTECODE=1``），
#      需要写的地方只有两个挂载点：``/workspace``（工作目录）与 ``/tmp``。
#      容器可以以 ``--read-only`` 启动。
#   3. **非 root**：进程以 uid 10001 运行，没有 shell、没有 home 目录写权限。
#   4. **最小出网**：**镜像本身管不了出网**——那是编排层的事
#      （见 ``deploy/k8s.yaml`` 的 NetworkPolicy 与 ``deploy/compose.yaml``）。
#      镜像能做的是：把要用的东西都带上，把用不到的留下。
#
# 本工程运行期零第三方依赖，所以运行阶段连 ``pip install`` 都不需要。

# --------------------------------------------------------------------------- 构建阶段
FROM python:3.12-slim AS builder

WORKDIR /build

# 先只拷元数据，让依赖层可以被缓存。本工程没有第三方依赖，这一步几乎不花钱，
# 但它保证了「以后加了依赖」时不用重编全部源码。
COPY pyproject.toml README.md ./
COPY dragon_agent ./dragon_agent

# 预编译：把 .pyc 提前生成，运行阶段就不再需要写权限。
RUN python -m compileall -q dragon_agent \
 && PYTHONPATH=/build python -c "import dragon_agent.cli as c; c.build_parser()" \
 && echo "构建阶段自检通过：CLI 解析器可构造"

# --------------------------------------------------------------------------- 运行阶段
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="dragon-agent" \
      org.opencontainers.image.description="《开源 Agent 架构技术全书》第二卷配套工程：自建 Agent 的上线镜像" \
      org.opencontainers.image.source="https://github.com/example/dragon-agent" \
      org.opencontainers.image.licenses="NOASSERTION"

# PYTHONUNBUFFERED：日志不缓冲——容器被 kill 时，最后几行日志不会丢。
# PYTHONDONTWRITEBYTECODE：不在运行期写 .pyc——这是只读根的前提。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/dragon-agent \
    DRAGON_WORKSPACE=/workspace \
    DRAGON_TRACE_DIR=/workspace/traces \
    HOME=/tmp

# 非 root 用户。没有 home 目录、不装 shell——攻击者拿到 shell 也没地方落脚。
RUN groupadd --gid 10001 dragon \
 && useradd --uid 10001 --gid dragon --no-create-home --shell /usr/sbin/nologin dragon \
 && mkdir -p /workspace /tmp/dragon \
 && chown -R dragon:dragon /workspace /tmp/dragon

COPY --from=builder --chown=dragon:dragon /build/dragon_agent /opt/dragon-agent/dragon_agent

WORKDIR /workspace
USER 10001:10001

# 健康检查用「列一次工具」：不联网、不调模型、只读内存，退出码 0 即健康。
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-m", "dragon_agent.cli", "tools"]

ENTRYPOINT ["python", "-m", "dragon_agent.cli"]
CMD ["demo"]

# 只读根启动示例（配合 deploy/compose.yaml）：
#   docker run --rm --read-only \
#     --tmpfs /tmp:rw,noexec,nosuid,size=64m \
#     --volume dragon-traces:/workspace/traces \
#     -e LLM_API_KEY=... dragon-agent:1.0 run "查一下项目预算"

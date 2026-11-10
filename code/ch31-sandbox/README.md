# ch31 沙箱与执行隔离 —— 配套示例

《开源 Agent 架构技术全书》第 31 章的参考实现。三层防御：

1. **决策与执行分离**：模型只产出命令候选（结构化 JSON），绝不直接拿到 `subprocess`；
2. **执行环境最小化**：独立进程组 / 独立 workdir / 环境变量白名单 / 资源限额 / 墙上时钟超时 / 整组 kill / 输出裁剪；
3. **全链路可审计**：每条命令的判定、理由、退出码、耗时落成一行 JSON。

## 依赖

- Python **3.8+**，**只用标准库**，无需 `pip install` 任何东西；
- Docker 引擎模式额外需要本机 Docker Engine，并预先拉好镜像：`docker pull python:3.12-slim`；
- 默认 **mock 模式不需要任何 API Key**，可直接跑通完整回路；
- `--live` 才需要 `OPENAI_API_KEY`。

## 安装

```bash
cd code/ch31-sandbox
python -c "import sys; print(sys.version)"
```

Windows 下用 `py -3` 代替 `python`。

## 运行

```bash
# 1) mock 全场景：正常 / 写文件 / 出网 / 敏感文件 / 路径逃逸 / shell 元字符 / 白名单外 / 超时
python main.py

# 2) 只跑一个场景
python main.py --scenario egress
python main.py --scenario timeout

# 3) Docker 引擎模式（--read-only --network none --cap-drop ALL --no-new-privileges ...）
docker pull python:3.12-slim
python main.py --engine docker --scenario benign

# 4) 放宽超时，观察挂起命令被整组 kill
python main.py --scenario timeout --timeout 1

# 5) 真实模型产生命令候选（候选同样要过策略判定）
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.openai.com/v1     # 可选，任意 OpenAI 兼容端点
export OPENAI_MODEL=gpt-4o-mini                       # 可选
python main.py --live --prompt "在一个临时工作目录里写一个 hello.txt，内容为 hello"

# 6) 单独放开出网白名单里的命令（默认禁止）
python main.py --scenario egress --allow-network
```

主要参数：`--engine {local,docker}`、`--scenario`、`--live`、`--prompt`、`--timeout`、`--mem-mb`、`--cpu-s`、`--max-pids`、`--max-output-bytes`、`--allow-network`、`--audit`。

## `--live` 所需环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `OPENAI_API_KEY` | 是 | — | OpenAI 兼容接口的密钥 |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | 接口根地址，可指向任意兼容端点或本地网关 |
| `OPENAI_MODEL` | 否 | `gpt-4o-mini` | 模型名 |

`--live` 调用 `POST {OPENAI_BASE_URL}/chat/completions`，要求模型只返回 `{"commands":[{"cmd":[...],"why":"..."}]}`；解析失败或请求失败时会自动回落到 mock 场景，演示不会中断。

## 输出说明

每条命令输出一行结论：

```
[RUN   ] ... -c print('hello from the sandbox')  ->  exit=0  12ms
[BLOCK] curl http://169.254.169.254/latest/meta-data/  ->  出网被拒绝: curl
```

结尾给出统计与审计文件路径。`audit.jsonl` 每行一条 JSON，含 `cmd / why / allowed / reason / exit_code / duration_ms / stdout / stderr / timed_out`。

`cmd` 字段记的是**实际执行的命令**，沙箱里跑 Python 时第一项就是解释器的绝对路径——每台机器都不一样。仓库里这份已归一化成 `python`，只为当输出格式的参照；你本地跑出来的会是自己的路径，这是正常的。文件是**追加**写的，跑一次多几行；需要干净的一份就先删掉再跑。

## 与正文的对应关系

| 正文位置 | 代码位置 |
|---|---|
| 2.2 资源限额 | `SandboxPolicy.mem_mb / cpu_s / max_pids` + `_apply_rlimits()`（`RLIMIT_CPU` / `RLIMIT_AS` / `RLIMIT_NPROC` / `RLIMIT_FSIZE` / `RLIMIT_CORE`） |
| 2.3 文件系统策略 | `DockerRunner` 的 `--read-only`、`--tmpfs /tmp`、只挂载 `/work`；本地模式用独立 workdir + 路径白名单 |
| 2.4 网络策略 | `DockerRunner` 的 `--network none`；`judge()` 里的 `NETWORK_TOOLS` 与 `SENSITIVE_HINTS`（含 `169.254.169.254`） |
| 2.6 超时与整组 kill | `LocalRunner` 的 `setsid` + `killpg`；`_kill_tree()` 在 Windows 上用 `taskkill /T` |
| 3.x 授权不在沙箱职责内 | `judge()` 只管执行面；数据面靠服务端最小权限凭据，正文 3.5 节说明 |

## 已知限制

- 本地模式在 Windows 上**不支持 POSIX rlimit**，退化为「进程组 + 超时 + 输出裁剪 + 路径/环境策略」，完整资源限额请用 Linux 或 Docker 模式；
- 本例未包含 seccomp profile、AppArmor/SELinux 策略文件与 cgroup 配置，这些需要在生产环境按 2.2 节逐条显式落地；
- Docker 模式需要能拉取镜像，离线环境请预先 `docker load`；
- 示例只演示「命令级」隔离。真实 Agent 还要处理浏览器 profile 隔离、输出令牌预算、快照回滚等，见正文 2.5 与 2.6。

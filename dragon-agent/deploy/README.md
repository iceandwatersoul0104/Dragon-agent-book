# 部署清单说明

本目录是 `dragon-agent` 的容器化清单。四份文件，各管一段：

| 文件 | 管什么 | 谁执行 |
| --- | --- | --- |
| `../Dockerfile` | 镜像怎么构建、分几层、用哪个用户 | 构建时 |
| `../.dockerignore` | 什么不进构建上下文（尤其是密钥） | 构建时 |
| `compose.yaml` | 单机/本地编排：只读根、非 root、资源限额 | 运维 |
| `k8s.yaml` | 集群编排：出网白名单、探针、定时任务 | 运维 |

## 四个约束分别落在哪

### 1. 镜像分层

构建阶段与运行阶段分开（`FROM ... AS builder` / `FROM ... AS runtime`）。

运行镜像里没有编译器、没有 pip 缓存、没有构建工具——只有 Python 解释器和
`dragon_agent/` 一个包。本工程运行期零第三方依赖，所以运行阶段连
`pip install` 都不需要。

改一行业务代码不需要重装依赖层，这是分层最直接的收益。

### 2. 只读根

三个条件同时成立，根文件系统才能只读：

- `PYTHONDONTWRITEBYTECODE=1`——运行期不写 `.pyc`；
- 需要写的地方只有两个挂载点：`/tmp`（tmpfs）与 `/workspace`（卷）；
- `--read-only` 启动时（compose 里是 `read_only: true`）。

只读根的价值不是"洁癖"：它把"进程写了不该写的地方"从**运行期异常**变成
**启动即失败**。这类 bug 在可写容器里可能要等几天才暴露。

### 3. 非 root

Dockerfile 里建 uid 10001 的用户，容器以 `10001:10001` 运行；编排层的
`runAsNonRoot: true` / `runAsUser: 10001` 必须与之一致。

两边不一致会出两个问题：容器直接起不来（`runAsNonRoot` 与实际用户冲突），
或者挂载卷的属主对不上，进程写不了 trace 目录。

配套的还有：丢弃全部 capabilities、`allowPrivilegeEscalation: false`、
不自动挂载 ServiceAccount token。

### 4. 最小出网

**镜像管不了出网，compose 也管不了。**

`compose.yaml` 里的 `internal: true` 网络能保证"内部服务之间不通外网"，
但应用容器本身在默认网桥上，它要访问模型 API，出网是必然的。

真正能做白名单的是 Kubernetes 的 NetworkPolicy——K8s 默认网络是放行的，
一条策略就能把出网收成白名单。`k8s.yaml` 里给了三条规则：DNS、模型服务
网段、内部服务网段。

**注意**：`k8s.yaml` 里的 `<模型服务网段>` 与 `<内部服务网段>` 是占位符。
不换掉就等于这条规则形同虚设（放行一个空的网段等于什么都不放行，
或写错成 0.0.0.0/0 就等于全部放行）。上线前必须替换。

## 配置与密钥：环境变量还是配置中心

两条界线：

| 类别 | 放哪 | 例子 | 改动的代价 |
| --- | --- | --- | --- |
| **非密配置** | 配置中心 / ConfigMap → 环境变量 | 模型标识、预算档位、工作目录 | 滚动更新即可 |
| **密钥** | 密钥管理系统 → 环境变量（运行时注入） | `LLM_API_KEY` | 轮换要重启 Pod |

为什么不把非密配置写进镜像：**镜像不该因配置而重新构建**。同一个镜像
在测试和生产跑，差别只在环境变量。

为什么不把配置写进文件挂载：文件挂载要等 Pod 起来才能生效，而环境变量
在调度时就在了；而且挂载的文件在容器里是可读的，密钥走文件等于多一次
泄露面。

本工程里配置的实际读取路径：

- `LLM_API_KEY` / `OPENAI_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 由
  `dragon_agent/config.py` 读取——**唯一入口**；
- 成本护栏档位（`DRAGON_WARN_RATIO` 等）由 `dragon_agent/cli.py` 读取，
  优先级是**命令行 > 环境变量 > 内置默认值**。

## 密钥轮换与审计

### 轮换

1. 在密钥管理系统里生成新 key，**先不撤销旧 key**——新旧并存，避免轮换
   瞬间全量 401。
2. 更新集群里的 Secret（新旧两个 key 都保留一段时间）。
3. 滚动重启 Pod，让新值生效。
4. 确认新 key 有流量之后，再撤销旧 key。

反过来做（先撤销后下发）会有一段全量失败。轮换窗口选在低峰，
并提前把降级模型的配额准备好。

### 审计

要能回答三个问题：

1. **谁在什么时候用了哪把 key？**——密钥系统自身的访问日志 +
   Pod 的 `kubectl logs`（容器日志里不会出现 key，因为
   `Config.to_dict()` 不含 `api_key`，日志脱敏在采集端做）。
2. **有没有 key 进了镜像层或代码库？**——CI 里跑密钥扫描；
   `.dockerignore` 排掉 `.env*` 与 `*.key`；构建上下文里不该有它们。
3. **key 多久没换了？**——`k8s.yaml` 里的 `secret.rotation.days: "30"`
   注解就是给审计用的：到点没轮换，巡检报出来。

**三条纪律**：

- key 只出现在环境变量里，不出现在命令行（命令行会进 shell 历史与
  `ps` 输出）；
- key 不进日志、不进 trace、不进检查点——`Config.to_dict()` 是落盘的
  唯一入口，它不含 `api_key`；
- 轮换演练要真的做过。没演练过的轮换流程等于没有。

## 起容器

```powershell
cd dragon-agent

# 1) 构建
docker build -t dragon-agent:1.0 .

# 2) 只读根跑一次（mock 模式，不需要 key）
docker run --rm --read-only `
  --tmpfs /tmp:rw,noexec,nosuid,size=64m `
  dragon-agent:1.0 demo

# 3) 接真实模型
docker run --rm --read-only `
  --tmpfs /tmp:rw,noexec,nosuid,size=64m `
  -e LLM_API_KEY=$env:LLM_API_KEY `
  dragon-agent:1.0 run "查一下项目预算" --budget 20000 --cost 0.05
```

缺 key 时容器会以退出码 3 结束并打印提示——**这是设计好的行为**，
不是崩溃。

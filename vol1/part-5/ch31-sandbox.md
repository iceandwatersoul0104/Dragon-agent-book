# 第 31 章 沙箱与执行隔离

## 一、本章要解决的问题

凌晨两点，值班手机响了：自动编码 Agent 在一个 PR 上卡了四十分钟后，CI 机器上的 `/home` 目录被整包发往一个外部地址。事后复盘，Agent 没有任何恶意，它只是照着 README 里"安装依赖"那一栏执行了 `curl ... | sh`，而 README 来自那个 PR 的作者。

这类事故的共同点是：Agent 读到的所有东西都可能被投毒，而它写下的东西直接作用在真实系统上。

一个编码 Agent 面对的输入至少有三类：

- 从陌生 fork 拉下来的 PR 代码；
- 仓库 README、issue 评论、网页里的自然语言"操作步骤"；
- 上一轮自己生成、又被后续 prompt 复述过的内容。

而 Agent 的输出不是一段文字，是命令、是文件写入、是 HTTP 请求、是 git push。这些动作在宿主机上以你的身份、你的权限、你的凭据执行。

真实的失败模式大致归成四类：

1. **直接落宿主机**。Agent 在开发机上直接 `exec`，`rm -rf`、`git reset --hard`、写到项目外的路径，全都没有边界。
2. **假隔离**。以为起了容器就安全，但 `-v /:/host`、`-v /var/run/docker.sock:/var/run/docker.sock`、`--privileged` 让容器等价于宿主机 root。
3. **状态复用**。所有会话共用同一个 workdir、同一个容器、同一个浏览器 profile，一个会话拉进来的文件、种下的 cookie、装上的依赖，被下一个会话直接继承。
4. **越权凭据**。沙箱（sandbox）里塞着一把能删库、能发信的长期 token。沙箱再强，也拦不住 Agent 用这把钥匙把数据 POST 到一个允许出网的域名。

所以沙箱的目标要先说清楚：**它不是用来判断 Agent 说的话对不对，而是用操作系统强制执行的方式，把"它读到的内容"与"它能造成的破坏"隔开。** 边界由内核、容器运行时和你的挂载参数决定，不由模型的判断力决定。

## 二、原理：机制拆解

### 2.1 隔离强度谱系

```
隔离强度 ↑
   │
   │  微虚机            Firecracker / Cloud Hypervisor / Kata
   │  ────────────     独立 guest kernel + 受限 virtio 设备模型
   │                    逃逸要打穿 VMM 与 guest kernel 两层
   │
   │  用户态内核         gVisor(runsc) / WasmEdge
   │  ────────────     在用户态实现 Linux syscall 子集
   │                    宿主机内核只看到少量受控 IPC
   │
   │  容器               runc / containerd / Docker
   │  ────────────     共享宿主机内核
   │                    namespace + capability + seccomp + LSM 划线
   │
   │  进程级隔离         chroot + setrlimit + Landlock
   │  ────────────     同 uid、同文件系统，靠 rlimit 与路径规则兜底
   └────────────────────────────────────────→ 开销 ↑
```

关键结论只有一句：**容器不是虚拟机**。容器内的进程调用的仍然是宿主机的系统调用，`/proc` 只是被 namespace 过滤后的视图。所以"容器逃逸"的本质是宿主内核漏洞。gVisor 的思路是换一个内核位置——用 Go 写一个用户态内核（sentry），容器内的 syscall 被翻译成结构化 IPC，宿主机内核只暴露实现过的那一小部分接口，攻击面因此被压扁。Firecracker 更彻底：用 KVM 起一个带极简设备模型的完整 guest 内核，容器体验由虚拟机外壳提供。

代价必须一起算：

| 方案 | 隔离强度 | 冷启动 | 额外内存 | 运维复杂度 |
|---|---|---|---|---|
| 进程级 | 低 | 几乎为零 | 零 | 低 |
| 容器 | 中 | 几十~几百毫秒 | 几 MB | 中 |
| gVisor | 中高 | 百毫秒级 | 几十 MB 量级 | 中高 |
| 微虚机 | 高 | 百毫秒级 | 百 MB 量级（guest 内核为主） | 高 |

表中为经验量级，随镜像、内核版本与配置变化，不是基准测试结果。

### 2.2 容器里真正起作用的六件事

1. **Namespace**：pid / mnt / net / uts / ipc / user。`--pid=private` 让容器看不到宿主机进程；`--network=none` 让它只剩 loopback；`--ipc=private` 隔离共享内存。
2. **Capabilities**：容器默认丢掉大部分 capability，只保留改文件属主、`NET_BIND_SERVICE` 这类少数几个。保留 `SYS_ADMIN` 基本等于交出 root。
3. **seccomp-bpf**：以白名单方式限制可用 syscall，未列出的直接返回 `EPERM`。真正要堵死的是 `mount`、`kexec_load`、`init_module`、`bpf`、`perf_event_open`、`ptrace` 这一类——它们是把普通代码变成提权代码的跳板。
4. **no_new_privs**：设置后进程无法通过 setuid/setcap 二进制提权，是容器内提权最常见路径的廉价补丁。
5. **LSM（AppArmor / SELinux）**：基于路径和类型的强制访问控制，比 capability 更细。容器里往往没开，需要单独写策略。
6. **cgroup v2**：资源限制的主力。`memory.max` 硬性封顶内存（超限由 OOM killer 直接杀进程），`cpu.max` 做周期配额，`pids.max` 限制进程数（防 fork 炸弹），`io.max` 限制块设备吞吐。

### 2.3 文件系统策略：少挂载即是安全

沙箱里最有价值的操作是"不挂"：

- 不挂 `/`、`/home`、`/etc`；
- 不挂 `docker.sock`，挂上等于把宿主机 root 权限交给容器内任意进程；
- 不用 `--privileged`、`hostPID`、`hostNetwork`；
- 根文件系统只读（`--read-only`），只给 `/tmp` 一个 tmpfs 写点；
- **输入用复制，输出用挂载**：把文件复制进 workdir，而不是 bind mount 源码目录。复制多花几毫秒，换来"改坏源文件"和"软链接逃逸"两类问题都不存在。

### 2.4 网络策略：默认拒绝，白名单放行

Agent 需要联网时，正确做法不是"开网"，而是走一条你能审计的通道：

- 用 HTTP(S) 正向代理做出口，按域名或 CIDR 放行，未匹配的连接直接拒绝；
- 显式阻断云元数据地址 `169.254.169.254` 与 link-local 网段——逃逸后偷凭据最常用的入口；
- 阻断回到宿主的路径：localhost、docker0 网桥地址、`host.docker.internal`；
- DNS 层防重绑定：先解析再校验 IP 白名单，避免"域名解析到 127.0.0.1"；
- 默认 `--network=none`，出网任务单独授权。

### 2.5 浏览器与 GUI：风险集中在渲染进程

浏览器 Agent 的沙箱问题和你以为的不一样：**真正的风险不是 Agent 点错了按钮，而是恶意网页里的代码打到了 Chromium 渲染进程的漏洞。** 所以：

- Chromium 自带多进程沙箱（browser / renderer / GPU / plugin 分进程，渲染进程用 seccomp-bpf 限制 syscall），这是第一层，必须开；
- **不要为了省事加 `--no-sandbox`**，那等于亲手拆掉这层；
- 每次任务用独立的 `user-data-dir`，不复用 profile，避免 cookie / localStorage / 登录态跨任务串味；
- 关掉自动下载与外部协议处理，限制剪贴板读取；
- headless 不等于没有渲染进程沙箱，headless Chromium 同样跑 sandbox；
- GUI 类任务要把整个桌面会话（X server、桌面、应用）关进独立容器或虚机，控制通道（VNC、CDP）单独走。

### 2.6 资源配额与超时：超时必须杀掉整棵进程树

- 墙上时钟超时与 CPU 时间限额是两件事：`sleep 100` 不吃 CPU，靠 wall clock 收；死循环吃 CPU，靠 `cpu.max` 或 `RLIMIT_CPU` 收；
- 超时后要杀**进程组**（`setsid` + `killpg`），只 kill 父进程会留下孤儿继续跑；
- 杀完后 workdir 要清理或快照回滚，否则上次的残留文件会成为下次的输入；
- 输出必须有上限。Agent 上下文有限，一条十万行日志就能挤爆会话，挤掉的往往正是错误信息；
- `pids.max` / `RLIMIT_NPROC` 防 fork 炸弹，这是资源型攻击打挂服务最常见的方式。

### 2.7 监督者伪代码

```text
run(command, policy):
    1. 静态判定：二进制在白名单？参数无 shell 元字符？路径都在 workdir 内？
       不通过 → 记录拒绝理由，直接返回，不执行
    2. 建执行环境：新 workdir，输入复制进来，环境变量按白名单重建
    3. 启动：独立进程组 / session
       cgroup: memory.max, cpu.max, pids.max
       rlimit: RLIMIT_CPU, RLIMIT_AS, RLIMIT_NPROC, RLIMIT_FSIZE
       网络: 默认 none，走代理时显式配置
    4. 限时限量收集 stdout/stderr，超时杀整组
    5. 回收：清理 workdir，输出裁剪后回填 Agent 上下文
    6. 审计：把「判定 + 结果 + 耗时」写成一行 JSON 落地
```

第 1 步和第 6 步最容易被跳过，也最值钱：前者把"模型说了什么"和"系统做了什么"隔开，后者让事故可以事后复盘。

## 三、为什么这样设计：取舍与失效条件

这一节是本章最该被反复读的部分。以下每一条都在说"什么时候**不**该用这个方案"。

**1. 任务完全可信时，不要上容器。** 代码、仓库、测试脚本都是你自己的，那层沙箱带来的启动开销、调试难度、镜像维护成本，换不回任何安全收益。一个 `subprocess` + `setrlimit` + 超时就够。

**2. 高频工具调用的 CLI Agent，不要每次起容器。** 一次会话几百次工具调用，容器冷启动按百毫秒算，累积起来就是分钟级的纯等待。正确做法是**长驻沙箱**：容器只起一次，每次工具调用往里 `exec`；需要状态复位时用文件系统快照或 git 分支回滚，而不是重启容器。

**3. 短命、大批量的任务，不要上微虚机。** Firecracker 的价值是"毫秒级冷启动 + 强隔离"，但它的内存开销主要来自 guest 内核，比容器高一个数量级。几千个短命 CLI Agent 用微虚机，机器会被内存吃光；这类场景容器池，甚至进程级隔离加 seccomp 就够。

**4. 不要把容器当成"恶意代码无害"的证明。** 容器是在缩小爆炸半径，不是在消除风险：挂 `docker.sock` 等于直接拿到宿主机 root；`--privileged` 让所有 namespace 与 capability 形同虚设；宿主内核一个 0day 就能绕开容器里的 seccomp 白名单和 namespace——gVisor、Firecracker、微虚机之所以存在，就是这个原因。

**5. 沙箱管不了授权问题。** 给 Agent 一把能 `DROP TABLE` 的数据库 token，沙箱再强也拦不住它把数据 POST 到一个允许出网的域名；反过来，沙箱里的进程照样能访问被授权的 API。数据面的安全必须靠**服务端策略**：最小权限凭据、只读视图、可撤销、有审计、敏感操作二次确认。这属于权限设计，不是隔离设计。

**6. 沙箱管不了提示注入。** 注入进来的文字在沙箱内依然是合法输入，Agent 读了照样照做。沙箱只保证"它做的那部分，最多只能毁掉沙箱里的东西"。防御注入要在输入侧做（来源标注、指令与数据的边界、外部内容降权），那是第 32 章的主题。

**7. 有不可回滚副作用的动作，不能只靠隔离兜底。** 沙箱内提交真实 PR、发出真实邮件、扣真实款项，这些动作一旦执行就无法回滚。它们需要幂等键、dry-run 模式、撤销通道与人工确认，隔离只是最后一道网。

**8. 过小的沙箱会把 Agent 逼出绕道路径。** 不给网，Agent 会去找环境里现成的代理；不给写权限，Agent 会去改自己的配置；不给装依赖的权限，Agent 会把依赖塞进 `/tmp`。看到"绕过"先别急着判定为恶意，先看是不是约束没覆盖真实工作流——总是被绕过的沙箱，不如把边界重新划到任务真正需要的位置。

**9. 可观测性缺失时，沙箱的收益会打折。** 你不会知道自己放出去过什么、失败在哪一步。沙箱、可观测、评测应当一起上，而不是分三期工程。

## 四、动手：最小可实现

参考实现放在 `code/ch31-sandbox/`，纯标准库、无第三方依赖，不需要 API Key 就能跑通完整回路。核心是把三层防御拆开，各自可单独测试。

**第一层：决策与执行分离。** LLM 只产出"命令候选"（结构化 JSON），绝不直接拿到 `subprocess`。候选必须过静态策略：可执行文件白名单、禁止 shell 元字符（`; | & $ > <`）、路径参数必须落在 workdir 内、禁止读取敏感路径。校验不通过就不执行，拒绝理由写进审计日志。

**第二层：执行环境。** 本地模式在 `setsid` 出的独立进程组里跑，配 `RLIMIT_CPU`、`RLIMIT_AS`、`RLIMIT_NPROC`、`RLIMIT_FSIZE`，独立 workdir，超时后 `killpg` 整组，输出裁剪；Docker 模式用 `--network none --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit --memory --cpus --user`，把上文参数逐条落实。

**第三层：审计。** 每条命令的判定、理由、退出码、耗时、截断后的输出写成一行 JSON，事后可完整回放。

```python
# 核心策略判定（节选，全文见 code/ch31-sandbox/main.py）
def judge(cmd, policy):
    if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
        return False, "命令必须是非空字符串列表"
    prog = os.path.basename(cmd[0])
    for arg in cmd[1:]:
        if any(ch in arg for ch in SHELL_META):          # ; | & $ ` > < 换行
            return False, f"参数含 shell 元字符: {arg!r}"
    if prog in NETWORK_TOOLS and not policy.allow_network:
        return False, f"出网被拒绝: {prog}"
    if prog not in policy.allowed_commands:
        return False, f"可执行文件不在白名单: {prog}"
    for arg in cmd[1:]:
        if _looks_like_path(arg):
            if any(h in arg for h in SENSITIVE_HINTS):
                return False, f"命中敏感路径: {arg}"
            if not _within(policy.workdir, arg):
                return False, f"路径越界: {arg}"
    return True, "ok"
```

Mock 模式用内置的假响应替代真实模型，输出几类必测样本：正常命令、工作目录内写文件、出网命令（应被拒）、宿主敏感文件（应被拒）、相对路径逃逸（应被拒）、shell 元字符（应被拒）、白名单外命令（应被拒）、以及一条故意挂起的命令（应被超时杀掉）。`--live` 打开后，模型产出的候选同样要过同一道判定——这条链路不变，换的只是候选从哪来。

## 五、开源实现对照

**OpenHands**（MIT，https://github.com/All-Hands-AI/OpenHands ）把"宿主机不执行用户代码"当作产品前提：Agent 的动作在 Docker 沙箱里发生，宿主机只暴露受控 API。值得借鉴的是沙箱与会话的绑定方式——一次会话一个沙箱，会话结束销毁。

**Claude Code**（专有商业条款，https://github.com/anthropics/claude-code ）走相反路线：本地直接执行，靠权限模式与确认提示控制。对自家可信仓库这是合理的效率取舍；要处理陌生 PR 或第三方代码，必须在它外面再包一层沙箱。

**browser-use**（MIT，https://github.com/browser-use/browser-use ）用无障碍树给模型描述页面元素并自主点击输入。它依赖 Chromium 自身的进程沙箱——"浏览器沙箱"只挡住渲染漏洞，挡不住 Agent 自己的错误操作，正好印证 2.5 节的分工。

**Stagehand**（MIT，https://github.com/browserbase/stagehand ）把 act/observe/extract 与确定性 Playwright API 混搭，用确定性换可控性。它展示的是自主性与可控性之间的连续谱：越靠近确定性 API，行为越可预测，越容易加策略约束。

**OpenLLMetry**（Apache-2.0，https://github.com/traceloop/openllmetry ）在沙箱内自动插桩，把 LLM 调用转成 OpenTelemetry span，回答"这次调用做了什么、访问了什么、烧了多少 token"。没有这层数据，成本优化和出网收紧都无从谈起。Langfuse、Arize Phoenix 可自托管在沙箱外集中收集，细节留到第 33 章。

> 许可证：OpenHands / browser-use / Stagehand 为 MIT，OpenLLMetry 为 Apache-2.0，Claude Code 为 Anthropic 商业条款，Langfuse 为 MIT Expat（`ee/` 等目录按 `ee/LICENSE` 另行授权），Arize Phoenix 为 Elastic License 2.0。商用前逐个确认。

## 六、坑与检查清单

上线前逐条打勾。

**执行环境**

- [ ] 没有以特权模式（`--privileged`）运行任何沙箱
- [ ] 没有挂载 `docker.sock`、`/var/run/docker.sock` 或宿主机根目录 `/`
- [ ] 没有使用 `hostPID` / `hostNetwork`
- [ ] 根文件系统只读，写入只发生在显式的 workdir / tmpfs
- [ ] 容器内以非 root 用户运行，capabilities 已 drop 全部
- [ ] 开启了 `no-new-privileges`
- [ ] 输入是复制进沙箱的，不是 bind mount 源目录
- [ ] 每个任务独立 workdir，会话结束清理

**资源与超时**

- [ ] 设置了内存上限（`memory.max` 或 `RLIMIT_AS`）
- [ ] 设置了 CPU 配额与 CPU 时间上限
- [ ] 设置了进程数上限（`pids.max`）
- [ ] 有墙上时钟超时，且超时后杀的是整个进程组
- [ ] stdout/stderr 有大小上限，不会撑爆 Agent 上下文
- [ ] workdir 有磁盘占用上限或定期清理

**网络**

- [ ] 默认禁止出网，需要出网的任务单独授权
- [ ] 显式阻断 `169.254.169.254` 与 link-local 网段
- [ ] 阻断容器访问宿主机的 localhost / 网桥地址 / `host.docker.internal`
- [ ] 出网走了可审计的代理，而不是直接放开

**凭据与权限**

- [ ] 沙箱里没有长期有效的全权限 token
- [ ] 凭据是短时、最小权限、可撤销的
- [ ] Agent 无法直接触达生产数据库 / 支付 / 发信接口，或这些动作有二次确认
- [ ] 审计日志里没有凭据明文（注意环境变量回显与命令行参数）

**策略与审计**

- [ ] 工具与命令有白名单，不依赖模型自觉
- [ ] 每次执行都留了可回放的审计记录
- [ ] 浏览器用独立 profile，没有共享 cookie
- [ ] 没有为了兼容而加 `--no-sandbox`
- [ ] 定期演练一次"逃逸尝试"：在沙箱里尝试读宿主文件、访问 metadata、连宿主端口，确认都被挡住

最后一条最容易被跳过，也最能暴露前面所有配置的真实有效水位——纸面上写了 `--read-only` 不等于真的挡住了。

## 七、小结与延伸阅读

沙箱不是"给 Agent 加个笼子"，而是用操作系统强制的边界替代"希望模型不要做坏事"。四个要点：

1. **隔离强度有谱系**：进程级 → 容器 → 用户态内核 → 微虚机，越强越贵。选格子的依据是"这段代码的来源可信度"，不是"任务重不重要"。
2. **安全来自配置，不来自默认值**：挂载、capability、seccomp、no-new-privileges、cgroup、非 root 用户，六件事要一条条显式配置。默认配置是为兼容设计的，不是为安全设计的。
3. **沙箱解决执行面，不管授权面和注入面**：长期凭据、内网可达服务、网页里的话，需要另外的机制。
4. **可观测与沙箱一起上**：没有审计就没有改进，只有配置没有回放能力，事故复盘只能靠猜。

延伸阅读：

- OpenHands — https://github.com/All-Hands-AI/OpenHands
- browser-use — https://github.com/browser-use/browser-use
- Stagehand — https://github.com/browserbase/stagehand
- Claude Code — https://github.com/anthropics/claude-code
- gVisor（Apache-2.0）— https://github.com/google/gvisor
- Firecracker（Apache-2.0）— https://github.com/firecracker-microvm/firecracker
- Kata Containers（Apache-2.0）— https://github.com/kata-containers/kata-containers
- runc（Apache-2.0）— https://github.com/opencontainers/runc
- OpenLLMetry — https://github.com/traceloop/openllmetry
- Langfuse — https://github.com/langfuse/langfuse
- Arize Phoenix — https://github.com/Arize-ai/phoenix
- seccomp(2) 手册页 — https://man7.org/linux/man-pages/man2/seccomp.2.html
- landlock(2) 手册页 — https://man7.org/linux/man-pages/man2/landlock_create_ruleset.2.html

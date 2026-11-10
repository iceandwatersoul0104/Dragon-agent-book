# `_plan/` —— 书稿的架构设计底稿

这个目录是《开源 Agent 架构技术全书》的**可追溯底稿**：规格表是唯一真相源，其余脚本都是它的确定性投影。
读者想复现任何一个数字，都能从这里找到它是怎么来的。

## 规格表（唯一真相源）

| 文件 | 内容 |
| --- | --- |
| `chapters.json` | 第一卷 40 章的规格表：每章的 id / 卷 / 标题 / slug / 目标 / 必覆盖要点 / 涉及项目 / 是否有配套代码 / 代码目录 / 目标字数。**README 目录、各卷参考资料、编委任务、Bot instructions 全部由它派生**，避免多处手抄导致漂移。 |
| `chapters-vol2.json` | 第二卷实践册 14 章规格 + `dragon-agent` 工程的 14 个模块布局。 |
| `volume-refs.json` | 测绘阶段联网核实过的 59 条项目条目（真实仓库 URL + 许可证 + 一句话定位），按卷切分。 |
| `vol2-verified-refs.json` | 第二卷正文外部引用的已核实清单（15 个仓库 + 许可证 + 证据来源）。第二卷的「开源实现对照」一节只允许引用这里或 `volume-refs.json` 里的项目。 |
| `_dragon_contract.md` | `dragon-agent` 14 个模块的接口契约 + 12 条跨模块不变式。**14 章并行写之前先定契约**，编委之间不互相改文件。 |
| `chapter-briefs.json` | 59 条测绘条目按各章的 project 列表匹配后的结果。 |
| `survey-orchestration.json` / `survey-protocol.json` / `survey-memory-eval.json` | 原始测绘输出（从失败的动态工作流运行里抢救出来的），含完整 prompts 与逐项目核实记录。 |

## 体例与终审闸门

| 脚本 | 作用 | 怎么跑 |
| --- | --- | --- |
| `_lint.py` | 体例 lint：七个二级标题逐字一致、篇幅区间、编码（无 BOM / 无 U+FFFD 乱码 / 行尾一致）、章节文件名与 `chapters.json` 一致、卷目录不得混入示例代码、空话与失效条件启发式检查 | `python _lint.py` |
| `_editorial_scan.py` | 终审机检：逐条 HTTP 验证链接、arXiv 编号与官方 API 标题对账、许可证声明与仓库实际对照、跨章大段重复检测、术语变体扫描 | `python _editorial_scan.py` |
| `_check_tables.py` | 附录表格体检：列数一致性、分隔行合法性、空表检查 | `python _check_tables.py` |
| `_link_review.ps1` | 用 PowerShell 复核全部非 GitHub 外部链接（Python 的 TLS 栈对部分域名不通，这一步是补充） | `powershell -File _link_review.ps1` |
| `_lint_vol2.py` | 第二卷闸门：14 章七节结构（**二级标题必须恰为七个，多一个都报**）、篇幅、编码、14 个模块是否齐全、测试是否实跑通过 | `python _lint_vol2.py` |
| `_code_ref_check.py` | 第二卷专有：正文代码块与 `dragon-agent/` 仓库文件**逐字一致**性核对。自动区分「需逐字一致的实现块」/「ASCII 图与伪代码」/「标注为反面教材的反例块」/「非 Python 块」 | `python _code_ref_check.py` |
| `_xcheck_vol2.py` | 第二卷正文的仓库 URL 与许可证 × 已核实清单（附录 B 61 项 + `vol2-verified-refs.json` 15 项）双向交叉核对 | `python _xcheck_vol2.py` |
| `_xscan_vol2.py` | 第二卷跨章扫描：重复段落、术语变体、空话套话、第二卷新术语是否已在附录 C 登记 | `python _xscan_vol2.py` |
| `_ref_chapters.py` | 第二卷正文里「第一卷第 N 章《标题》」的**交叉引用核对**——章号错会让读者翻不到书，标题简称不算错 | `python _ref_chapters.py` |
| `_license_probe.ps1` | 逐个核实任意 GitHub 仓库的许可证（优先取仓库页 sidebar 的 "X license" 标记，取不到则拉 LICENSE 原文首行） | `powershell -File _license_probe.ps1 owner/repo ...` |
| `_license_probe.py` | 同上，Python 版；仓库页抓不动时用 PyPI JSON API 的 `license` / `license_expression` 交叉验证 | `python _license_probe.py import owner/repo ...` |
| `../dragon-agent/run_all.py` | **第二卷的代码验收闸门**：批量实跑 13 个示例脚本 + 全量 unittest，核对每个退出码。与第一卷的 `code/run_all.py` 同构 | `python dragon-agent/run_all.py` |

## 生成器

| 脚本 | 产物 | 说明 |
| --- | --- | --- |
| `_gen_readme.py` | `../README.md` | 从 `chapters.json` 确定性投影出全书目录、体例说明与代码运行方式。 |
| `_append_glossary.py` | `../vol1/appendix/C-术语表.md` | 把第二卷新术语按主题归入附录 C，幂等：已存在的条目跳过。 |
| `_append_index.py` | `../vol1/appendix/D-全书索引.md` | 把第二卷补进附录 D 的三张索引（章节速查 / 项目索引 / 代码目录索引），幂等。 |

附录 B《开源项目速查表》与附录 E《主编终审报告》是**定稿产物**，已入库；生成它们的一次性脚本（附录 B 的三步流水线、附录 E 的汇总器）在交付前已移除。附录 F《第二卷主编终审报告》同理，是手工撰写并随书定稿的。

## 网络访问备忘

本机的 GitHub 访问有三条路，可用性与额度都不同（**2026-11-08 实测，与旧记录相反，以本节为准**）：

- `github.com` 网页路径 —— **可用**。仓库页 sidebar 会渲染成 `License BSD-3-Clause license`，正则取这个标记即可判定许可证；
  `<repo>/raw/<ref>/<file>` 也能取到文件原文，不必绕道 raw 域。
- `raw.githubusercontent.com` —— **被 TLS 中间设备阻断**（连接被意外关闭 / EOF）。第一卷那轮能用，这一轮不通，路径与网络策略会变，别硬试。
- `api.github.com` —— 权威（能拿到 `spdx_id`），但**未认证时每 IP 每小时只有 60 次**，且共享出口 IP 时限流来得很快，批量核查会超额。
- `pypi.org/pypi/<pkg>/json` —— 可用。`info.license` 与 `info.license_expression` 能定许可证，
  是仓库页抓不动时的可靠替代（`structlog` 的双许可就是靠它确认的）。
- **PowerShell 的 .NET TLS 栈比 Python 的 urllib 稳**。同一个 `github.com` 地址，Python 侧间歇性
  `UNEXPECTED_EOF`，PowerShell 侧多数能通。核查脚本优先写 `.ps1`（且必须纯 ASCII）。
- 密集请求会被中间设备切断，单次之间留 3–8 秒间隔。

### git 推送走哪条路（2026-11-10 实测）

**HTTPS 推送不通，SSH 通。**

- `git push` 走 `https://github.com/...` —— **失败**。`schannel: failed to receive handshake` / `server closed abruptly (missing close_notify)`；
  换 `http.sslBackend=openssl`、换 `http.version=HTTP/1.1` 都一样。这与上面的 `raw.githubusercontent.com` 阻断是同一类中间设备问题。
- `git push` 走 **SSH** —— **可用**。`git@github.com`（22 端口）与 `ssh.github.com:443` 都认证成功。
- `gh` 走 Go 的 HTTP 客户端 —— **可用**，`gh api` 正常。必要时可以用 REST API 手工完成
  「blob → tree → commit → ref」四步来代替 `git push`。

**踩过的坑：全局 `~/.gitconfig` 里这条规则写法是错的**——

```ini
[url "git@github.com:443"]
	insteadOf = git@github.com
```

`git@github.com:443` 放在 URL 基位会被解析成「主机 `github.com`、路径 `443`」，
于是所有 `git@github.com:owner/repo.git` 被改写成 `git@github.com:443:owner/repo.git`，
再按 scp 式语法读出来路径就成了 `443:owner/repo.git`，报 `is not a valid repository name`。
**正确写法**是用带 scheme 的完整 URL：

```ini
[url "ssh://git@ssh.github.com:443/"]
	insteadOf = git@github.com:
```

不想动全局配置时，本仓库用显式 URL 绕开该规则即可：

```powershell
git remote set-url origin ssh://git@ssh.github.com:443/iceandwatersoul0104/Dragon-agent-book.git
```

## 已知的坑

- `_gen_readme.py` 与 `_lint.py` 的路径判定都以 `chapters.json` 为准；改动章节文件名后必须重跑这两个脚本。
- 各章编委在审查时会临时建 `_audit*.py` / `_lic*.py` 之类的脚本在 `_plan/` 下，它们**删不掉**（运行时可恢复删除被工作区边界策略拦截），
  需要用单条 `rm -- "<路径>"` 清理。
- Windows 上 PowerShell 用 `-File` 读 `.ps1` 时按系统 ANSI 码页解析，**脚本文件里不要出现中文字面量**，否则解析器会炸。

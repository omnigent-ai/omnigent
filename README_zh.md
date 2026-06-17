<div align="center">

# <img src="https://raw.githubusercontent.com/omnigent-ai/omnigent/main/docs/images/omnigent-logo.svg" alt="" height="38" valign="middle" /> Omnigent

### 适用于所有AI代理的元工具

Omnigent 在 Claude Code、Codex、Cursor、Pi 和您自己编写的代理之上提供了一个通用层：无需重写即可交换或组合工具，通过策略和沙箱控制它们，并从任何设备在同一实时会话中实时协作。

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/omnigent-ai/omnigent/blob/main/LICENSE)
![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](#1-install)

[omnigent.ai](https://omnigent.ai) · **[⬇️ 下载macOS桌面应用](https://omnigent.ai/download/mac)** · [简体中文 README](README.zh-CN.md) 


</div>

<p align="center">
  <img src="https://raw.githubusercontent.com/omnigent-ai/omnigent/main/docs/images/omnigent-hero.png" alt="一个Omnigent编排器及其子代理在一个共享会话中" width="520" />
</p>

---

## 为什么选择Omnigent？

Omnigent让您可以：

- **📱 从任何设备使用代理，包括手机。** 会话跟随您：在终端开始，在浏览器中继续，在手机上继续。消息、子代理、终端和文件保持同步。

- **🤖 监督多个代理。** 在同一会话中同时使用Claude Code、Codex、Pi和自定义代理（在YAML中定义）。让一个代理审查另一个代理的工作，或将任务分配给擅长不同事物的代理。

- **🔌 使用任何模型。** 一方API密钥、Claude/ChatGPT订阅或任何兼容的网关。所有都是一等公民。

- **🤝 协作。** 共享会话，以便队友可以与您的代理聊天并实时观看其工作，在您的机器上共同驱动它，或分叉对话以便他们自己继续。

- **☁️ 在云沙箱中运行代理。** 无需笔记本电脑：在临时[Modal](https://modal.com)、[Daytona](https://www.daytona.io)或[Islo](https://islo.dev)沙箱中运行会话，从CLI启动或由服务器按会话配置（*托管主机*）。

- **🛡️ 管理您的代理。** 创建[策略](#6-用策略管理您的代理)以在风险操作之前暂停等待您的批准，设置支出上限或限制代理可以使用的工具。它们适用于整个服务器、一个代理或单个聊天。

---

## 快速开始

### 1. 安装

一条命令安装Omnigent及其所有依赖：

```bash
curl -fsSL https://raw.githubusercontent.com/omnigent-ai/omnigent/main/scripts/install_oss.sh | sh
```

<details>
<summary>喜欢手动安装？</summary>

Omnigent需要 **Python 3.12+**。安装`omnigent`包：

```bash
uv tool install omnigent        # 或：pip install "omnigent"
```

或使用[Homebrew](https://github.com/omnigent-ai/homebrew-tap)：

```bash
brew install omnigent-ai/tap/omnigent
```

或直接从仓库安装：

```bash
uv tool install -q --python 3.12 git+https://github.com/omnigent-ai/omnigent.git
```

</details>

<details>
<summary>工具链和先决条件（如果安装程序报告缺少工具）</summary>

- **`uv`**（必需）。https://docs.astral.sh/uv/getting-started/installation/
  安装程序会为您设置此工具。
- **`git`**（必需）。
- **Node.js 22 LTS或更高版本**，包含**`npm`**，用于Claude、Codex和Pi编码工具。`omnigent run`会安装您选择的工具CLI。https://docs.npmjs.com/downloading-and-installing-node-js-and-npm
- **`tmux`**，原生`omnigent claude` / `omnigent codex`包装器所需（`brew install tmux` / `apt install tmux`；安装程序会为您安装它）。
- **`bubblewrap`**（`bwrap`），仅限**Linux**。原生`omnigent claude` / `omnigent codex`和`pi`工具在`bwrap` OS沙箱中包装每个代理终端；在Linux上这种隔离是强制性的，因此缺少`bwrap`二进制文件会导致这些终端启动失败（`apt install bubblewrap`；安装程序会为您安装它）。macOS使用内置的`seatbelt`沙箱，无需额外安装。
- **Databricks**（可选）。要使用Databricks工作区作为模型提供商，请使用`databricks`额外选项安装Omnigent：`uv tool install "omnigent[databricks]"` - 或通过引导安装程序传递`... | sh -s -- --extra databricks`。登录工作区还需要[Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install)。

</details>

<details>
<summary>更新到新版本</summary>

当PyPI上有更新版本时，Omnigent会显示一行通知（每个版本一次）指向此处。要更新：

```bash
omni upgrade            # 检测您的安装方式，停止本地服务器，然后运行匹配的升级命令
omni upgrade --check    # 仅报告是否有更新版本可用
```

`omni upgrade`等待进行中的代理会话完成后再停止本地服务器（传递`--force`可立即停止它们）；下一个`omni`命令会在新版本上启动服务器。源代码检出使用`git pull`更新。使用`OMNIGENT_NO_UPDATE_CHECK=1`可静默通知。

检查会查询您配置的软件包索引 - 尊重`UV_INDEX_URL` / `PIP_INDEX_URL`和您的`uv.toml` / `pip.conf`（默认PyPI），因此私有镜像开箱即用；如果需要，可以使用`OMNIGENT_INDEX_URL`覆盖。

</details>

### 2. 启动您的第一个代理

`omnigent`与您一起选择模型并在终端中启动会话。它还会在`http://localhost:6767`启动一个本地Web UI，在浏览器或网络上的手机上显示相同的会话（步骤4）。[桌面应用](https://omnigent.ai/docs/interact/desktop)在原生窗口中包装相同的UI并添加操作系统通知和程序坞徽章 - [为macOS下载它](https://omnigent.ai/download/mac)。

> [!NOTE]
> 安装会在您的PATH上为相同的CLI放置两个名称：`omnigent`和更短的`omni`。它们可以互换使用。

> [!TIP]
> 首次运行时，Omnigent会从您的环境中获取已有的模型凭据（`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`，或您已登录的`claude` / `codex` CLI）并将其作为默认值提供。

```bash
omnigent
```

或启动特定的代理运行时，或您自己的代理：

```bash
omnigent claude                      # Claude Code，在您的团队可以加入的会话中
omnigent codex                       # Codex
omnigent run path/to/agent.yaml      # 您自己的代理（参见"编写您自己的代理"）
```

#### 🐙 Polly和🟠🔵 Debby

仓库中有两个示例代理，它们是很好的第一个会话：

```bash
omnigent run examples/polly/
omnigent run examples/debby/

# 在不同工具上运行编排器（子代理保持自己的工具）：
omnigent run examples/polly/ --harness pi
omnigent run examples/debby/ --harness openai-agents
omnigent run examples/polly/ --harness cursor  # Cursor CLI（需要cursor-agent + CURSOR_API_KEY）
```

**🐙 Polly**是一个多代理编码编排器，她自己不编写代码。她是技术负责人：她计划，将工作分配给编码子代理（Claude Code、Codex或Pi）在并行git工作树中，然后将每个差异路由给与编写它的代理不同供应商的审查者。您合并。

**🟠🔵 Debby**是一个有两个脑袋的头脑风暴伙伴，一个是Claude，一个是GPT。您问的每个问题都会同时发送给两个脑袋，她将两个答案并排显示。输入`/debbate`，两个脑袋会相互批评几轮，然后达成一致。（她需要Claude和OpenAI凭据；参见步骤3。）

**更喜欢浏览器？** 启动服务器并将您的机器注册为主机：

```bash
omnigent server start   # 在后台启动本地服务器和Web UI
omnigent host           # （单独的终端）将此机器注册为主机
```

在Web UI中，点击**新聊天**，选择您的机器，然后开始。使用`omnigent server status`检查状态；使用`omnigent stop`停止所有内容。

### 3. 选择和切换模型

```bash
omnigent setup
```

添加凭据、设置默认值或删除，按代理分组。Omnigent支持四种凭据：

| | 类型 | 说明 |
|---|---|---|
| 🔑 | **API密钥** | 用于Anthropic、OpenAI和类似提供商的一方供应商密钥 |
| 🎟️ | **订阅** | Claude Pro/Max或ChatGPT计划，通过官方`claude` / `codex` CLI |
| 🌐 | **网关** | 任何OpenAI或Anthropic兼容的`base_url`和密钥（OpenRouter、LiteLLM、Ollama、vLLM、Azure） |
| 🧱 | **Databricks** | Databricks工作区配置文件（需要`databricks`额外选项） |

默认值按代理设置，因此Claude默认值和Codex默认值可以共存。您也可以在会话中使用`/model`命令切换模型。

<details>
<summary>网关基础URL（OpenRouter、Ollama）</summary>

当您添加**网关**凭据时，`omnigent setup`会询问基础URL和密钥。基础URL取决于您将其指向哪个代理：

| 提供商 | 用于 | 基础URL | 密钥 |
|---|---|---|---|
| **OpenRouter** | Claude Code | `https://openrouter.ai/api` | 您的OpenRouter密钥（`sk-or-…`） |
| **OpenRouter** | Codex / OpenAI代理 | `https://openrouter.ai/api/v1` | 您的OpenRouter密钥（`sk-or-…`） |
| **Ollama**（本地） | Codex / OpenAI代理 | `http://localhost:11434/v1` | 任何值（Ollama忽略它） |

对于Claude Code，指向OpenRouter的Anthropic兼容端点（`…/api`，**不是**`…/api/v1`）。对于Codex和OpenAI代理工具，使用OpenAI兼容的`…/api/v1`。

</details>

### 4. 部署服务器（并从手机📱使用它）

在具有稳定URL的服务器上运行Omnigent（[`deploy/README.md`](https://github.com/omnigent-ai/omnigent/blob/main/deploy/README.md)是完整指南），您的会话将可以从任何地方访问，包括您的手机。Web UI专为移动设备设计，因此您可以在笔记本电脑同步的情况下获得相同的聊天、子代理、终端和文件。

一个`docker compose up`就可以在您拥有的任何主机（VPS、家庭服务器）上运行服务器；Render一键部署；Fly.io、Railway、Hugging Face Spaces和Modal也被涵盖。服务器还可以为每个会话配置云沙箱（*托管主机*），因此无需笔记本电脑保持在线。完整的目标菜单、数据库选项和沙箱设置位于[`deploy/README.md`](https://github.com/omnigent-ai/omnigent/blob/main/deploy/README.md)。

服务器启动后，登录并将您的笔记本电脑注册为主机：

```bash
omnigent login https://your-host    # 登录一次；run / attach / host重用令牌
omnigent host  https://your-host    # 新会话现在可以在此机器上运行
```

> [!TIP]
> 在您自己的网络上，您不需要部署。在手机上打开您机器的局域网地址（例如`http://192.168.x.x:6767`）。

### 5. 与团队协作

Omnigent支持**多用户帐户**，由一个环境变量控制：

```bash
OMNIGENT_AUTH_ENABLED=1 omnigent server start
```

**[步骤4](#4-部署服务器并从手机使用它)中的Docker部署会为您启用它**（`OMNIGENT_AUTH_ENABLED`在那里默认为`1`）。

#### 邀请您的队友

打开Web UI（本地`http://localhost:6767`，或您的主机URL），以`admin`身份登录；首次运行会打印密码并将其保存在本地。然后打开**管理 → 成员 → 邀请**创建一次性邀请链接，无需电子邮件服务器。发送它；您的队友打开它，设置密码，然后加入。注册仅限邀请。

<!-- TODO: 管理 → 成员 → 邀请的截图。 -->

> [!NOTE]
> 队友需要能够访问服务器。本地服务器仅在网络上可访问；对于网络外的任何人，请部署始终在线的主机（参见[步骤4](#4-部署服务器并从手机使用它)）。

#### 一起编码

- **共享实时会话。** 在Web UI中点击**分享**并发送链接；队友观看您的代理工作并实时与其聊天。
- **共同驱动。** 队友共同附加到您正在运行的会话；他们的消息在**您的**机器上执行。非常适合结对编程或在调查过程中将键盘交给领域专家。

  ```bash
  omnigent attach <session_id>
  ```

- **分叉。** 将对话克隆到您自己的机器上，并从分叉点独立继续。

  ```bash
  omnigent run --fork <session_id>
  ```

> [!TIP]
> 希望您的团队使用他们已有的登录名（**Google、GitHub、Okta、Microsoft**）登录？在您的部署服务器上设置`OMNIGENT_OIDC_ISSUER`以及客户端ID和密钥，然后重启。完整演练、域名允许列表和仅代理`header`认证模式在[`deploy/README.md#auth`](https://github.com/omnigent-ai/omnigent/blob/main/deploy/README.md#auth)中介绍。

### 6. 用策略管理您的代理

**策略**决定代理可以做什么：运行shell命令、编辑文件、花费令牌。它们检查每个操作并允许、阻止或暂停以先询问您。

- **在Web UI中**：打开会话的信息面板以浏览可用策略并开启或关闭它们。
- **在聊天中**：询问。*"添加一个策略，在运行shell命令之前询问我。"* 代理会为您设置。

想要应用于所有人或特定代理的默认值？在服务器配置或代理的YAML中定义它们：

```yaml
policies:
  approve_shell:
    type: function
    handler: omnigent.policies.builtins.safety.ask_on_os_tools   # 在shell / 文件写入之前询问
  cap_calls:
    type: function
    handler: omnigent.policies.builtins.safety.max_tool_calls_per_session
    factory_params:
      limit: 50                    # 限制一个会话可以调用的工具数量
  budget:
    type: function
    handler: omnigent.policies.builtins.cost.cost_budget
    factory_params:
      max_cost_usd: 5.00           # 硬性支出上限...
      ask_thresholds_usd: [3.00]   # ...并在途中提供软警告
```

策略在三个级别上叠加：**服务器范围**（管理员）、**每个代理**（开发者）和**每个会话**（您），更严格的会话规则首先检查。支出上限和访问限制作为内置功能提供。

有关完整目录和信任模型，请参阅[策略指南](https://github.com/omnigent-ai/omnigent/blob/main/docs/POLICIES.md)。

---

## 编写您自己的代理

代理是一个简短的YAML文件：您的提示、您的工具，以及可选的辅助子代理，主管可以委派给它们。您不必手动编写：代理可以构建代理，因此在任何Omnigent聊天中描述您想要的代理，它会为您编写文件。

```yaml
name: my_agent
prompt: You are a helpful data analyst.

executor:
  harness: claude-sdk          # 或：codex, codex-native, claude-native, cursor, openai-agents, pi, antigravity

tools:
  # 一个本地Python函数（从签名自动生成模式）
  word_count:
    type: function
    callable: mypackage.mymodule.word_count

  # 主管可以委派的子代理
  researcher:
    type: agent
    prompt: Search for relevant information and summarize it.
    tools:
      word_count: inherit
```

使用以下命令运行：

```bash
omnigent run path/to/my_agent.yaml
```

同一个文件可以声明子代理和审查者。更完整的示例，请参阅Polly在[`examples/polly/`](https://github.com/omnigent-ai/omnigent/tree/main/examples/polly/)，以及[代理YAML规范](https://github.com/omnigent-ai/omnigent/blob/main/docs/AGENT_YAML_SPEC.md)获取完整架构。

---

## 贡献

欢迎贡献。请参阅[CONTRIBUTING.md](https://github.com/omnigent-ai/omnigent/blob/main/CONTRIBUTING.md)了解如何设置您的环境、运行检查和提交拉取请求。
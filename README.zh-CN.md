<div align="center">

# <img src="https://raw.githubusercontent.com/omnigent-ai/omnigent/main/docs/images/omnigent-logo.svg" alt="" height="38" valign="middle" /> Omnigent

### 适用于所有 AI 代理的元工具

Omnigent 在 Claude Code、Codex、Cursor、Pi 和您自己编写的智能体之上提供了一个通用层：无需重写即可更换 Harness，并通过策略和沙箱管理，也可从任何设备在同一会话中实时协作。

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/omnigent-ai/omnigent/blob/main/LICENSE)
![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](#1-install)

[omnigent.ai](https://omnigent.ai) · **[⬇️ 下载 macOS 桌面应用](https://omnigent.ai/download/mac)** · [English README](README.md)

</div>

<p align="center">
  <img src="https://raw.githubusercontent.com/omnigent-ai/omnigent/main/docs/images/omnigent-hero.png" alt="Omnigent 编排器及其子智能体在一个共享会话中" width="520" />
</p>

---

## 为什么选择 Omnigent？

Omnigent 让您可以：

- **📱 从任何设备（包括手机）使用智能体。** 会话可在不同设备间无缝衔接：在终端开始，在浏览器继续，也能随时切换到手机。消息、子智能体、终端和文件都会自动同步。

- **🤖 监督多个代理。** 在同一会话中同时使用 Claude Code、Codex、Pi 和自定义代理（在 YAML 中定义）。让一个代理审查另一个代理的工作，或将任务分配给擅长不同领域的多个代理。

- **🔌 使用任何模型。** 第一方 API 密钥、Claude/ChatGPT 订阅或任何兼容的网关。全部都是一等公民。

- **🤝 协作。** 共享会话，让队友可以与您的代理聊天并实时观看其工作，共同驱动您机器上的代理，或分叉对话以继续独立工作。

- **☁️ 在云沙箱中运行代理。** 无需笔记本电脑：在可丢弃的 [Modal](https://modal.com)、[Daytona](https://www.daytona.io) 或 [Islo](https://islo.dev) 沙箱中运行会话，从 CLI 启动或由服务器按会话配置（*托管主机*）。

- **🛡️ 管理您的代理。** 创建[策略](#6-使用策略管理您的代理)，以便在执行危险操作前暂停等待您的批准、设置支出上限或限制代理可以使用的工具。它们适用于整个服务器、单个代理或单个聊天。

---

## 快速开始

### 1. 安装

一条命令即可安装 Omnigent 及其所有依赖：

```bash
curl -fsSL https://raw.githubusercontent.com/omnigent-ai/omnigent/main/scripts/install_oss.sh | sh
```

<details>
<summary>更喜欢手动安装？</summary>

Omnigent 需要 **Python 3.12+**。安装 `omnigent` 包：

```bash
uv tool install omnigent        # 或：pip install "omnigent"
```

或使用 [Homebrew](https://github.com/omnigent-ai/homebrew-tap)：

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
  安装程序会为您提供设置选项。
- **`git`**（必需）。
- **Node.js 22 LTS 或更新版本**及 **`npm`**，用于 Claude、Codex 和 Pi 编码工具。`omnigent run` 会安装您选择的工具 CLI。
  https://docs.npmjs.com/downloading-and-installing-node-js-and-npm
- **`tmux`**，原生 `omnigent claude` / `omnigent codex` 包装器所需（`brew install tmux` / `apt install tmux`；安装程序会为您提供安装选项）。
- **`bubblewrap`**（`bwrap`），仅限 **Linux**。原生 `omnigent claude` / `omnigent codex` 和 `pi` 工具使用 `bwrap` OS 沙箱包装每个代理终端；在 Linux 上此隔离是必需的，因此缺少 `bwrap` 二进制文件会导致这些终端无法启动（`apt install bubblewrap`；安装程序会为您提供安装选项）。macOS 使用内置的 `seatbelt` 沙箱，无需额外安装。
- **Databricks**（可选）。要使用 Databricks 工作区作为模型提供商，请使用 `databricks` 扩展安装 Omnigent：`uv tool install "omnigent[databricks]"` — 或将其传递给引导安装程序：`... | sh -s -- --extra databricks`。登录工作区还需要 [Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install)。

</details>

<details>
<summary>升级到新版本</summary>

当 PyPI 上有更新的版本时，Omnigent 会显示一行通知（每个版本一次）并指向此处。要升级：

```bash
omni upgrade            # 检测您的安装方式，停止本地服务器，然后运行相应的升级命令
omni upgrade --check    # 仅报告是否有更新的版本可用
```

`omni upgrade` 会等待进行中的代理会话完成后再停止本地服务器（使用 `--force` 可立即停止）；下一个 `omni` 命令会在新版本上启动服务器。源代码检出使用 `git pull` 进行更新。使用 `OMNIGENT_NO_UPDATE_CHECK=1` 可以静默此通知。

检查会查询您配置的软件包索引 — 遵循 `UV_INDEX_URL` / `PIP_INDEX_URL` 和您的 `uv.toml` / `pip.conf`（默认为 PyPI），因此私有镜像可以开箱即用；如有需要，可使用 `OMNIGENT_INDEX_URL` 进行覆盖。

</details>

### 2. 启动您的第一个代理

`omnigent` 会与您一起选择模型并在终端中启动会话。它还会在 `http://localhost:6767` 启动本地 Web UI，在浏览器中显示相同的会话，或在网络上的手机中显示（步骤 4）。[桌面应用](https://omnigent.ai/docs/interact/desktop)将相同的 UI 包装在原生窗口中，并添加操作系统通知和 Dock 徽章 — [下载 macOS 版本](https://omnigent.ai/download/mac)。

> [!NOTE]
> 安装会在您的 PATH 上放置两个相同 CLI 的名称：`omnigent` 和更短的 `omni`。它们可以互换使用。

> [!TIP]
> 首次运行时，Omnigent 会检测您环境中已有的模型凭据（`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`，或您已登录的 `claude` / `codex` CLI），并将其作为默认选项提供。

```bash
omnigent
```

或启动特定的代理运行时，或您自己的代理：

```bash
omnigent claude                      # Claude Code，在团队可以加入的会话中
omnigent codex                       # Codex
omnigent run path/to/agent.yaml      # 您自己的代理（参见"编写您自己的代理"）
```

#### 🐙 Polly 和 🟠🔵 Debby

仓库附带两个示例代理，它们是很好的第一个会话：

```bash
omnigent run examples/polly/
omnigent run examples/debby/

# 在不同的工具上运行编排器（子代理保持自己的）：
omnigent run examples/polly/ --harness pi
omnigent run examples/debby/ --harness openai-agents
omnigent run examples/polly/ --harness cursor  # Cursor CLI（需要 cursor-agent + CURSOR_API_KEY）
```

**🐙 Polly** 是一个多代理编码编排器，她自己不写代码。她是技术负责人：她规划工作，将任务并行委托给编码子代理（Claude Code、Codex 或 Pi），然后将每个差异路由给与编写它的代理不同供应商的审查者。您进行合并。

**🟠🔵 Debby** 是一个有两个头脑的头脑风暴伙伴，一个是 Claude，一个是 GPT。您提出的每个问题都会发送给两个头脑，她会将两个答案并排呈现。输入 `/debbate`，两个头脑会在几轮后批评对方，然后达成一致。（她需要 Claude 和 OpenAI 凭据；参见步骤 3。）

**更喜欢浏览器？** 启动服务器并将您的机器注册为主机：

```bash
omnigent server start   # 在后台启动本地服务器和 Web UI
omnigent host           # （单独终端）将此机器注册为主机
```

在 Web UI 中，点击 **New Chat**，选择您的机器，然后开始。使用 `omnigent server status` 检查状态；使用 `omnigent stop` 停止所有内容。

### 3. 选择和切换模型

```bash
omnigent setup
```

添加凭据、设置默认值或删除凭据，按代理分组。Omnigent 支持四种凭据：

| | 类型 | 说明 |
|---|---|---|
| 🔑 | **API 密钥** | 用于 Anthropic、OpenAI 和类似提供商的第一方供应商密钥 |
| 🎟️ | **订阅** | Claude Pro/Max 或 ChatGPT 计划，通过官方 `claude` / `codex` CLI |
| 🌐 | **网关** | 任何 OpenAI 或 Anthropic 兼容的 `base_url` 和密钥（OpenRouter、LiteLLM、Ollama、vLLM、Azure） |
| 🧱 | **Databricks** | Databricks 工作区配置文件（需要 `databricks` 扩展） |

默认值按代理设置，因此 Claude 默认值和 Codex 默认值可以共存。您也可以在会话中途使用 `/model` 命令切换模型。

<details>
<summary>网关基础 URL（OpenRouter、Ollama）</summary>

添加 **Gateway** 凭据时，`omnigent setup` 会要求输入基础 URL 和密钥。基础 URL 取决于您指向的代理：

| 提供商 | 用于 | 基础 URL | 密钥 |
|---|---|---|---|
| **OpenRouter** | Claude Code | `https://openrouter.ai/api` | 您的 OpenRouter 密钥（`sk-or-…`） |
| **OpenRouter** | Codex / OpenAI 代理 | `https://openrouter.ai/api/v1` | 您的 OpenRouter 密钥（`sk-or-…`） |
| **Ollama**（本地） | Codex / OpenAI 代理 | `http://localhost:11434/v1` | 任意值（Ollama 会忽略它） |

对于 Claude Code，请指向 OpenRouter 的 Anthropic 兼容端点（`…/api`，**不是** `…/api/v1`）。对于 Codex 和 OpenAI 代理工具，请使用 OpenAI 兼容的 `…/api/v1`。

</details>

### 4. 部署服务器（并从手机使用📱）

在具有稳定 URL 的服务器上运行 Omnigent（[`deploy/README.md`](https://github.com/omnigent-ai/omnigent/blob/main/deploy/README.md) 是完整指南），您的会话将可以从任何地方访问，包括您的手机。Web UI 专为移动端构建，因此您可以在笔记本电脑同步的情况下获得相同的聊天、子代理、终端和文件。

一个 `docker compose up` 即可在您拥有的任何主机（VPS、家庭服务器）上运行服务器；Render 一键部署；Fly.io、Railway、Hugging Face Spaces 和 Modal 也都支持。服务器还可以为每个会话配置云沙箱（*托管主机*），因此无需保持笔记本电脑在线。完整的目标列表、数据库选项和沙箱设置请参阅 [`deploy/README.md`](https://github.com/omnigent-ai/omnigent/blob/main/deploy/README.md)。

服务器启动后，登录并将您的笔记本电脑注册为主机：

```bash
omnigent login https://your-host    # 登录一次；run / attach / host 会重用令牌
omnigent host  https://your-host    # 新会话现在可以在此机器上运行
```

> [!TIP]
> 在自己的网络中，您不需要部署。在手机上打开您机器的局域网地址（例如 `http://192.168.x.x:6767`）。

### 5. 与团队协作

Omnigent 支持**多用户帐户**，由一个环境变量控制：

```bash
OMNIGENT_AUTH_ENABLED=1 omnigent server start
```

**[步骤 4](#4-部署服务器并从手机使用) 中的 Docker 部署会为您开启此功能**（`OMNIGENT_AUTH_ENABLED` 在那里默认为 `1`）。

#### 邀请您的队友

打开 Web UI（本地 `http://localhost:6767`，或您的主机 URL），以 `admin` 身份登录；首次运行会打印密码并在本地保存。然后打开 **Admin → Members → Invite** 创建一次性邀请链接，无需电子邮件服务器。发送给您的队友；您的队友打开链接，设置密码，然后就可以加入了。注册仅限邀请制。

<!-- TODO: Admin → Members → Invite 截图。 -->

> [!NOTE]
> 队友需要能够访问服务器。本地服务器仅在您的网络上可访问；对于网络外的任何人，请部署始终在线的主机（参见[步骤 4](#4-部署服务器并从手机使用)）。

#### 一起编码

- **共享实时会话。** 在 Web UI 中点击 **Share** 并发送链接；队友可以观看您的代理工作并与其实时聊天。
- **共同驱动。** 队友共同附加到您正在运行的会话；他们的消息在**您的**机器上执行。非常适合结对编程或在调查过程中将键盘交给领域专家。

  ```bash
  omnigent attach <session_id>
  ```

- **分叉。** 将对话克隆到您自己的机器上，并从分叉点独立继续。

  ```bash
  omnigent run --fork <session_id>
  ```

> [!TIP]
> 希望您的团队使用已有的登录方式（**Google、GitHub、Okta、Microsoft**）？在部署的服务器上设置 `OMNIGENT_OIDC_ISSUER` 以及客户端 ID 和密钥，然后重新启动。完整的演练、域名允许列表和仅代理的 `header` 认证模式在 [`deploy/README.md#auth`](https://github.com/omnigent-ai/omnigent/blob/main/deploy/README.md#auth) 中有介绍。

### 6. 使用策略管理您的代理

**策略**决定代理可以做什么：运行 shell 命令、编辑文件、消耗 token。它们检查每个操作，然后允许、阻止或暂停以先询问您。

- **在 Web UI 中**：打开会话的信息面板，浏览可用的策略并切换它们的开关。
- **在聊天中**：直接询问。*"添加一个策略，在运行 shell 命令前先询问我。"* 代理会为您设置。

想要适用于所有人或特定代理的默认值？在服务器配置或代理的 YAML 中定义它们：

```yaml
policies:
  approve_shell:
    type: function
    handler: omnigent.policies.builtins.safety.ask_on_os_tools   # 在 shell / 文件写入前询问
  cap_calls:
    type: function
    handler: omnigent.policies.builtins.safety.max_tool_calls_per_session
    factory_params:
      limit: 50                    # 限制一个会话可以调用的工具数量
  budget:
    type: function
    handler: omnigent.policies.builtins.cost.cost_budget
    factory_params:
      max_cost_usd: 5.00           # 硬支出上限...
      ask_thresholds_usd: [3.00]   # ...在过程中发出软警告
```

策略在三个层级上叠加：**服务器范围**（管理员）、**每个代理**（开发者）和**每个会话**（您），更严格的会话规则会首先检查。支出上限和访问限制作为内置功能提供。

完整的目录和信任模型请参阅[策略指南](https://github.com/omnigent-ai/omnigent/blob/main/docs/POLICIES.md)。

---

## 编写您自己的代理

代理是一个简短的 YAML 文件：您的提示、您的工具，以及可选的辅助子代理（主管可以委派任务）。您不必手动编写：代理可以构建代理，因此在任何 Omnigent 聊天中描述您想要的代理，它会为您编写文件。

```yaml
name: my_agent
prompt: You are a helpful data analyst.

executor:
  harness: claude-sdk          # 或：codex, codex-native, claude-native, cursor, openai-agents, pi, antigravity

tools:
  # 本地 Python 函数（从签名自动生成 schema）
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

运行它：

```bash
omnigent run path/to/my_agent.yaml
```

同一个文件可以声明子代理和审查者。更完整的示例请参阅 [`examples/polly/`](https://github.com/omnigent-ai/omnigent/tree/main/examples/polly/) 中的 Polly，完整 schema 请参阅 [Agent YAML 规范](https://github.com/omnigent-ai/omnigent/blob/main/docs/AGENT_YAML_SPEC.md)。

---

## 贡献

欢迎贡献。请参阅 [CONTRIBUTING.md](https://github.com/omnigent-ai/omnigent/blob/main/CONTRIBUTING.md) 了解如何设置环境、运行检查和提交拉取请求。

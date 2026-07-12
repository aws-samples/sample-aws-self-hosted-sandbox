"use client";

import { ArchDiagram } from "@/components/ArchDiagram";

/**
 * 方案讲解 PPT —— 解决方案架构师(SA)视角,面向客户。
 * 叙事融合了《Agent 专用沙箱环境 · AWS 实践(客户版)》的框架:
 *   痛点建立共鸣 → 技术基础与方案对比建立可信度 → 落脚到自建方案,逐条映射"痛点→解法",用真机数字与成本模型收束。
 * 事实来源:本仓库 README/docs 真机实测 + 两份深度调研(官方一手来源)。
 * 谨慎表述的边界(AgentCore 底层 FC 属推断、快照数字用真机实测、JuiceFS 为可选路径、
 * Spot 自动疏散默认 DRY-RUN 等)均在注脚标注,避免过度宣称。
 */

// 星级小组件:实心/空心
function Stars({ n }: { n: number }) {
  return (
    <span className="stars">
      <span className="on">{"★".repeat(n)}</span>
      <span className="off">{"★".repeat(5 - n)}</span>
    </span>
  );
}

export const SLIDES: { id: string; render: () => React.ReactNode }[] = [
  // ============ 0. 封面 ============
  {
    id: "cover",
    render: () => (
      <div className="slide slide-cover">
        <div className="cover-badge">AGENTIC AI INFRASTRUCTURE · 沙箱方案</div>
        <h1>
          在 AWS 上自建
          <br />
          <span className="grad">Firecracker microVM 沙箱平台</span>
        </h1>
        <p>
          为 AI Agent 的不可信代码执行提供裸机级隔离、快照挂起、超卖与 24×7 常驻 ——
          成本更低、完全可控、数据留在您自己的 AWS 账户内。
        </p>
        <div className="cover-chips">
          <span className="chip chip-strong">硬件级隔离</span>
          <span className="chip">快照 Suspend/Resume</span>
          <span className="chip">超卖 Overcommit</span>
          <span className="chip">24×7 常驻</span>
          <span className="chip">数据主权</span>
        </div>
        <div className="slide-foot" style={{ maxWidth: 640, margin: "34px auto 0" }}>
          用 ← / → 或空格翻页 · 底部圆点可跳页 · 面向客户的方案讲解
        </div>
      </div>
    ),
  },

  // ============ 1. 范式转变 ============
  {
    id: "paradigm",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">① 背景与必要性</span>
        <h2 className="slide-h">从"对话式 AI"到"行动式 AI"</h2>
        <p className="slide-lead">
          Agent 不再只是处理自然语言对话 —— 它会<b>主动执行代码、操作应用、分析数据</b>。这正是它必须运行在隔离沙箱中的根本原因。
        </p>
        <div className="paradigm">
          <div className="p-box from">
            <div className="p-t">💬 对话式 AI</div>
            <div className="p-s">Conversational AI · 生成文本</div>
          </div>
          <div className="p-arrow">➜</div>
          <div className="p-box to">
            <div className="p-t">⚙️ 行动式 AI</div>
            <div className="p-s">Action-oriented AI · 执行真实操作</div>
          </div>
        </div>
        <div className="deck-cards c3">
          <div className="deck-card">
            <span className="dc-icon">🧠</span>
            <h3>自主规划</h3>
            <p>理解意图、拆解任务、制定并调整执行计划。</p>
          </div>
          <div className="deck-card">
            <span className="dc-icon">🛠️</span>
            <h3>调用工具</h3>
            <p>写代码、跑命令、访问数据源与第三方 API。</p>
          </div>
          <div className="deck-card">
            <span className="dc-icon">🔁</span>
            <h3>多步迭代</h3>
            <p>长程任务多次执行、分支探索、断点续跑。</p>
          </div>
        </div>
        <div className="slide-foot">
          一旦 AI 开始"动手做事",运行环境的<b>安全边界</b>就从"锦上添花"变成了"不可绕过的地基"。
        </div>
      </div>
    ),
  },

  // ============ 2. 四大核心挑战(痛点) ============
  {
    id: "challenges",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">① 背景与必要性</span>
        <h2 className="slide-h">Agent 落地绕不开的四大挑战</h2>
        <p className="slide-lead">
          这四条是我们和客户交流时反复听到的真实痛点 —— 也是任何沙箱方案必须正面回答的问题。
        </p>
        <div className="deck-cards c4">
          <div className="deck-card bad">
            <span className="dc-icon">⚠️</span>
            <h3>执行外部生成代码</h3>
            <p>LLM 生成的代码<b>不可信</b>,可能含有害逻辑,必须假设它会尝试逃逸。</p>
          </div>
          <div className="deck-card bad">
            <span className="dc-icon">🌐</span>
            <h3>访问第三方数据</h3>
            <p>接入外部数据源与 API,<b>显著放大攻击面</b>与数据泄露风险。</p>
          </div>
          <div className="deck-card bad">
            <span className="dc-icon">👥</span>
            <h3>多租户并发</h3>
            <p>不同用户任务必须<b>互不干扰、故障隔离</b>,一个崩溃不能拖垮邻居。</p>
          </div>
          <div className="deck-card bad">
            <span className="dc-icon">📦</span>
            <h3>高密度部署</h3>
            <p>规模化后要在<b>安全、性能与成本</b>之间找到最优平衡点。</p>
          </div>
        </div>
        <div className="slide-foot">
          换句话说:既要<b>强隔离</b>(安全),又要<b>高密度 + 低成本</b>(规模化)—— 这两者天然矛盾,方案的价值就在于同时兼顾。
        </div>
      </div>
    ),
  },

  // ============ 3. 两大应用场景 ============
  {
    id: "scenarios",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">① 背景与必要性</span>
        <h2 className="slide-h">两大核心应用场景</h2>
        <p className="slide-lead">
          客户的 Agent 负载几乎都落在这两类,它们对沙箱的诉求略有不同,但都要求真实、隔离的执行环境。
        </p>
        <div className="scene-grid">
          <div className="scene">
            <div className="sc-tag">2.1 · Code Execution</div>
            <h3>代码执行环境</h3>
            <p>
              <b>典型:企业数据分析 Agent</b> —— 分析师上传 1GB 销售数据,自然语言下指令:"分析趋势、找最佳品类、生成可视化报告"。
            </p>
            <p>
              Agent 解析意图 → LLM 生成读取/处理/分析代码 → 多次拉起沙箱执行 → 输出含图表的完整报告。
            </p>
            <p className="dim" style={{ marginTop: 10 }}>
              运行时需求:Python Runtime · VSCode Server · 命令行与高级代码解析。
            </p>
          </div>
          <div className="scene">
            <div className="sc-tag">2.2 · Visual Operation</div>
            <h3>可视化操作环境</h3>
            <p>
              <b>Computer Use</b> —— AI 像人一样操作 GUI:点击按钮、输入文本、拖拽文件。
            </p>
            <p>
              <b>Browser Use</b> —— 重要子场景:网页浏览、表单填写、数据抓取等浏览器自动化。
            </p>
            <p className="dim" style={{ marginTop: 10 }}>
              这类交互式、长会话负载,尤其需要<b>可暂停/恢复</b>与<b>长驻</b>能力。
            </p>
          </div>
        </div>
        <div className="slide-foot">
          两类场景的共同底线:任意代码 / 任意操作 + 真实系统环境 + 强隔离 —— 决定了底座必须是 microVM,而非受限的托管运行时。
        </div>
      </div>
    ),
  },

  // ============ 4. 四大技术需求 ============
  {
    id: "requirements",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">② 技术需求</span>
        <h2 className="slide-h">好的沙箱平台:四大技术需求</h2>
        <p className="slide-lead">
          从痛点出发,一个真正可用的沙箱平台要在这四个维度都及格 —— 这也是我们评估所有方案的标尺。
        </p>
        <ul className="req-list">
          <li>
            <span className="req-no">3.1</span>
            <div>
              <div className="req-b">🧰 易用接入 · Easy Access</div>
              <div className="req-d">简单的 SDK / API,屏蔽底层部署与路由复杂度;一键启动与发布,不阻碍业务迭代。</div>
            </div>
          </li>
          <li>
            <span className="req-no">3.2</span>
            <div>
              <div className="req-b">🎛️ 简化管理 · Simplified Management</div>
              <div className="req-d">"先建模板,再拉运行时":用模板 ID 即可起环境;弹性伸缩、并行运行、自动负载均衡。</div>
            </div>
          </li>
          <li>
            <span className="req-no">3.3</span>
            <div>
              <div className="req-b">⏱ 全生命周期管理 · Lifecycle</div>
              <div className="req-d">毫秒级启停销毁;持久化临时数据;<b>自动快照、恢复、暂停/恢复</b>,支撑多阶段推理与多分支探索。</div>
            </div>
          </li>
          <li>
            <span className="req-no">3.4</span>
            <div>
              <div className="req-b">🔒 完整安全保障 · Security</div>
              <div className="req-d"><b>硬件级隔离</b> + 系统调用最小化 + 网络/文件系统细粒度权限;真正的故障边界隔离与高密度部署。</div>
            </div>
          </li>
        </ul>
      </div>
    ),
  },

  // ============ 5. 多层隔离架构 ============
  {
    id: "security",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">③ 安全基础</span>
        <h2 className="slide-h">安全:遵循最小权限的多层隔离</h2>
        <p className="slide-lead">
          Agent 只应访问完成任务所需的最小资源。我们把隔离做成四层纵深,而不是押注单一边界。
        </p>
        <div className="deck-cards c4">
          <div className="deck-card accent">
            <span className="dc-icon">🖥️</span>
            <h3>1 · 虚拟化隔离</h3>
            <p>Firecracker microVM 提供硬件级隔离,每个沙箱独立 guest 内核,代码无法突破边界。</p>
          </div>
          <div className="deck-card accent">
            <span className="dc-icon">🌐</span>
            <h3>2 · 网络隔离</h3>
            <p>每沙箱独立网络槽位与 IP 空间,网络池管理避免冲突;可从完全断网到细粒度放行。</p>
          </div>
          <div className="deck-card accent">
            <span className="dc-icon">🗂️</span>
            <h3>3 · 文件系统隔离</h3>
            <p>基于模板创建独立根文件系统防篡改;临时数据执行后自动清理,不泄露不残留。</p>
          </div>
          <div className="deck-card accent">
            <span className="dc-icon">📊</span>
            <h3>4 · 资源限制与监控</h3>
            <p>严格限 CPU/内存防耗尽攻击;可设最大生命周期;周期性健康检查(如每 30s)。</p>
          </div>
        </div>
        <div className="slide-foot">
          第 1 层是地基:如果虚拟化边界不够硬,后面三层都只是"深度不足的补丁"。所以先看虚拟化技术怎么选。
        </div>
      </div>
    ),
  },

  // ============ 6. 虚拟化对比(星级) ============
  {
    id: "virt",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">③ 安全基础</span>
        <h2 className="slide-h">VM vs 容器 vs Firecracker</h2>
        <p className="slide-lead">
          传统虚拟机安全但慢又重;容器快又省但共享内核、隔离弱。Firecracker 把两者的优点合到了一起。
        </p>
        <div className="deck-table-wrap">
          <table className="deck-table">
            <thead>
              <tr>
                <th>能力维度</th>
                <th>虚拟机 VM</th>
                <th>容器 Containers</th>
                <th className="col-us">Firecracker</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>安全隔离</td>
                <td><Stars n={5} /></td>
                <td><Stars n={2} /></td>
                <td className="col-us"><Stars n={5} /></td>
              </tr>
              <tr>
                <td>启动速度 ¹</td>
                <td><Stars n={1} /></td>
                <td><Stars n={5} /></td>
                <td className="col-us"><Stars n={5} /></td>
              </tr>
              <tr>
                <td>资源效率</td>
                <td><Stars n={1} /></td>
                <td><Stars n={5} /></td>
                <td className="col-us"><Stars n={4} /></td>
              </tr>
              <tr>
                <td>灵活性 ²</td>
                <td><Stars n={5} /></td>
                <td><Stars n={4} /></td>
                <td className="col-us"><Stars n={2} /></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div className="deck-cards c3" style={{ marginTop: 16 }}>
          <div className="deck-metric">
            <div className="m-val">&lt;125ms</div>
            <div className="m-lab">Firecracker 冷启动(官方)</div>
          </div>
          <div className="deck-metric">
            <div className="m-val">&lt;5 MiB</div>
            <div className="m-lab">单 microVM 内存开销(官方)</div>
          </div>
          <div className="deck-metric">
            <div className="m-val">Lambda</div>
            <div className="m-lab">AWS 用它跑 Lambda / Fargate</div>
          </div>
        </div>
        <div className="slide-foot">
          ¹ 模板已本地缓存时启动极快(100–800ms 级)。 ² Firecracker 灵活性受限于精简设备模型(仅 5 个虚拟设备)——
          这恰恰也是它攻击面小的原因。 · <b>结论:Firecracker 兼得强隔离与快启动,是 Agent 沙箱的理想底座。</b>
        </div>
      </div>
    ),
  },

  // ============ 7. 方案版图 ============
  {
    id: "landscape",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">④ 方案对比</span>
        <h2 className="slide-h">市面上的沙箱方案版图</h2>
        <p className="slide-lead">
          选对了底座(Firecracker),下一步是选交付方式。我们把候选分成三类,逐一看它们能否满足客户的四大需求。
        </p>
        <div className="deck-cards c3">
          <div className="deck-card">
            <span className="dc-icon">☁️</span>
            <h3>AWS 托管</h3>
            <p>
              <b>Bedrock AgentCore</b> —— 每会话独立 microVM,分钟级上手;但有 8 小时运行时上限。
            </p>
          </div>
          <div className="deck-card">
            <span className="dc-icon">🧩</span>
            <h3>可自建(自己账户)</h3>
            <p>
              <b>E2B on AWS</b>(开源,Firecracker)· <b>EKS + Kata / gVisor</b>(K8s 生态)· <b>自建裸 Firecracker</b>(本方案)。
            </p>
          </div>
          <div className="deck-card">
            <span className="dc-icon">🌐</span>
            <h3>第三方 SaaS</h3>
            <p>
              <b>E2B SaaS</b> · <b>Fly.io</b> · <b>Cloudflare Workers / Containers</b> —— 上手快,但数据不落自己账户。
            </p>
          </div>
        </div>
        <div className="slide-foot">
          结论先行:没有一个现成方案能同时满足「强隔离 + 快照 + 超卖 + 24×7 常驻 + 数据主权」—— 下面逐一拆解取舍。
        </div>
      </div>
    ),
  },

  // ============ 8. AgentCore ============
  {
    id: "agentcore",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">④ 方案对比 · 托管路线</span>
        <h2 className="slide-h">AWS Bedrock AgentCore:最快上手的托管方案</h2>
        <p className="slide-lead">
          它解决的是一个真问题 —— 大量组织卡在"从试点到生产"的鸿沟上,鸿沟不在模型,而在基础设施。
        </p>
        <div className="gap-stat">
          <div className="gs">
            <div className="gv warn">78%</div>
            <div className="gl">组织有 AI 试点</div>
          </div>
          <div className="gs-arrow">➜</div>
          <div className="gs">
            <div className="gv bad">仅 14%</div>
            <div className="gl">能进入生产</div>
          </div>
        </div>
        <div className="deck-split">
          <div className="deck-card good">
            <h3>✓ 托管的价值</h3>
            <p>
              分钟级跑通首个 Agent、每会话独立 microVM 隔离、自动扩缩、框架无关(Strands / LangGraph / CrewAI)、消费型计费。工程投入从"管道"转到"Agent 本身"。
            </p>
          </div>
          <div className="deck-card bad">
            <h3>✗ 为什么不够</h3>
            <p>
              <b>8 小时运行时上限</b> + 15 分钟空闲回收,会话状态 ephemeral;<b>无用户级 VM 快照</b>;超卖策略由平台控制、不可自定义;数据在 AWS 托管边界内,不完全落到自己 VPC。
            </p>
          </div>
        </div>
        <div className="slide-foot">
          定位:快速验证与标准 Agent 的绝佳起点。 但 8h 硬顶直接违反「24×7 常驻」、无快照违反「全生命周期」——
          <b>长驻 / 高密度 / 深度自控的场景需要自建。</b> ·{" "}
          <span className="pill">出处:AWS devguide runtime-how-it-works</span>
        </div>
      </div>
    ),
  },

  // ============ 9. E2B ============
  {
    id: "e2b",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">④ 方案对比 · 自建路线</span>
        <h2 className="slide-h">E2B on AWS(开源自建 & 托管 SaaS)</h2>
        <p className="slide-lead">
          E2B 是最接近的对标:底层就是 Firecracker、支持快照、开源可自建。但两条路各有硬伤。
        </p>
        <div className="deck-split">
          <div className="deck-card">
            <div className="cap-head">
              <span className="cap-ic">🧩</span>
              <h3 style={{ margin: 0 }}>E2B on AWS(开源自建)</h3>
            </div>
            <p style={{ marginTop: 10 }}>
              <span className="tick">✓</span> Firecracker microVM,支持 pause/resume 快照,Apache-2.0 开源、Terraform 部署。
            </p>
            <p>
              <span className="tick">✓</span> 已有客户在生产环境<b>大规模部署</b>,方案本身经过验证。
            </p>
            <p>
              <span className="cross">✗</span> 强依赖 <b>Consul + Nomad</b> 编排栈 —— 与现有 EKS 体系<b>异构</b>,<b>运维较重</b>,自建等于接手一整套平台。
            </p>
            <p>
              <span className="cross">✗</span> 跟随社区版本更新<b>有滞后</b>;整体<b>不适合小的 startup 团队</b>驾驭。
            </p>
          </div>
          <div className="deck-card">
            <div className="cap-head">
              <span className="cap-ic">🌐</span>
              <h3 style={{ margin: 0 }}>E2B SaaS(托管)</h3>
            </div>
            <p style={{ marginTop: 10 }}>
              <span className="tick">✓</span> 上手最快,按秒计费,Firecracker 隔离。
            </p>
            <p>
              <span className="cross">✗</span> 单会话有时长上限(Hobby 1h / Pro 24h),并非无限常驻。
            </p>
            <p>
              <span className="cross">✗</span> 数据经第三方托管环境,<b>不落自己 AWS 账户</b> —— 数据出境 / 合规是明确顾虑。
            </p>
          </div>
        </div>
        <div className="slide-foot">
          裁决:能力达标且有大规模生产案例,但自建路径异构、运维重、更新滞后,小团队难驾驭;SaaS 路径数据不自控。 ·
          <span className="pill">出处:e2b-dev/infra self-host.md · e2b.dev/pricing</span>
        </div>
      </div>
    ),
  },

  // ============ 10. EKS + Kata / gVisor ============
  {
    id: "kata",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">④ 方案对比 · 自建路线</span>
        <h2 className="slide-h">EKS + Kata / gVisor:K8s 里拿隔离</h2>
        <p className="slide-lead">
          很自然的想法:用 K8s RuntimeClass 选运行时。隔离能达标,但 Firecracker 的快照/超卖优势被 K8s 抽象层吃掉了。
        </p>
        <div className="deck-cards c3">
          <div className="deck-card">
            <h3>Kata Containers</h3>
            <p>轻量 VM 硬隔离,安全等级 ≈ EC2 实例,多租户/合规首选。<b>默认 QEMU</b>;换 Firecracker 会丢 GPU / 机密计算。</p>
          </div>
          <div className="deck-card">
            <h3>gVisor</h3>
            <p>用户态内核拦截 syscall,攻击面收缩 90%+;轻量、接近 runc。但<b>不是 microVM 级</b>硬隔离。</p>
          </div>
          <div className="deck-card">
            <h3>runc(基线)</h3>
            <p>共享宿主内核,启动最快、开销最低;隔离最弱,仅适合可信代码。</p>
          </div>
        </div>
        <div className="deck-split" style={{ marginTop: 14 }}>
          <div className="deck-card bad">
            <h3>✗ 快照能力不被一等暴露</h3>
            <p>
              Firecracker 自身支持快照,但经 Kata(containerd shim / K8s 编排)去驱动"暂停容器→存内存快照→跨节点恢复"<b>没有成熟的一等 API 路径</b>。
            </p>
          </div>
          <div className="deck-card bad">
            <h3>✗ 细粒度控制丢失</h3>
            <p>为适配 OCI/K8s,hypervisor 被抽象掉 —— 拿不到 Firecracker 的<b>快照/超卖细粒度控制</b>,而这恰是成本杠杆的关键。</p>
          </div>
        </div>
        <div className="slide-foot">
          裁决:隔离够,但「快照 + 超卖」这两条被抽象层削掉。 我们项目早期评估过 Kata-on-EKS,正因无法一等快照而移除。 ·
          <span className="pill">出处:kata-containers hypervisors.md</span>
        </div>
      </div>
    ),
  },

  // ============ 11. 第三方 SaaS ============
  {
    id: "saas",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">④ 方案对比 · SaaS 路线</span>
        <h2 className="slide-h">第三方 SaaS:Fly.io / Cloudflare</h2>
        <p className="slide-lead">
          都很成熟,但要么定位错配,要么隔离模型根本不同 —— 且数据都不落自己的 AWS 账户。
        </p>
        <div className="deck-split">
          <div className="deck-card">
            <div className="cap-head">
              <span className="cap-ic">🎈</span>
              <h3 style={{ margin: 0 }}>Fly.io Machines</h3>
            </div>
            <p style={{ marginTop: 10 }}>
              <span className="tick">✓</span> 底层同样是 Firecracker microVM,stop/start 亚秒级,有 suspend。
            </p>
            <p>
              <span className="cross">✗</span> 定位是<b>通用应用托管平台</b>,不是 agent 沙箱:缺 code-interpreter API / 模板 / 按会话回收等原语。
            </p>
            <p>
              <span className="cross">✗</span> 数据落 Fly 基础设施,不进自己账户。
            </p>
          </div>
          <div className="deck-card">
            <div className="cap-head">
              <span className="cap-ic">⚡</span>
              <h3 style={{ margin: 0 }}>Cloudflare Workers / Containers</h3>
            </div>
            <p style={{ marginTop: 10 }}>
              <span className="cross">✗</span> Workers 是 <b>V8 isolate,不是 microVM</b> —— 隔离弱;CPU 时长 30s(最高 5min),跑不了任意二进制 / 常驻进程。
            </p>
            <p>
              <span className="cross">✗</span> Containers(公测)能跑任意代码,但以 <b>scale-to-zero 休眠</b>为设计,仍是 CF 托管、非常驻 microVM,不落自己账户。
            </p>
          </div>
        </div>
        <div className="slide-foot">
          裁决:Fly 定位错配 + 数据不自控;Cloudflare 隔离弱 / 时长受限。 ·
          <span className="pill">出处:fly.io/docs · developers.cloudflare.com/workers/platform/limits</span>
        </div>
      </div>
    ),
  },

  // ============ 12. 五方案对比大表 ============
  {
    id: "matrix",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">④ 方案对比</span>
        <h2 className="slide-h">五种沙箱方案,一张表看清</h2>
        <p className="slide-lead">
          把启动、隔离、暂停恢复、成本、时长上限拉平了比 —— 只有自建沙盒兼具低成本、可超卖与快照,控制力最强。
        </p>
        <div className="deck-table-wrap">
          <table className="deck-table">
            <thead>
              <tr>
                <th>方案</th>
                <th>启动速度</th>
                <th>安全隔离</th>
                <th>暂停/恢复</th>
                <th>成本</th>
                <th>时长上限</th>
                <th>计费方式</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>AWS Lambda</td>
                <td>非冷启 10ms 级</td>
                <td><span className="tick">高</span></td>
                <td><span className="cross">不支持</span></td>
                <td><span className="tick">低</span></td>
                <td><span className="warn">15 分钟</span></td>
                <td>请求数 + 执行时间</td>
              </tr>
              <tr>
                <td>容器 EKS / ECS</td>
                <td>镜像缓存则秒级</td>
                <td><span className="cross">低</span></td>
                <td><span className="cross">不支持</span></td>
                <td><span className="cross">高</span></td>
                <td><span className="tick">无限制</span></td>
                <td>按实例运行时长</td>
              </tr>
              <tr>
                <td>Bedrock AgentCore</td>
                <td>100ms 级</td>
                <td><span className="tick">高</span></td>
                <td><span className="cross">暂不支持</span></td>
                <td><span className="tick">低</span></td>
                <td><span className="warn">8 小时</span></td>
                <td>按 CPU/内存用量</td>
              </tr>
              <tr>
                <td>E2B on AWS</td>
                <td>缓存命中 100ms 级</td>
                <td><span className="tick">高</span></td>
                <td><span className="tick">支持(增量快照)</span></td>
                <td><span className="cross">高</span></td>
                <td><span className="tick">无限制</span></td>
                <td>按沙箱运行时长</td>
              </tr>
              <tr>
                <td className="col-us">自建沙盒(本方案)</td>
                <td className="col-us">Firecracker 级</td>
                <td className="col-us"><span className="tick">高</span></td>
                <td className="col-us"><span className="tick">✓ 支持</span></td>
                <td className="col-us"><span className="tick">低</span></td>
                <td className="col-us"><span className="tick">无限制</span></td>
                <td className="col-us">按实例规格,<b>可超卖</b></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div className="slide-foot">
          关键洞察:容器成本灵活但隔离最弱;microVM 方案(AgentCore / E2B / 自建)兼顾强隔离与快启动;
          <b>只有自建沙盒同时拿下低成本、超卖与暂停/恢复。</b> 代价是要自研编排/rootfs/网络 —— 下面展示我们已建好这套底座。
        </div>
      </div>
    ),
  },

  // ============ 13. 我们的方案 · 架构图 ============
  {
    id: "arch",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">⑤ 我们的方案</span>
        <h2 className="slide-h">整体架构:EKS 管平台,node-agent 管 microVM</h2>
        <p className="slide-lead">
          中心化无状态控制面 + DynamoDB 状态 + 每台裸金属上的 node-agent 直管裸 Firecracker。数据全留在您自己的账户内。
        </p>
        {/* 架构图沿用深色"图纸"画布(彩色 AWS 图标在深底上最清晰,亦是 AWS 官方架构图惯例),
            用细边与圆角让它在浅色 deck 上成为一块有意为之的聚焦区。 */}
        <div
          style={{
            padding: 18,
            background: "#0e1116",
            border: "1px solid #e6e3db",
            borderRadius: 14,
            boxShadow: "0 1px 2px rgba(26,28,32,0.04)",
          }}
        >
          <ArchDiagram />
        </div>
        <div className="slide-foot">
          数据流:用户/Agent → NLB+ingress → 控制面(鉴权/幂等/选点)→ 写 DynamoDB → 调对应节点 node-agent →
          起/恢复 Firecracker microVM(tap 网络 + vsock exec);凭据经 LiteLLM 网关注入,永不进沙盒。
        </div>
      </div>
    ),
  },

  // ============ 14. 痛点 → 解法映射(核心说服页) ============
  {
    id: "pain-map",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">⑤ 我们的方案</span>
        <h2 className="slide-h">逐条回应:四大挑战 → 我们的解法</h2>
        <p className="slide-lead">
          方案好不好,不看功能清单,看它是否把开头那四个痛点一条条落地解决了。
        </p>
        <div className="pain-map">
          <div className="pain-row">
            <div className="pain-cell pain">
              <div className="pc-h">⚠️ 执行不可信代码</div>
              <div className="pc-d">LLM 代码可能尝试逃逸、提权、访问宿主。</div>
            </div>
            <div className="pain-arrow">➜</div>
            <div className="pain-cell fix">
              <div className="pc-h">Firecracker microVM + jailer</div>
              <div className="pc-d">每沙箱独立 guest 内核(实测 guest ≠ 宿主内核)+ 极简设备模型 + jailer 二次屏障。完整 root 也逃不出 VM 边界。</div>
            </div>
          </div>
          <div className="pain-row">
            <div className="pain-cell pain">
              <div className="pc-h">🌐 访问第三方数据 / 凭据风险</div>
              <div className="pc-d">外部数据源放大攻击面,模型凭据一旦泄露后果严重。</div>
            </div>
            <div className="pain-arrow">➜</div>
            <div className="pain-cell fix">
              <div className="pc-h">网络隔离 + 凭据零进沙盒</div>
              <div className="pc-d">每沙箱独立网络槽位,可细粒度放行;Bedrock 凭据只在 LiteLLM 的 IRSA,沙箱永远看不到真实 key;强制 IMDSv2 挡 SSRF。</div>
            </div>
          </div>
          <div className="pain-row">
            <div className="pain-cell pain">
              <div className="pc-h">👥 多租户并发 / 故障隔离</div>
              <div className="pc-d">一个用户的崩溃或恶意行为不能影响邻居。</div>
            </div>
            <div className="pain-arrow">➜</div>
            <div className="pain-cell fix">
              <div className="pc-h">VM 级故障边界 + 自愈编排</div>
              <div className="pc-d">VM 是天然故障域;控制面 reconcile 对账自愈 + leader 选举 + 节点心跳,漂移自动标记回收,生产级而非 POC。</div>
            </div>
          </div>
          <div className="pain-row">
            <div className="pain-cell pain">
              <div className="pc-h">📦 高密度 / 成本</div>
              <div className="pc-d">强隔离通常意味着高开销,规模化后成本失控。</div>
            </div>
            <div className="pain-arrow">➜</div>
            <div className="pain-cell fix">
              <div className="pc-h">快照回收 + 超卖 + Spot</div>
              <div className="pc-d">空闲沙箱快照挂起释放内存;按分配超配、按实际驻留装箱;Spot 把裸金属压到按需三成。单沙箱摊算 ~$4/月。</div>
            </div>
          </div>
        </div>
      </div>
    ),
  },

  // ============ 15. 核心组件 ============
  {
    id: "components",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">⑤ 我们的方案</span>
        <h2 className="slide-h">核心组件与能力</h2>
        <p className="slide-lead">
          七个模块把"裸 Firecracker"包装成一个生产可用、开发者友好的沙盒平台。
        </p>
        <div className="deck-cards c3">
          <div className="deck-card accent">
            <span className="dc-icon">🖥️</span>
            <h3>EKS + Bare Metal EC2</h3>
            <p>c6g.metal(arm64,默认)/ c5n.metal(x86)。EC2 仅在 .metal 暴露 /dev/kvm,裸金属才能跑 microVM。EKS 管平台,microVM 不进 K8s。</p>
          </div>
          <div className="deck-card accent">
            <span className="dc-icon">🎛️</span>
            <h3>编排层</h3>
            <p>无状态控制面 + DynamoDB。reconcile 自愈 + leader 选举 + 节点心跳注册表 + 快照落盘强一致 —— 生产级四原语。</p>
          </div>
          <div className="deck-card accent">
            <span className="dc-icon">📸</span>
            <h3>快照机制</h3>
            <p>Firecracker snapshot/restore + Diff 增量脏页,落节点持久 EBS。suspend 释放内存、resume 亚秒恢复,支持暖池预热。</p>
          </div>
          <div className="deck-card accent">
            <span className="dc-icon">🗄️</span>
            <h3>JuiceFS 存储</h3>
            <p>可选 workspace 路径:guest 内挂 JuiceFS,底层 S3 + Redis 元数据,对齐 Fly+JuiceFS 现状,数据天然持久到 S3。</p>
          </div>
          <div className="deck-card accent">
            <span className="dc-icon">🔌</span>
            <h3>端口暴露</h3>
            <p>路径路由 /s/&#123;id&#125;/&#123;port&#125;,共享 NLB,任意端口 + WebSocket 透传 + Web 终端 + 文件传输,零 per-sandbox 公网 IP。</p>
          </div>
          <div className="deck-card accent">
            <span className="dc-icon">💸</span>
            <h3>Spot 利用</h3>
            <p>c6g.metal Spot 约按需 29%。Karpenter 管节点池,IMDS 监听回收,Diff 快照 + EBS 卷幸存保住内存状态。</p>
          </div>
        </div>
        <div className="slide-foot">
          外加第七块 —— <b>Fly Machines 风格 REST API</b>:一套幂等 API 覆盖创建/exec/快照/文件/端口的完整生命周期(第 17 页)。
        </div>
      </div>
    ),
  },

  // ============ 16. 成本模型(条形图) ============
  {
    id: "cost",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">⑤ 我们的方案 · 成本</span>
        <h2 className="slide-h">成本:快照是杠杆,Spot 是引擎</h2>
        <p className="slide-lead">
          客户最关心的问题:规模化后多少钱?我们用"快照回收 + 超卖 + Spot"三招,把单台裸金属月费降到按需的三分之一。
        </p>
        <div
          style={{
            padding: "20px 22px",
            marginBottom: 16,
            background: "#fff",
            border: "1px solid var(--line)",
            borderRadius: 14,
          }}
        >
          <div style={{ fontSize: 12, marginBottom: 14, color: "var(--paper-faint)" }}>
            单台 c6g.metal(64 vCPU / 128 GiB)最小配置月费 · us-east-1 · 2026-07 实测
          </div>
          <div className="cost-bars">
            <div className="cost-bar">
              <div className="cb-lab">
                按需 On-Demand<small>$2.304/hr</small>
              </div>
              <div className="cb-track">
                <div className="cb-fill hi" style={{ width: "100%" }}>
                  ~$1,828 / 月
                </div>
              </div>
              <div className="cb-val">基线</div>
            </div>
            <div className="cost-bar">
              <div className="cb-lab">
                按需 + Savings Plan<small>~42% off 计算</small>
              </div>
              <div className="cb-track">
                <div className="cb-fill mid" style={{ width: "61%" }}>
                  ~$1,122 / 月
                </div>
              </div>
              <div className="cb-val">-39%</div>
            </div>
            <div className="cost-bar">
              <div className="cb-lab">
                Spot + 快照回收<small>本平台目标模式</small>
              </div>
              <div className="cb-track">
                <div className="cb-fill lo" style={{ width: "35%" }}>
                  ~$632 / 月
                </div>
              </div>
              <div className="cb-val">-65%</div>
            </div>
          </div>
        </div>
        <div className="deck-metrics">
          <div className="deck-metric">
            <div className="m-val">400+</div>
            <div className="m-lab">单台承载沙箱(快照回收)</div>
          </div>
          <div className="deck-metric">
            <div className="m-val">~$4</div>
            <div className="m-lab">单沙箱摊算 / 月</div>
          </div>
          <div className="deck-metric">
            <div className="m-val">~50 MB</div>
            <div className="m-lab">空载每 VM 实际驻留(分配 1.5 GiB)</div>
          </div>
          <div className="deck-metric">
            <div className="m-val">~1.2s</div>
            <div className="m-lab">快照 resume,用户无感</div>
          </div>
        </div>
        <div className="slide-foot">
          空载驻留仅 ~50 MB ⇒ 按分配超配、按实际装箱;空闲即快照挂起回收内存。24×7 活跃 ~$23/沙箱·月 → 快照回收 <b>~$4/沙箱·月</b>。
          Spot 价格实时浮动,以实际报价为准。
        </div>
      </div>
    ),
  },

  // ============ 17. API 便捷性 ============
  {
    id: "api",
    render: () => (
      <div className="slide">
        <span className="slide-kicker">⑤ 我们的方案</span>
        <h2 className="slide-h">API 便捷性:一套 Fly 风格 REST 覆盖全生命周期</h2>
        <p className="slide-lead">
          回应"易用接入"需求:Bearer 认证 + 幂等键 + 乐观锁 + capability 模型。配 Portal 控制台即可演示与运维。
        </p>
        <div className="deck-split">
          <div className="code-block" style={{ maxHeight: "none", fontSize: "12px" }}>
{`# 创建(幂等,可选镜像/端口暴露)
POST /sandboxes
{"image":"web","cpu":2,"mem_mib":4096,
 "idempotency_key":"req-123"}

# 等待就绪(长轮询)
GET  /sandboxes/{id}/wait?state=running
# 执行命令(vsock 优先)
POST /sandboxes/{id}/exec  {"cmd":"npm test"}

# 挂起(快照)/ 恢复(~1.2s)
POST /sandboxes/{id}/suspend
POST /sandboxes/{id}/resume

# 端口暴露(含 WebSocket)
ANY  /s/{id}/{port}/{path}
# 文件上传 / 下载(base64 over exec)
PUT  /sandboxes/{id}/files?path=/root/app.py
GET  /sandboxes/{id}/files?path=/root/out.txt`}
          </div>
          <div>
            <div className="deck-cards" style={{ gap: 12 }}>
              <div className="deck-card">
                <h3>开发者体验</h3>
                <p>create/wait/suspend/resume/exec/locate + 文件 + 端口,一套 API 全搞定;幂等键让重试安全。</p>
              </div>
              <div className="deck-card">
                <h3>Portal 控制台</h3>
                <p>Dashboard 总览 + 沙盒详情 + API Playground + 拖拽文件传输 + 一键 Web 终端(xterm.js),纯本地即可跑。</p>
              </div>
              <div className="deck-card">
                <h3>凭据零进沙盒</h3>
                <p>Bedrock 凭据只在 LiteLLM 的 IRSA 角色,沙盒永远看不到真实 key;多 key 支持多租户。</p>
              </div>
            </div>
          </div>
        </div>
        <div className="slide-foot">
          当前提供 REST API + Portal;独立客户端 SDK 为后续规划项。
        </div>
      </div>
    ),
  },

  // ============ 18. 收尾 ============
  {
    id: "closing",
    render: () => (
      <div className="slide slide-cover">
        <div className="cover-badge">总结 · SUMMARY</div>
        <h1 style={{ fontSize: 38 }}>
          全绿的那一列,<span className="grad">我们已经建好了</span>
        </h1>
        <p>
          强隔离 · 快照 · 超卖 · 24×7 常驻 · 数据主权 —— 客户的四大挑战逐条落地,
          自建 Firecracker 是唯一同时满足的路径,且成本最优、控制力最强。
        </p>
        <div className="deck-metrics" style={{ maxWidth: 820, margin: "10px auto 24px" }}>
          <div className="deck-metric">
            <div className="m-val">-65%</div>
            <div className="m-lab">单台月费 vs 按需(Spot+快照)</div>
          </div>
          <div className="deck-metric">
            <div className="m-val">~$4</div>
            <div className="m-lab">单沙箱摊算 / 月</div>
          </div>
          <div className="deck-metric">
            <div className="m-val">400+</div>
            <div className="m-lab">单台承载沙箱</div>
          </div>
          <div className="deck-metric">
            <div className="m-val">100%</div>
            <div className="m-lab">数据留在自己 AWS 账户</div>
          </div>
        </div>
        <div className="cover-chips">
          <span className="chip chip-strong">真机已验证:reconcile · leader · 快照 · 暖池 · Spot 疏散</span>
        </div>
        <div className="slide-foot" style={{ maxWidth: 720, margin: "26px auto 0" }}>
          建议路线:标准 / 快速验证场景用 AgentCore 起步;长驻、高密度、深度自控与数据主权场景,用本方案自建 —— 两者可并存互补。
        </div>
      </div>
    ),
  },
];

"use client";

/**
 * 自建 Sandbox 平台架构图(AWS 图标风格,纯 SVG)。
 * 用 AWS 官方产品类别配色的圆角方块图标示意各服务:
 *   - 计算(EC2/EKS/Compute): 橙 #ED7100
 *   - 网络与内容分发(VPC/NLB): 紫 #8C4FFF
 *   - 存储(S3/EBS): 绿 #7AA116
 *   - 数据库(DynamoDB): 蓝 #2E27AD / #4D72F3
 *   - 机器学习(Bedrock): 青绿 #01A88D
 * 数据流:开发者 → NLB/Ingress → 控制面(EKS) → node-agent(裸金属) → Firecracker microVM
 * 状态与存储:DynamoDB(状态)· EBS(内存快照)· S3/JuiceFS(rootfs 模板/持久化)· Bedrock(经 LiteLLM)
 */

// AWS 风格图标:顶部类别色标题条 + 主体浅色底 + 中央符号
function AwsIcon({
  x,
  y,
  w = 74,
  h = 74,
  color,
  glyph,
  title,
  sub,
}: {
  x: number;
  y: number;
  w?: number;
  h?: number;
  color: string;
  glyph: React.ReactNode;
  title: string;
  sub?: string;
}) {
  return (
    <g transform={`translate(${x},${y})`}>
      <rect width={w} height={h} rx="10" fill="#fff" />
      <path
        d={`M0 10 a10 10 0 0 1 10 -10 h${w - 20} a10 10 0 0 1 10 10 v18 h-${w} z`}
        fill={color}
      />
      <g transform={`translate(${w / 2},${h / 2 + 4})`} fill="none" stroke={color} strokeWidth="2.4">
        {glyph}
      </g>
      <text
        x={w / 2}
        y={h + 16}
        textAnchor="middle"
        fontSize="12"
        fontWeight="700"
        fill="#e6e9ef"
      >
        {title}
      </text>
      {sub ? (
        <text x={w / 2} y={h + 32} textAnchor="middle" fontSize="10.5" fill="#8b93a1">
          {sub}
        </text>
      ) : null}
    </g>
  );
}

export function ArchDiagram() {
  return (
    <svg
      viewBox="0 0 1000 560"
      width="100%"
      style={{ maxHeight: 480, display: "block", margin: "0 auto" }}
      role="img"
      aria-label="自建 Sandbox 平台 AWS 架构图"
    >
      {/* ==== 外框:AWS Cloud ==== */}
      <rect x="8" y="8" width="984" height="544" rx="16" fill="none" stroke="#3a4048" strokeDasharray="2 4" />
      <text x="28" y="34" fontSize="12" fontWeight="700" fill="#ED7100">
        ☁ AWS Cloud · 单一账户 · 数据主权自控
      </text>

      {/* ==== VPC 框 ==== */}
      <rect x="24" y="48" width="952" height="492" rx="12" fill="none" stroke="#8C4FFF" strokeOpacity="0.5" />
      <text x="40" y="68" fontSize="11" fontWeight="700" fill="#a986ff">
        VPC · 私有网络
      </text>

      {/* ==== EKS 集群框 ==== */}
      <rect x="250" y="86" width="726" height="440" rx="12" fill="rgba(237,113,0,0.05)" stroke="#ED7100" strokeOpacity="0.55" />
      <text x="266" y="106" fontSize="11" fontWeight="700" fill="#f0965a">
        Amazon EKS 集群 · claude-sbx
      </text>

      {/* 开发者 / Agent */}
      <AwsIcon
        x={44}
        y={230}
        color="#232F3E"
        title="开发者 / AI Agent"
        sub="Claude Code · Codex"
        glyph={
          <>
            <circle cx="0" cy="-8" r="7" />
            <path d="M-12 14 a12 10 0 0 1 24 0" />
          </>
        }
      />

      {/* NLB + Ingress */}
      <AwsIcon
        x={140}
        y={140}
        color="#8C4FFF"
        title="NLB + ingress-nginx"
        sub="REST API · /s/{id}/{port}"
        glyph={
          <>
            <circle cx="0" cy="0" r="11" />
            <path d="M-11 0 h22 M0 -11 v22" />
          </>
        }
      />

      {/* 端口暴露 / WebSocket */}
      <AwsIcon
        x={140}
        y={330}
        color="#8C4FFF"
        title="端口暴露 / WS"
        sub="Web 预览 · 终端"
        glyph={
          <>
            <path d="M-11 -6 h22 M-11 6 h22" />
            <circle cx="-4" cy="-6" r="2.5" fill="#8C4FFF" />
            <circle cx="4" cy="6" r="2.5" fill="#8C4FFF" />
          </>
        }
      />

      {/* 控制面 Deployment */}
      <AwsIcon
        x={286}
        y={150}
        w={150}
        color="#ED7100"
        title="sandbox-control-plane"
        sub="Driver·WarmPool·Reconciler"
        glyph={
          <>
            <rect x="-16" y="-12" width="32" height="24" rx="3" />
            <path d="M-16 -2 h32 M-6 -12 v24" />
          </>
        }
      />

      {/* LiteLLM / Bedrock */}
      <AwsIcon
        x={286}
        y={300}
        w={150}
        color="#01A88D"
        title="LiteLLM → Bedrock"
        sub="凭据仅在 IRSA,不进沙盒"
        glyph={
          <>
            <circle cx="-8" cy="0" r="4" />
            <circle cx="8" cy="-6" r="4" />
            <circle cx="8" cy="6" r="4" />
            <path d="M-4 0 l8 -5 M-4 0 l8 5" />
          </>
        }
      />

      {/* 裸金属节点框 */}
      <rect x="486" y="128" width="286" height="300" rx="12" fill="rgba(255,255,255,0.02)" stroke="#f5a623" strokeOpacity="0.5" />
      <text x="500" y="148" fontSize="11" fontWeight="700" fill="#f5a623">
        c6g.metal / c5n.metal 裸金属 · KVM 嵌套虚拟化
      </text>

      {/* node-agent */}
      <AwsIcon
        x={510}
        y={168}
        w={150}
        color="#ED7100"
        title="node-agent DaemonSet"
        sub="jailer · tap · snapshot · 心跳"
        glyph={
          <>
            <rect x="-15" y="-11" width="30" height="22" rx="3" />
            <circle cx="0" cy="0" r="4" />
          </>
        }
      />

      {/* microVM x3 */}
      {[0, 1, 2].map((i) => (
        <g key={i} transform={`translate(${510 + i * 88},${300})`}>
          <rect width="72" height="72" rx="10" fill="#12141c" stroke="#7c5cff" />
          <rect x="10" y="12" width="52" height="26" rx="4" fill="none" stroke="#7c5cff" strokeWidth="2" />
          <text x="36" y="29" textAnchor="middle" fontSize="9" fill="#b9a9ff" fontWeight="700">
            guest
          </text>
          <text x="36" y="52" textAnchor="middle" fontSize="9" fill="#8b93a1">
            µVM
          </text>
          <text x="36" y="64" textAnchor="middle" fontSize="8" fill="#5c6472">
            独立内核
          </text>
        </g>
      ))}
      <text x={624} y={392} textAnchor="middle" fontSize="10" fill="#8b93a1">
        Firecracker microVM(超卖 · 独立 guest 内核)
      </text>

      {/* ==== 右侧存储与状态列 ==== */}
      {/* DynamoDB */}
      <AwsIcon
        x={806}
        y={150}
        w={150}
        color="#2E27AD"
        title="DynamoDB"
        sub="状态·事件·nodes·locks"
        glyph={
          <>
            <ellipse cx="0" cy="-8" rx="14" ry="5" />
            <path d="M-14 -8 v16 a14 5 0 0 0 28 0 v-16" />
          </>
        }
      />

      {/* EBS 快照 */}
      <AwsIcon
        x={806}
        y={270}
        w={150}
        color="#7AA116"
        title="持久状态 EBS"
        sub="内存快照 base+diff"
        glyph={
          <>
            <rect x="-14" y="-11" width="28" height="22" rx="2" />
            <path d="M-14 -3 h28 M-14 4 h28" />
          </>
        }
      />

      {/* S3 + JuiceFS */}
      <AwsIcon
        x={806}
        y={390}
        w={150}
        color="#7AA116"
        title="S3 + JuiceFS"
        sub="rootfs 分发 · /workspace"
        glyph={
          <>
            <ellipse cx="0" cy="-8" rx="13" ry="4.5" />
            <path d="M-13 -8 v16 a13 4.5 0 0 0 26 0 v-16" />
            <path d="M-13 0 a13 4.5 0 0 0 26 0" />
          </>
        }
      />

      {/* ==== 连线 ==== */}
      <defs>
        <marker id="arr" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto">
          <path d="M0 0 L7 3 L0 6 z" fill="#5c6472" />
        </marker>
        <marker id="arrA" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto">
          <path d="M0 0 L7 3 L0 6 z" fill="#7c5cff" />
        </marker>
      </defs>
      <g stroke="#5c6472" strokeWidth="1.6" fill="none" markerEnd="url(#arr)">
        {/* 开发者 → NLB */}
        <path d="M118 258 C 150 240, 150 220, 172 200" />
        {/* 开发者 → 端口暴露 */}
        <path d="M118 280 C 150 300, 150 330, 172 360" />
        {/* NLB → 控制面 */}
        <path d="M254 190 C 268 200, 270 210, 300 216" />
        {/* 端口暴露 → node-agent */}
        <path d="M254 360 C 400 360, 430 220, 508 200" />
      </g>
      <g stroke="#7c5cff" strokeWidth="1.8" fill="none" markerEnd="url(#arrA)">
        {/* 控制面 → node-agent (HTTP) */}
        <path d="M438 190 C 470 190, 480 195, 520 198" />
        {/* node-agent → microVM */}
        <path d="M585 240 v54" />
      </g>
      <g stroke="#4d72f3" strokeWidth="1.5" fill="none" strokeDasharray="4 3" markerEnd="url(#arr)">
        {/* 控制面 → DynamoDB */}
        <path d="M438 172 C 640 120, 720 150, 804 178" />
        {/* node-agent → EBS 快照 */}
        <path d="M662 210 C 740 240, 760 270, 804 292" />
        {/* node-agent → S3/JuiceFS */}
        <path d="M662 226 C 740 360, 760 400, 804 416" />
      </g>

      {/* 图例 */}
      <g transform="translate(44,470)" fontSize="10.5" fill="#8b93a1">
        <line x1="0" y1="0" x2="22" y2="0" stroke="#7c5cff" strokeWidth="2" markerEnd="url(#arrA)" />
        <text x="28" y="4">控制/调度</text>
        <line x1="110" y1="0" x2="132" y2="0" stroke="#4d72f3" strokeWidth="2" strokeDasharray="4 3" />
        <text x="140" y="4">状态/存储</text>
        <line x1="220" y1="0" x2="242" y2="0" stroke="#5c6472" strokeWidth="2" markerEnd="url(#arr)" />
        <text x="250" y="4">用户流量 / 端口暴露</text>
      </g>
    </svg>
  );
}

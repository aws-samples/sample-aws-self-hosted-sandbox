# 自定义镜像 / 多 rootfs 模板设计

> 让 `image` 字段真正生效 —— 沙盒按 `image` 选不同根文件系统模板(如自带 web 的镜像),
> 而非所有沙盒都 CoW 同一个 min-rootfs。**已实现并真机验证(2026-07)。**

---

## 1. 现状(改造前):`image` 是"死字段"

`node-agent op_create` 无论传什么 `image`,都 CoW 固定的 `/opt/sbx/rootfs.ext4`(min-rootfs)。
`image` 只是 DynamoDB 元数据标签,不影响 microVM 内容 → 沙盒里没有 web 服务,端口暴露打开是空的。

Firecracker 是 microVM,启动 `kernel + ext4 rootfs`,不存在"拉容器镜像"动作 —— 必须把镜像内容
变成 ext4 rootfs 才能用。

## 2. 方案:命名 rootfs 模板(而非实时拉 OCI)

- 预构建业务镜像的 `rootfs-{name}.tar.gz` 传 S3;节点启动拉下来造 `/opt/sbx/rootfs-{name}.ext4`;
  create 按 `image` 选模板 CoW 复制。
- 与现有"节点 userData 拉 min-rootfs 造 ext4"机制同构,改动最小、create 保持秒级 CoW。
- 实时拉 OCI→转 ext4 更灵活但 create 慢数十秒、节点要装 docker、并发复杂,弃用(留未来增强)。

## 3. 数据流

```
create(image="web")
  → 控制面 normalize_image → "web";非默认 image 跳过暖池(暖池只预热 min)走冷建
  → driver create body 带 rootfs_template="web"
  → node-agent _rootfs_template_path: /opt/sbx/rootfs-web.ext4 存在 → CoW;否则回退默认 min
  → guest sbxinit 检测 /web/index.html → 开机自起 :80
  → 端口暴露 /s/{id}/80/ 直接返回站点
```

## 4. 实现要点(已落地)

- **`scripts/build-rootfs-image.sh <name> <bucket>`**:泛化自 build-min-rootfs.sh,复用 sbxinit +
  vsock-exec-agent(保证 exec/端口暴露仍工作),按 name 叠加应用层。内置 `web` 预设:demo 首页 +
  sbxinit 开机自起 :80(用 python 绝对路径 `/usr/local/bin/python3`,因 sbxinit 由 /bin/sh 执行 PATH 不全)。
- **node-agent**:`_rootfs_template_path(name)` —— 空/min/default→默认 ROOTFS;否则找
  `/opt/sbx/rootfs-{name}.ext4`,不存在回退默认。`op_create` / `op_resume` 兜底均用它。
- **控制面**:`normalize_image(image)`(取末段去 tag)+ create/resume body 带 `rootfs_template`;
  `/admin/images` 返回可用列表(`SANDBOX_IMAGES` env,默认 `min,web`)供 Portal 下拉。
  **非默认 image 跳过暖池**(暖池预热 min,快照不匹配自定义 image)。
- **terraform phase3**:`rootfs_images` 变量(默认 `web`),userData 循环从 rootfs_s3_uri 同目录拉
  `rootfs-{name}.tar.gz` 造 `/opt/sbx/rootfs-{name}.ext4`。
- **Portal**:创建表单 image 改下拉(拉 `/admin/images`),web 预设标注"自带 demo 站点"。

## 5. 真机验证(2026-07,EKS + c6g.metal)

- `build-rootfs-image.sh web` → 节点造 `/opt/sbx/rootfs-web.ext4`。
- create `image=web`(冷建,`restore_time_s=none`)→ guest `/web/index.html` 存在 →
  `/s/{id}/80/` **直接返回 demo 站点 HTML,无需手动起服务** ✅。
- 回归:`image` 留空 → 走暖池秒起 min,未受影响 ✅。

## 6. 关键坑

1. **暖池冲突**:暖池预热的是 min 快照;不判断 image 就 claim 会让 `image=web` 拿到 min 内容。
   修复:非默认 image 跳过暖池走冷建。
2. **sbxinit PATH**:sbxinit 由 `/bin/sh` 执行,PATH 未必含 `/usr/local/bin` → `python3` 找不到。
   自起 web 用绝对路径。
3. **模板需在节点启动前就位**:userData 启动时拉模板;运行中新增模板需重启节点或手动造(SSM)。

## 7. 未来增强

- 按 image 分暖池(对齐 E2B/Fly,让自定义 image 也能秒起)。
- 实时 OCI → rootfs 转换(真正"任意容器镜像")。
- 更多预设(node / python-web 等,同脚本加 case)。

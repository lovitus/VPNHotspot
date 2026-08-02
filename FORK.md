# 此 fork 的补丁与发布方式

此 fork 以 [Mygod/VPNHotspot](https://github.com/Mygod/VPNHotspot) 的稳定版为基础，维护少量可重放的补丁。当前补丁针对 v2 遗留的 `vpnhotspot_fwd` 链仍引用 `vpnhotspot_acl`、导致 v3 的 Clean/reapply 删除 ACL 失败的情况。它保留其他防火墙错误的报告，不把所有 `iptables-restore` 失败当作成功。

另有一个 Rust 1.99 Clippy 兼容补丁：ICMP 校验和按两个字节迭代的写法改用 `as_chunks::<2>()`。输入为固定 16 字节数组，校验和计算、路由行为及外部状态均不改变，因此不需要新增 routing.md 状态说明。维护分支的上游 master 已将相关函数拆到 `icmp_quote.rs`，稳定版补丁仍针对 v3.0.8 的 `icmp_wire.rs` 重放。

`iptables-restore: line 10 failed` 本身不足以确认每一台设备的原因。若重启或本补丁的 Clean/reapply 后仍出错，需要应用版本、Android/ROM 版本、触发操作及“导出调试信息”里的 restore 输入。

## v3.0.8-p3：旧策略路由恢复

设备诊断确认：旧版 `17800/17900` 规则指向已失效的接口表，`17980 unreachable` 在 v3 的 `20700/20800` 之前拒绝客户端流量。移除设备上三条精确旧规则后，转发、NAT、TCP 握手与双向数据立即恢复。

新增 patch 让 Clean/reapply 识别同接口的旧 guard 与无额外选择器的 lookup 组，通过 netlink 精确删除规则快照；不按优先级全删、不清空上游表、不更改正常启动/停止。保留独立自定义 lookup、带 mark/前缀的规则和当前规则。识别采用规则形状，不可能区分刻意复用完全相同规则组的第三方工具，因此不能同时运行另一个旧版路由管理者。新增平台中立 Rust 测试及隔离命名空间真实 netlink 回归，验证旧阻断重现、精确清理、重复规则、无关规则保留和幂等性。

来源已追溯至上游 [`c6b260ebf65193d01245640df3fac3834f433370`](https://github.com/Mygod/VPNHotspot/commit/c6b260ebf65193d01245640df3fac3834f433370)，作者时间 2026-04-24 21:14:04 -04:00，首次包含于 v3.0.0（2026-05-25 发布）。该提交改编号时明确声明不清理旧规则、要求无残留升级或手动重启。此补丁补充 Clean 的升级恢复能力，不否定新优先级设计，也不声称修复所有无网络问题。

## 维护分支与发布源码

仓库默认分支是维护分支，可以沿用 `master` 这个名字。它保存初始上游源码、`patches/` 和构建发布流程；定时任务不会把上游的新代码合并回这个分支。修改维护分支中的源码后，必须同步生成对应 patch；自动发布只应用 `patches/*.patch`，不会把维护分支的其他源码改动自动带进 APK。

`Patched stable release` workflow 按以下过程运行：

1. 读取上游最新正式 release，排除预发布，并把 tag 固定到具体 commit SHA。
2. 新建独立源码目录，按文件名顺序严格应用 `patches/*.patch`。补丁不适用就停止，不自动解决冲突或丢弃补丁。
3. 从 `patches/revision` 读取 1–999 的补丁修订号。应用版本名改为 `上游版本-p修订号`，`versionCode` 改为 `上游 versionCode × 1000 + 修订号`。
4. 从触发本次运行的维护 commit 读取明确白名单：完整 `.github/workflows/`、`scripts/fork-release/`、`patches/`、`scripts/verify_legacy_firewall_cleanup.py`、`FORK.md`。将这些文件携入源码，核对 workflow 目录的 Git tree 与维护 commit 完全一致；不复制整个维护源码、`.git` 或未跟踪文件。
5. 把真实补丁源码、版本修改和上述可信维护文件提交为一个新 commit，以 Git bundle 把同一个 commit 交给后续 job。
6. 在独立网络命名空间重现上游旧清理逻辑的失败，验证修复后的实际规则；随后执行 Rust 格式、编译、clippy、库测试、依赖审计，以及 Android Debug 构建、检查、R8 检查和 Release 构建。
7. 只有验证 job 成功，独立发布 job 才读取签名 secrets。它核对包名、版本、四种 ABI 的 daemon、APK 对齐和签名，再发布。

当前目标 tag 为 `v3.0.8-p2`。首次试运行已创建 `v3.0.8-p1` 源码 tag，但在草稿发布阶段失败，未公开 release；保留该 tag，不移动它，以 revision 2 修复发布流程。这个 tag 指向**实际编译的源码 commit**，不是维护分支的 commit；其父 commit 是精确上游版本，新增内容是应用补丁、版本修改和可信维护文件。GitHub release 自带的源代码归档对应 APK 的源码；AOSP 子模块仍需按 `.gitmodules` 和 gitlink 初始化。release 的 `build-info.json` 另外记录上游 SHA、源码 SHA/tree、各补丁 SHA-256、维护文件来源 SHA 与逐文件 SHA-256、workflow run URL、签名证书和 APK 摘要。`SHA256SUMS` 可用于核对下载。

同一 tag 已发布且源码相同则跳过；同一 tag 已存在但源码不同则失败，需要递增 `patches/revision`。**维护 workflow、发布脚本、回归脚本和 `FORK.md` 也属于发布源码；修改这些文件后要发布新版本，同样需要递增 revision。** 不会强推或移动旧 tag，也不会覆盖已发布 release 的附件。GitHub 的按 tag 查询接口只返回正式 release；流程改用已认证的 release 列表定位草稿，创建时直接取得 release ID，后续按 ID 检查附件及草稿状态。中断留下的 draft 可以用相同产物重试：若已有 `build-info.json`，流程先校验其附件摘要，并核对除运行链接以外的全部构建信息（包括源码、签名证书及 APK 摘要）。仅运行链接不同则保留首次构建的原始元数据，重建对应校验清单；所有已有附件仍须摘要一致，流程不会覆盖它们。新一次构建不保证 APK 字节完全相同，若 APK 或其他构建信息变化，仍需人工检查并处理草稿；不能把不同产物混入同一版本。

上游每次发布新稳定版会自动重放当前补丁。上游修复本问题、修改清理路径或改变构建声明时，补丁/检查可能不再适用；这时应审查失败日志并更新或删除相应 patch，不能绕过验证。

## 首次启用

先在自己的账号下 fork 仓库，将本次维护提交放到 fork 的**默认分支**。在 fork 的 Actions 页面启用 workflows，然后设置下述三个 repository secrets。默认使用 workflow 自带的 `GITHUB_TOKEN`，只有发布 job 拥有 `contents: write`；此流程没有配置额外 GitHub App 或长期 PAT。

GitHub 对新 ref 的 workflow 文件还会做权限检查。因此发布源码携带维护分支的完整 workflow 目录，并核对路径、内容和文件模式一致；官方文档说明，同仓库另一分支已存在相同路径和内容的 workflow 时，可不增加 workflow scope。这里不导入新的上游 workflow 权限，也不请求额外 PAT。首次远端运行仍需确认实际 tag push；若仓库规则或权限检查明确拒绝，流程会在创建公开 release 前停止，不会自行扩大凭据权限，也不会把 tag 改指向未经该次编译的维护 commit。

| Secret | 内容 |
| --- | --- |
| `APK_SIGNING_KEY` | 长期使用的 PKCS12 keystore 文件的 Base64 内容 |
| `APK_SIGNING_PASSWORD` | keystore 和私钥的密码（PKCS12 使用同一密码） |
| `APK_SIGNING_ALIAS` | keystore 中的私钥别名，例如 `vpnhotspot` |

首次在自己的安全环境生成密钥，例如：

```bash
keytool -genkeypair -storetype PKCS12 \
  -keystore vpnhotspot-release.p12 \
  -alias vpnhotspot -keyalg RSA -keysize 3072 \
  -validity 10000 -dname "CN=VPNHotspot fork"
```

该命令交互式询问密码。保管并备份 keystore 和密码，**不要提交到仓库、上传为公开附件或粘贴进 issue/聊天**。已登录 GitHub CLI 时，可在替换 `OWNER/VPNHotspot` 后设置 secrets：

```bash
python3 -c 'import base64, pathlib, sys; sys.stdout.buffer.write(base64.b64encode(pathlib.Path("vpnhotspot-release.p12").read_bytes()))' \
  | gh secret set APK_SIGNING_KEY --repo OWNER/VPNHotspot
gh secret set APK_SIGNING_PASSWORD --repo OWNER/VPNHotspot
gh secret set APK_SIGNING_ALIAS --repo OWNER/VPNHotspot --body vpnhotspot
gh workflow enable patched-release.yml --repo OWNER/VPNHotspot
gh workflow run patched-release.yml --repo OWNER/VPNHotspot
```

密码命令同样采用交互输入，不在命令行参数中写密码。后续发布必须继续使用同一把密钥；流程会与 fork 上一份正式 release 的证书摘要比较，发现变化就停止。缺少 secrets 时会明确失败，没有临时 debug key 的替代路径。签名私钥只写入发布 runner 的临时目录并在签名步骤结束时删除，不进入构建 artifacts。验签同时兼容旧版 `Signer #1` 与 Build Tools 37 的 `V3.0 Signer:` 证书输出；要求恰好一个签名者、各签名 scheme 证书一致，并核对四个 ABI 使用同一证书。

默认分支上的 patch、发布脚本、回归脚本、完整 workflow 目录或 `FORK.md` 变更会触发 workflow；也可以手动运行。定时检查为每日 **UTC 03:23 / 台北时间 11:23**。GitHub 可能延迟定时任务；公开仓库 60 天没有活动时还会自动停用 schedule，需要重新启用。此流程不通过空提交强行保活。

## 安装与验证范围

应用包名仍为 `be.mygod.vpnhotspot`，签名属于此 fork。因此**首装本 fork 不能直接覆盖官方版**。请先备份需要保留的设置，关闭 VPN 共享，再处理官方版的安装；卸载会清除应用数据。安装本 fork 后授予 root，并在需要时执行“设置 → 清理/重新应用路由规则”。以后本 fork 的新版本使用相同签名且递增版本号，可正常更新。

release 提供 `arm64-v8a`、`armeabi-v7a`、`x86`、`x86_64` 四个独立 APK；大多数较新手机使用 `arm64-v8a`，以设备实际 ABI 为准。

CI 的真实 netfilter 测试在 GitHub Linux runner 的隔离网络命名空间中运行。没有 root、网络命名空间权限或 iptables 后端时测试失败，不能跳过后仍算验证通过。Linux runner 的通过结果也不等于已经验证所有 Android ROM/内核，用户设备上的热点连通性仍需实测。

**准备记录（2026-11-09，首轮远端验证前）：** 当时尚未执行远端 GitHub Actions，也未发布 APK。这是初次准备阶段的历史记录；当前落地状态请以实际成功的 workflow run 及其 release 链接为准。

## 延期事项

- 用户设备的 ROM/内核及实际热点流量验证：云端没有该设备，需要首版安装后确认；保留调试导出作为仍失败时的诊断依据。
- 首次远端发布的 tag 权限：已通过 GitHub CLI 创建 fork 并配置长期签名 secrets；仍需以首次成功的发布 job 确认 `GITHUB_TOKEN` 对本 fork 生成的源码 tag 通过 GitHub 的 workflow 文件检查。
- 若上游稳定版长期不更新，GitHub 的 60 天 schedule 停用需要人工重新启用；暂不增加外部定时服务或自动空提交。

## 参考

- [上游 v3.0.8](https://github.com/Mygod/VPNHotspot/releases/tag/v3.0.8) 与 [上游构建 workflow](https://github.com/Mygod/VPNHotspot/blob/v3.0.8/.github/workflows/test.yml)
- [GitHub 的触发事件与 fork/schedule 限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [GitHub GITHUB_TOKEN](https://docs.github.com/en/actions/concepts/security/github_token) 与 [secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)
- [GitHub workflow scope 与已存在 workflow 文件的规则](https://docs.github.com/en/enterprise-cloud@latest/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)
- [Android 签名与升级](https://developer.android.com/studio/publish/app-signing) 与 [apksigner](https://developer.android.com/tools/apksigner)

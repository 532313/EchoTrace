# EchoTrace 开发流程

本仓库以 Echo-Mate 上可直接操作的完整“记忆”App 为交付目标。功能范围见 [开发任务](docs/roadmap.md)，部署与发布见 [版本发布](docs/releases.md)。

## 一次功能改动

1. 从 `main` 建立短期分支：`feat/ET-002-projects`、`fix/media-replay`、`chore/development-workflow`。一个分支解决一个可描述的用户问题。
2. 先写清入口、操作和可见结果，再完成代码。根据改动运行相关编译和必要检查；文档改动不重复硬件长测。
3. 审阅 `git diff`，按完整的小步骤提交。提交说明使用 `feat(app): add project creation` 等格式；禁止把模型、录像、运行数据和凭据提交进仓库。
4. 在 PR 中记录功能效果、验证范围、剩余限制和回退方法。CI 通过后合并到 `main`，删除已合并分支。
5. 准备发布时更新版本和 CHANGELOG，生成带提交摘要和 SHA-256 的发布包；板端安装记录必须能找到对应提交和上一版产物。

初始化当前仓库的开发设置：

```sh
sh scripts/setup_dev.sh
git switch main
git switch -c feat/ET-002-projects
git add 路径
git diff --cached
git commit -m 'feat(app): add project creation'
```

`setup_dev.sh` 只配置本仓库的 hooks、换行和 fast-forward pull 策略。作者身份使用你自己的姓名与邮箱。提交 hook 检查暂存区字节，不会自动改写代码，也不运行整套硬件测试。

远程仓库尚未接入时，在本地用 `git diff main...分支名` 自审、记录本次检查后，以 `git merge --ff-only 分支名` 合并。此时应明确记录“本地合并，尚无远程 PR/CI”；接入远端后使用 PR 流程。

## 版本与分支

- `main` 保存已完成并经过本次改动所需检查的代码；功能开发使用 `feat/*`，修复使用 `fix/*`，流程维护使用 `chore/*`。当前项目采用短期功能分支，不额外维护长期 `develop` 分支。
- 产品版本唯一来源是 `src/echotrace/__init__.py` 的 `__version__`。Python 包、API、原生程序与 Buildroot 包都从这里取值。协议版本和数据库版本独立演进。
- `0.2.0.devN` 是开发版，`0.2.0rcN` 是候选版，`0.2.0` 才是完整小屏流程的正式版本。修复发布递增 patch，新增兼容能力递增 minor。
- Git 标签指向固定提交，使用 `v版本号`；不移动已经发布的标签。`v0.1.0-baseline.1` 是导入历史代码的基线快照，不表示完整 App 已交付。
- 已共享历史的回退使用 `git revert` 后重新发布；不通过改写历史掩盖问题。

## CI 与评审

GitHub Actions 配置包含 `source-policy` 和 `portable-build` 两项。前者检查源码、凭据与版本规则，后者构建主机采集端并执行已有可移植检查。云端无法验证真实摄像头、音频设备或 NPU，这些结果在对应硬件功能交付时记录。

远端仓库接入后，为 `main` 设置必须经 PR 合并、两项 CI 通过、讨论已解决，并禁止 force push 和分支删除。单人开发可在 PR 留下自审记录；有其他维护者时再启用其实际评审，不填写虚假评审结果。远程规则必须在 GitHub 实际设置后才能称为已生效。

工作流使用只读默认权限，Action 固定到完整提交摘要，依据 [GitHub 的 Action 固定版本说明](https://docs.github.com/en/actions/reference/security/secure-use#using-third-party-actions)。带标签的发布任务只创建草稿，正式发布前查看功能说明和附件。

## 工作区与上板

Mac 是当前源码仓库；WSL 负责 RV1106 构建，鲁班猫负责 RK3576 构建与运行。两边后续使用 `git clone/fetch/checkout` 同一提交，避免只复制几个文件却遗漏依赖。SDK、原 Echo-Mate、模型和运行数据继续保存在仓库之外。

完整源码备份可使用 `git bundle`；它保存提交、分支和标签，但不包含被忽略的模型、录像和 SDK。实际应用安装须正常结束录制，保存旧二进制、配置和数据迁移备份，再替换新产物。每次部署记录产品版本、完整 Git 提交、产物摘要、板型和恢复命令。

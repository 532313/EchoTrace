# 版本、产物和回退

当前 `0.2.0.dev0` 用于完整小屏 App 的开发。已存在的 `v0.1.0-baseline.1` 标签保存接入 Git 前的源码基线。源码提交与板端正在运行的手动安装版本是两件事；本次建立 Git 流程不会自动更新开发板。

## 准备版本

在功能分支完成本次改动所需检查、更新功能说明和剩余限制。准备候选版时执行：

```sh
python3 scripts/release.py bump 0.2.0rc1
# 编辑 CHANGELOG.md，新增 [0.2.0rc1] 条目和实际完成的功能。
git add src/echotrace/__init__.py CHANGELOG.md
git commit -m 'chore(release): prepare 0.2.0rc1'
```

合并后在干净的 `main` 上执行：

```sh
python3 scripts/release.py check --tag v0.2.0rc1
git tag -a v0.2.0rc1 -m 'EchoTrace 0.2.0rc1'
git push origin main
git push origin v0.2.0rc1
```

示例版本不代表已经发布。发布任务校验标签、产品版本和 CHANGELOG 一致，然后创建包含源码包与摘要的 GitHub 草稿 Release。云端生成的是源码包，不冒充 RV1106 固件或经过板端确认的安装包。

## 本地源码包

```sh
python3 scripts/release.py source --ref HEAD
```

输出位于 `build/releases/`，默认文件名包含版本和提交摘要：源码 `.tar.gz`、`.sha256` 和 `.manifest.json`。打包只使用指定 Git 提交中的文件，拒绝脏工作区和覆盖已有产物；压缩包内也有提交清单。相同提交生成的内容固定，不混入当前工作区的录像、模型或临时依赖目录。依赖由已提交的版本锁和准备脚本恢复，设备构建另需相应厂商 SDK。

原 `scripts/package_sources.py --output 路径` 入口仍可使用，现委托给同一 Git 打包流程；它不再捎带整个 `build/vendor` 或 `build/deps`。

## 设备产物与部署记录

从干净提交构建后，立即为产物生成清单，例如：

```sh
python3 scripts/release.py artifact --target rv1106 --component edge \
  --file build/rv1106/echotrace-edge --toolchain 'GCC 8.3.0 / uClibc / Echo SDK' \
  --output build/releases/rv1106-edge.manifest.json
```

清单包含产品版本、完整源码提交、目标平台、工具链说明、每个产物的大小与 SHA-256，以及模型和第三方依赖锁摘要。开发调试可显式使用 `--allow-dirty`，清单会标为 dirty；这种产物不进入正式发布。

发布包在 WSL/RK3576 上按清单构建，记录实际编译器和运行时版本。安装前保留上一版二进制和配置；涉及数据库变化时先备份数据目录。上板后记录版本与用户可见功能，出问题时恢复上一版安装，并用 `git revert` 创建后续修复版本。原始录像不通过 Git 回退或删除。

## 离线保存仓库

```sh
git bundle create build/echotrace-history.bundle --all
git bundle verify build/echotrace-history.bundle
git clone build/echotrace-history.bundle /新的目录/EchoTrace
```

远程 URL 未设置时可以完成本地提交、标签和 bundle；远程推送、PR、分支保护与云端 CI 则需要实际远程仓库和有效认证。

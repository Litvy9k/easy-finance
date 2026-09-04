# 简账独立 CI/CD

仓库：`Litvy9k/easy-finance`。工作流：`.github/workflows/deploy.yml`。

只有管理员完成服务器权限配置，并将仓库 Actions Variable `EF_DEPLOY_ENABLED` 设为 `true` 后，自动发布才会启用；未启用时只运行 CI 测试。

- 向 `main` 推送：运行隔离测试；成功后自动发布。也支持 Actions 页面手动运行。
- Pull Request：仅测试，不提供部署密钥、不发布。
- GitHub 托管 runner 构建测试；不在这台 4 GB VPS 上安装常驻 runner。
- Actions 部署并发组不取消正在发布的任务；服务器还使用独立文件锁防止重叠。
- 官方 Actions 固定到已核实的提交 SHA；服务器主机密钥固定，不使用未经核实的 ssh-keyscan 结果。

## 与主站隔离

简账 CI 不操作 `/var/www/dope-website`、`/etc/nginx`、主站证书或主站仓库。

| 用途 | 服务器位置 |
| --- | --- |
| 初始版本，保留作为退路 | `/opt/easy-finance` |
| CI 版本目录 | `/opt/easy-finance-releases/<commit>-<随机后缀>` |
| 当前版本指针 | `/opt/easy-finance-current` |
| 数据库与图片，不随发布覆盖 | `/var/lib/easy-finance` |
| 密码/签名密钥，不进入 Git 或 CI | `/etc/easy-finance.env` |
| 每次发布前的数据库快照 | `/var/backups/easy-finance` |

通过 `easy-finance.service.d/ci.conf` 让**简账服务**使用当前版本指针，不修改基础 service 文件。只在候选版本准备完毕后短暂停止简账、备份数据库、切换版本并启动；主站不重启。

## 受限部署权限

`ef-deploy` 的独立 SSH 公钥带有 forced-command 和 restrict 约束，不允许交互式 Shell、PTY 或端口转发。只接受 `deploy <40 位 commit SHA>`，部署包从 SSH 标准输入接收，不提供任意 SCP/rsync 上传权限。

该账号仅可免密码调用 root 所有的 `/usr/local/sbin/ef-ci-deploy`。管理器校验归档大小、路径、类型和版本号，拒绝越界路径、符号链接、重复文件、密码和数据库；依赖安装及代码检查均以非 root 的 ef-deploy 执行，实际应用仍以 finance 运行。发布后文件归 root 所有。

**拥有仓库写权限或部署私钥意味着拥有简账程序发布权限，进而可能读取简账数据。** 它不等于普通只读权限，应保护 GitHub 账号并启用双因素认证。

`deploy/ci/` 中的特权管理脚本只供管理员审查后安装，不包含在 CI 部署包里，也不能由 CI 自动替换。

## GitHub Actions Secrets

只配置在简账仓库，主站的同类设置不变：

- `EF_HOST`：服务器地址。
- `EF_SSH_KEY`：新生成的简账专用部署私钥；不是用户个人 SSH 私钥或 root 登录私钥。
- `EF_KNOWN_HOSTS`：已经通过可信控制台核实过的服务器公钥条目。

本机 `.deploy/`、账本、上传图片、ZIP 包、环境文件和私钥均由 `.gitignore` 排除。不要使用 `git add -f` 将其加入仓库。

## 回滚与恢复

发布成功同时要求内网及真实 HTTPS `/ef/health` 返回本次 commit。失败会自动恢复先前代码指针并重启简账；**不会自动恢复数据库**，以免删除新数据。数据库结构变更需兼容旧版本；不兼容迁移必须单独安排备份和恢复。

数据库快照在短暂停止应用期间制作，包含当时存在的 SQLite WAL/SHM 文件；图片不包含在这些发布快照中，仍需另行定期备份。没有定时备份、没有自动删除历史版本或快照；空间不足 1 GiB 时管理器拒绝发布，管理员应在确认不影响当前版本/所需备份后清理。

手工回滚时，管理员先检查 `readlink -f /opt/easy-finance-current` 和历史版本，停止简账后将指针切回所选版本，再启动并验证 `/ef/health`。不要覆盖数据库、环境文件或主站目录。

## 密码修改

修改服务器 `/etc/easy-finance.env` 中的 `APP_PASSWORD` 后执行 `systemctl restart easy-finance`。若需撤销现有登录 Cookie，同时更换 `SECRET_KEY`。代码发布不会重置密码。

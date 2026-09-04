# 实际部署记录

部署日期：2026-09-04。目标：`https://l9k.dev/ef/`。

- 服务器：`107.149.92.201`，Debian 13 x86_64，Python 3.13。
- 运行账号：`finance`，服务：`easy-finance.service`，已设置开机自启。
- 监听：仅 `127.0.0.1:8001`；公网通过现有 Nginx HTTPS 接入。
- 当前代码指针：`/opt/easy-finance-current`；CI 版本存放于 `/opt/easy-finance-releases/`，初始版本 `/opt/easy-finance/` 保留。
- 数据：`/var/lib/easy-finance/easy_finance.db`，图片：`/var/lib/easy-finance/uploads/`（首次上传时创建）。
- 密码/签名密钥：`/etc/easy-finance.env`，root 所有，权限 600。密码不写入本文或 Git。
- 按用户选择使用空白账本，没有上传本机数据库或图片。

## 主站保护

主站 `/var/www/dope-website/` 内容未修改，部署前后完整文件清单校验值一致。现有 `/etc/nginx/conf.d/dope-website.conf` 只在 `l9k.dev` 的 HTTPS server 中增加：

```nginx
include /etc/nginx/snippets/easy-finance.conf;
```

另增加 `/etc/nginx/snippets/easy-finance.conf` 和 `/etc/nginx/conf.d/easy-finance-limits.conf`。原证书、主站路由、ACME 校验及 www 跳转均保留。原 Nginx 配置完整备份在服务器私有目录：

`/root/easy-finance-deploy.Rlxnnu/nginx-backup/`

备份包含服务器配置和证书文件，父目录只允许 root 访问，不应公开或放进网站目录。若主站自己的发布流程会覆盖 Nginx 配置，须在其配置源中保留上述 include。

## 日常管理

登录服务器后：

```bash
systemctl status easy-finance --no-pager
journalctl -u easy-finance -n 100 --no-pager
systemctl restart easy-finance
```

修改应用登录密码：用编辑器修改 `/etc/easy-finance.env` 中的 `APP_PASSWORD`，然后重启服务。若还要让已有登录 Cookie 失效，请同时替换 `SECRET_KEY` 为新的长随机值。不要把私钥、应用环境文件或账本发到聊天中。

更新方式：向 `Litvy9k/easy-finance` 的 `main` 分支推送，由独立 GitHub Actions 测试后发布；配置说明见 [CI-CD.md](CI-CD.md)。专用 `ef-deploy` 账号只能通过固定脚本发布简账，不能使用该密钥登录普通 Shell。主站使用自己的仓库和工作流，不受简账发布影响。

发布保留 `/var/lib/easy-finance/` 和 `/etc/easy-finance.env`。每次切换版本前会生成数据库快照至 `/var/backups/easy-finance/`，不包含图片；当前没有设置定时备份任务。完整数据备份方式见 `deploy/README.md`。

## 已验证

- 在服务器以 finance 用户执行 `/ef` 下的 8 项隔离测试，全部通过；不操作线上账本。
- 公网 HTTPS 登录、Secure/HttpOnly/Cookie Path=/ef/、静态资源、设置、空 CSV、退出后鉴权通过。
- 公网 OCR 合成图片识别成功，测试耗时约 1.72 秒（包含网络和冷启动，仅代表该测试图片）。
- 主站 HTTP 200，首页及全站静态文件校验值未改变。
- 应用服务运行账号 finance，数据库权限 600，数据目录权限 700，端口不直接暴露公网。

本机首次登录信息单独保存在项目 `.deploy/login.txt`（已由 `.gitignore` 排除），不要将其提交或公开。

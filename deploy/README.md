# VPS 部署：l9k.dev/ef

不使用 Docker。Nginx 保留个人网站，只把 `/ef/` 转发到本机 `127.0.0.1:8001`；systemd 负责后台运行和开机启动。访问 `/ef` 会跳转到 `/ef/`。

以下示例以 Ubuntu 24.04 + Python 3.12 + Nginx 为基准。如果你用的是宝塔、1Panel、Caddy、其他系统或托管面板，先确认现有网站配置位置，不要直接覆盖它。本文没有操作任何远程服务器。

## 1. 准备代码和运行环境

域名 A/AAAA 记录应指向你的服务器；现有 `https://l9k.dev` 应已正常访问。财务登录必须使用 HTTPS。防火墙仅开放所需的 SSH、80、443；不开放 8001。

在服务器上安装运行环境（已安装的 Nginx 不必重装）：

```bash
sudo apt update
sudo apt install python3-venv python3-pip libodbc2 libgl1 libglib2.0-0t64 nginx
sudo useradd --system --user-group --home-dir /var/lib/easy-finance --no-create-home --shell /usr/sbin/nologin finance
sudo install -d -m 755 /opt/easy-finance
```

`finance` 用户已存在时跳过 useradd。把本项目的 `app/`、`requirements.txt`、`deploy/` 上传到 `/opt/easy-finance/`，不要上传 Windows `.venv`、`.env`、旧 Azure ZIP 或数据库。该目录不能放在个人网站可直接下载的静态目录中。

```bash
cd /opt/easy-finance
sudo python3 -m venv .venv
sudo .venv/bin/python -m pip install -r requirements.txt
sudo install -m 600 deploy/easy-finance.env.example /etc/easy-finance.env
sudoedit /etc/easy-finance.env
```

必须替换 `APP_PASSWORD` 和 `SECRET_KEY`。可以用以下命令生成随机值，分别生成，不要使用示例占位符：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

配置文件由 systemd 读取，不是 Shell 脚本；不用 `export`。含空格的值用双引号包围。保持 `APP_ROOT_PATH=/ef`（结尾不加 `/`）和 `COOKIE_SECURE=true`。

## 2. 启动后台服务

```bash
sudo install -m 644 deploy/easy-finance.service /etc/systemd/system/easy-finance.service
sudo systemctl daemon-reload
sudo systemctl enable --now easy-finance
sudo systemctl status easy-finance --no-pager
curl -f http://127.0.0.1:8001/health
```

健康检查应返回 `{"status":"ok"}`。启动命令已写入 service 文件，无需 Azure Startup Command，也不要在生产使用 `--reload`。只启动一个 worker，适合当前单用户应用，避免多个进程重复加载 OCR 模型。

systemd 自动创建并授权 `/var/lib/easy-finance/`，应用使用：

- 数据库：`/var/lib/easy-finance/easy_finance.db`
- 图片：`/var/lib/easy-finance/uploads/`
- 代码：`/opt/easy-finance/`（运行用户只读）

这是新部署的本地存储方案，不会自动迁移现有 SQLite、Azure SQL 或 Blob 数据。若仍要用 Azure 数据服务，保留对应 `DATABASE_URL`/存储连接字符串；Azure SQL 还需另行安装 Microsoft ODBC 驱动。

## 3. 将路径接入现有网站

```bash
sudo install -m 644 deploy/nginx-finance.conf /etc/nginx/snippets/easy-finance.conf
sudo install -m 644 deploy/nginx-finance-limits.conf /etc/nginx/conf.d/easy-finance-limits.conf
```

编辑**现有** `l9k.dev` HTTPS 的 `server { ... }`，在里面加入：

```nginx
include /etc/nginx/snippets/easy-finance.conf;
```

保留个人网站的 `location /`、证书和 80 → HTTPS 跳转。不要把该片段作为独立站点配置，也不要加在 `http {}` 顶层。若已有 `/ef` 路由，先合并而非重复声明。`^~ /ef/` 可避免个人网站的通用 CSS/JS 正则路由截走记账资源。

```bash
sudo nginx -t
# 只有语法检查成功后再执行：
sudo systemctl reload nginx
```

访问 `https://l9k.dev/ef/`：未登录应跳转到 `/ef/login`。登录后检查记账、设置、图片查看、编辑、删除和 CSV 导出；再检查 `https://l9k.dev/` 个人网站未受影响。

Nginx 的 `proxy_pass` 结尾 `/` 不能省略。本方案去掉请求前缀再转发，应用用 `APP_ROOT_PATH` 为链接、跳转、Cookie 和 OCR 请求补上前缀。systemd 同时将该值传给 Uvicorn 的 `--root-path`，保证静态资源挂载正确；手动启动时也要传 `--root-path /ef`，仅设置环境变量不够。参见 [FastAPI 反向代理说明](https://fastapi.tiangolo.com/advanced/behind-a-proxy/) 和 [Nginx proxy_pass 文档](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)。若 Nginx 前还有 CDN/代理，需按其配置额外校准真实 IP、HTTPS 和缓存规则，不要缓存 `/ef/*`。

## 4. 迁移和备份

上传代码不会带上你的现有账本。若要迁移本地 SQLite：先停止本地记账进程，再复制 `easy_finance.db` 和整个 `uploads/`；服务器上也要停止 `easy-finance` 后，把它们放入上述数据目录，确认文件归 `finance:finance`，再启动。**替换任何已有数据库之前先备份，不能直接覆盖已有账本。** 仅 CSV 不包含原始图片，也不能替代完整备份。

最简单的一致性备份是短暂停服务，完整备份 `/var/lib/easy-finance/` 后再启动；备份应保存在站点目录之外并定期下载到另一台设备，包含私人财务信息，注意权限和加密。不要把数据目录随代码更新删除。

后续更新代码/依赖后执行 `sudo systemctl restart easy-finance`。排障：

```bash
sudo journalctl -u easy-finance -n 100 --no-pager
sudo nginx -t
```

## 5. OCR 和安全边界

已移除 Azure OCR，使用 [RapidOCR](https://rapidai.github.io/RapidOCRDocs/main/install_usage/rapidocr/usage/) 3.9.2 + ONNX Runtime CPU 推理，随 Python 包安装的 PP-OCRv6 small 检测/识别和移动版方向分类模型合计约 30 MiB。无需 GPU、密钥或运行时下载模型，上传图片不发送到第三方。首次使用会在内存中加载模型，之后复用；模型文件大小不等于运行内存占用。4 核 4 GB VPS 建议保持单 worker；当前限制 OCR 一次处理一张、CPU 线程数 2，并缩小大图。超过 2500 万像素的图片会被拒绝，请先缩小；HEIC 若不能解码，请转成 JPG/PNG/WebP。

依赖安装后可在服务器验证模型（无需数据库、不会上传图片）：

```bash
cd /opt/easy-finance
sudo .venv/bin/python -c 'from app.ocr import get_engine; get_engine(); print("Local OCR ready")'
```

新增 `libgl1`/GLib 是 OpenCV 的 Linux 运行依赖；上述 GLib 包名对应 Ubuntu 24.04，其他系统需使用对应包名。OCR 初始化失败时网页会解锁并提示错误，细节见 systemd 日志。更新依赖后重启服务。旧 Azure OCR 环境变量可以删除，不再使用。

“图转文”处理期间显示无法关闭的等待层，阻止页面点击、编辑和提交；文字成功追加到备注末尾后解锁，失败或 90 秒请求超时也会解锁。超时只停止浏览器等待，后台正在进行的推理可能仍会完成；此时再次 OCR 会提示忙碌。保存记录锁定到更新后的明细页面加载，错误时保留表单并解锁。新增记录用数据库中的 `transaction_submissions` 唯一标识防重，重复提交同一表单不会重复记账；该表启动时自动创建，无需修改历史记录。不要清空该表，否则旧请求将失去防重保护。

Cookie 限定为 `/ef/` 并启用 HttpOnly、Secure、SameSite=Lax；这不是跨应用的安全隔离。主网站与记账同源，主网站脚本或 XSS 可影响记账应用，因此不要在主站加载不可信脚本；需要隔离时应改用独立子域名。Nginx 模板对登录 POST 按 IP 限速（每分钟 5 次、允许小幅突发），超限返回 429；限速配置文件必须位于 `http` 作用域。应用本身没有账户锁定功能。

## 本地回归测试

在 Windows 项目目录执行（无需 Azure，测试只使用临时数据库/图片）：

```powershell
.venv\Scripts\python tests/test_deployment.py
.venv\Scripts\python tests/test_deployment.py --root-path /ef
```

另有桌面/手机尺寸的浏览器测试（需要单独安装测试依赖 `playwright` 和本机 Edge，不需要装到生产服务器）：

```powershell
.venv\Scripts\python -m pip install playwright
.venv\Scripts\python tests/test_browser.py
```

测试涵盖本地离线 OCR、识别失败/超时解锁、备注追加、等待层阻止 Esc、编辑弹窗、连续提交、并发请求以及网络重试防重。测试过程中不使用真实账本。

普通本地 UI 测试保持 `APP_ROOT_PATH` 为空、`COOKIE_SECURE=false`，运行 `.venv\Scripts\uvicorn app.main:app --reload` 并访问 `http://127.0.0.1:8000/`。不要把生产环境的安全 Cookie 设置用于本地 HTTP。

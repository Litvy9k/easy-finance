# 简账（FastAPI）

面向单人、移动端优先的网页记账应用。支持密码 Cookie 登录、收支记录、自定义资金来源、月度统计、凭证图片、可选 OCR，以及全量 CSV 导出。

## 当前部署方案：个人 VPS

目标地址为 `https://l9k.dev/ef/`。已提供 Nginx 子路径反向代理、systemd 服务和环境变量模板，无需 Docker。完整步骤见 [VPS 部署指南](deploy/README.md)。

日常更新使用独立的 [GitHub Actions CI/CD](deploy/CI-CD.md)：向 `main` 推送，测试通过后仅发布简账，不修改个人主站。账本、图片和密码与代码分开保存。

设置 `APP_ROOT_PATH=/ef` 启用子路径；留空时仍可在本地根路径运行。`UPLOAD_DIR` 可单独指定图片持久化目录。VPS 模板使用 SQLite 和服务器本地图片，不自动迁移旧数据；Azure SQL/Blob 配置仍兼容，以下内容保留供需要时使用。

OCR 使用 RapidOCR + ONNX Runtime CPU 版及随包安装的小型模型，图片不发送到云端，不再连接 Azure OCR。安装/更新 `requirements.txt` 后即可使用图片旁的“图转文”；识别期间页面锁定，成功后换行追加备注，失败或超时解锁并显示错误。保存记录期间也锁定页面，更新记录页面后恢复；新增记录通过数据库唯一提交标识防止网络重试重复入账。

## 本地运行

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
$env:APP_PASSWORD="your-password"
$env:SECRET_KEY="a-long-random-secret"
.venv\Scripts\uvicorn app.main:app --reload
```

打开 `http://127.0.0.1:8000`。未配置 Azure 时，数据保存在 `easy_finance.db`，图片保存在 `uploads/`。

## Azure 环境变量

在 Web App 的“环境变量 / 应用设置”中配置：

- `APP_PASSWORD`：登录密码（必须修改）
- `SECRET_KEY`：用于签名 Cookie 的长随机字符串（必须修改）
- `COOKIE_SECURE=true`
- `DATABASE_URL`：Azure SQL 的 SQLAlchemy URL，例如：
  `mssql+pyodbc://USER:PASSWORD@SERVER:1433/DATABASE?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no`
- `AZURE_STORAGE_CONNECTION_STRING`：Blob Storage 连接字符串
- `AZURE_STORAGE_CONTAINER=receipts`（可选）
- `MAX_UPLOAD_MB=8`（可选）

若密码含 `@`、`:`、`/` 等字符，需要先做 URL 编码。Azure SQL 防火墙需允许 Web App 出站访问；Linux Web App 应选择带有 Microsoft ODBC Driver 18 的 Python 运行时。

## 不使用 Docker 的 ZIP 部署

1. 创建 Linux Azure Web App，运行时选择 Python 3.11/3.12。
2. Startup Command 设置为：`bash startup.sh`。
3. 将项目文件压缩（确保 `app/`、`requirements.txt` 位于 ZIP 根目录），通过 Deployment Center 或 Azure CLI ZIP Deploy 上传。
4. 启用构建自动化：应用设置加入 `SCM_DO_BUILD_DURING_DEPLOYMENT=true`。
5. 配置上述环境变量，然后重启应用并访问 `/health` 检查状态。

示例压缩命令（不要把本地数据库、上传目录和虚拟环境打包）：

```powershell
Compress-Archive -Path app,requirements.txt,startup.sh,README.md -DestinationPath easy-finance.zip -Force
```

> 当前推荐部署到自己的 VPS，步骤见上面的部署指南；OCR 已改为本地 CPU 推理。

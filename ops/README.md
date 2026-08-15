# 运维脚本

## 本地启动

`dev-compose.ps1` 是受支持的 Docker Compose 启动入口。脚本会先校验 Compose
配置，默认重新构建镜像并启动服务，然后在 30 秒内轮询 `/api/ping`。

```powershell
powershell -ExecutionPolicy Bypass -File ops/dev-compose.ps1
```

已有最新镜像时可跳过构建：

```powershell
powershell -ExecutionPolicy Bypass -File ops/dev-compose.ps1 -NoBuild
```

## 隔离验证

`verify.ps1` 使用系统临时目录运行后端测试、Python 编译、前端测试和生产构建，
不会读取或修改 G 盘数据。缺少 `node_modules` 时自动执行 `npm ci`；使用
`-InstallFrontend` 可强制重新安装依赖。

```powershell
powershell -ExecutionPolicy Bypass -File ops/verify.ps1 -InstallFrontend
```

应用数据和运行状态必须位于仓库外，并通过基于 `.env.example` 创建的本地
`.env` 配置。`host/` 仅包含可选的主机管理脚本，执行前必须单独审查。

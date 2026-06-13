# deploy.ps1 — 一键重新构建并部署
Write-Host "=== Binance AI Trader 部署脚本 ===" -ForegroundColor Cyan

Write-Host "[1/3] 构建前端..." -ForegroundColor Yellow
Set-Location frontend
npm run build
if ($LASTEXITCODE -ne 0) { Write-Host "前端构建失败" -ForegroundColor Red; exit 1 }
Set-Location ..

Write-Host "[2/3] 构建 Docker 镜像..." -ForegroundColor Yellow
docker compose build
if ($LASTEXITCODE -ne 0) { Write-Host "Docker 构建失败" -ForegroundColor Red; exit 1 }

Write-Host "[3/3] 启动容器..." -ForegroundColor Yellow
docker compose up -d

Write-Host ""
Write-Host "部署完成！访问 http://localhost" -ForegroundColor Green
Write-Host "查看日志: docker compose logs -f" -ForegroundColor Gray

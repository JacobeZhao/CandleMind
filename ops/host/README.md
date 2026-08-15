# 主机管理

`setup_docker_mirror.sh` 是面向中国网络环境的可选 Docker 镜像源初始化脚本，
不属于 CandleMind 的正常启动流程。脚本仅在目标文件不存在时创建 daemon
配置，不会覆盖现有配置，也不会重启 Docker。

执行前必须检查目标主机、镜像源和目标路径：

```sh
sudo sh ops/host/setup_docker_mirror.sh /etc/docker/daemon.json
```

执行后请先验证 JSON，再使用目标系统的服务管理器重新加载 Docker。不要在未
备份现有 daemon 配置的生产主机上直接运行。

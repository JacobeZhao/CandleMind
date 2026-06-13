#!/bin/sh
cat > /run/config/docker/daemon.json << 'EOF'
{
  "registry-mirrors": [
    "https://docker.mirrors.ustc.edu.cn",
    "https://mirror.baidubce.com"
  ]
}
EOF
echo "Written to /run/config/docker/daemon.json:"
cat /run/config/docker/daemon.json
kill -HUP 165 && echo "Sent SIGHUP to dockerd (PID 165)"

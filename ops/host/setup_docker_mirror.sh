#!/bin/sh
set -eu

config_file="${1:-/etc/docker/daemon.json}"

if [ -e "$config_file" ]; then
    echo "Refusing to overwrite existing Docker configuration: $config_file" >&2
    echo "Merge the registry-mirrors setting manually." >&2
    exit 1
fi

mkdir -p "$(dirname "$config_file")"
cat > "$config_file" << 'EOF'
{
  "registry-mirrors": [
    "https://docker.mirrors.ustc.edu.cn",
    "https://mirror.baidubce.com"
  ]
}
EOF

echo "Wrote $config_file"
echo "Validate the configuration, then reload Docker with the host's service manager."

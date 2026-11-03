#!/usr/bin/env bash
# Testing-only Docker installation for the existing Ubuntu 20.04 host.
# No OS upgrade, Docker TCP socket, firewall mutation or public port exposure.
set -euo pipefail
if [[ ${EUID} != 0 || $# != 3 ]]; then
  echo 'Usage (root): install_m1_runtime.sh BOOTSTRAP_DIR DOCKER_SHA256 COMPOSE_SHA256' >&2
  exit 2
fi
bundle_dir=$(realpath "$1")
runtime_dir=/opt/video-ai/runtime
if [[ "$bundle_dir" != /opt/video-ai/bootstrap || -e "$runtime_dir" || -e /etc/systemd/system/video-ai-docker.service ]]; then
  echo 'Unexpected bootstrap directory or runtime already present; inspect before replacing.' >&2
  exit 2
fi
[[ "$2" =~ ^[0-9a-f]{64}$ && "$3" =~ ^[0-9a-f]{64}$ ]]
printf '%s  %s\n' "$2" "$bundle_dir/docker-29.9.1.tgz" | sha256sum -c -
printf '%s  %s\n' "$3" "$bundle_dir/docker-compose-linux-x86_64" | sha256sum -c -
install -d -m 0755 "$runtime_dir/bin" "$runtime_dir/cli-plugins"
tar -xzf "$bundle_dir/docker-29.9.1.tgz" -C "$runtime_dir/bin" --strip-components=1
install -m 0755 "$bundle_dir/docker-compose-linux-x86_64" "$runtime_dir/cli-plugins/docker-compose"
cat > "$runtime_dir/daemon.json" <<'JSON'
{
  "hosts": ["unix:///run/video-ai-docker.sock"],
  "data-root": "/opt/video-ai/runtime/docker-data",
  "exec-root": "/run/video-ai-docker",
  "pidfile": "/run/video-ai-docker.pid",
  "bridge": "none",
  "iptables": false,
  "ip6tables": false,
  "ip-forward": false,
  "ip-masq": false,
  "log-driver": "local",
  "log-opts": {"max-size": "10m", "max-file": "2"}
}
JSON
cat > /etc/systemd/system/video-ai-docker.service <<'UNIT'
[Unit]
Description=Video AI private M1 container runtime (manual updates; testing only)
After=network-online.target
Wants=network-online.target
[Service]
Type=notify
Environment=PATH=/opt/video-ai/runtime/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/opt/video-ai/runtime/bin/dockerd --config-file=/opt/video-ai/runtime/daemon.json
Restart=on-failure
TimeoutStartSec=90
Delegate=yes
KillMode=process
LimitNOFILE=1048576
TasksMax=infinity
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now video-ai-docker.service
echo 'Private M1 runtime installed; no application containers or public listeners were created.'

#!/usr/bin/env bash

# Copyright (c) 2021-2026 community-scripts ORG
# Author: dmellok
# License: MIT | https://github.com/community-scripts/ProxmoxVE/raw/main/LICENSE
# Source: https://tesserae.ink/ | Github: https://github.com/dmellok/tesserae

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

var_tesserae_channel="${var_tesserae_channel:-stable}"

msg_info "Installing Dependencies"
$STD apt install -y fonts-noto-color-emoji
msg_ok "Installed Dependencies"

PYTHON_VERSION="3.13" setup_uv
if [[ "$var_tesserae_channel" == "edge" ]]; then
  fetch_and_deploy_gh_branch "tesserae" "dmellok/tesserae" "main" "/opt/tesserae"
else
  fetch_and_deploy_gh_release "tesserae" "dmellok/tesserae" "tarball" "latest" "/opt/tesserae"
fi

msg_info "Setting up Tesserae"
mkdir -p /opt/tesserae_data
$STD uv venv --clear /opt/tesserae/.venv --python 3.13
$STD uv pip install --python /opt/tesserae/.venv/bin/python -e /opt/tesserae
cat <<EOF >/opt/tesserae_data/tesserae.env
TESSERAE_DATA_ROOT=/opt/tesserae_data
TESSERAE_BIND_HOST=0.0.0.0
TESSERAE_BIND_PORT=8765
TESSERAE_CHANNEL=$var_tesserae_channel
TESSERAE_INSTALL_METHOD=community-script
PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
PYTHONUNBUFFERED=1
EOF
msg_ok "Set up Tesserae"

msg_info "Installing Chromium for the dashboard renderer"
$STD env PLAYWRIGHT_BROWSERS_PATH=/opt/playwright /opt/tesserae/.venv/bin/playwright install --with-deps chromium
msg_ok "Installed Chromium"

msg_info "Creating Service"
cat <<EOF >/etc/systemd/system/tesserae.service
[Unit]
Description=Tesserae e-ink dashboard server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/tesserae
EnvironmentFile=/opt/tesserae_data/tesserae.env
ExecStart=/opt/tesserae/.venv/bin/python -m app.main
SyslogIdentifier=tesserae
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl enable -q --now tesserae
msg_ok "Created Service"

motd_ssh
customize
cleanup_lxc

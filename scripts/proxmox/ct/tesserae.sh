#!/usr/bin/env bash
_cs_boot="${COMMUNITY_SCRIPTS_CORE_DIR:-$(dirname "${BASH_SOURCE[0]}")/../../core}/core/build.func"
source "$_cs_boot" 2>/dev/null || source <(curl -fsSL "${COMMUNITY_SCRIPTS_CORE_URL:-https://raw.githubusercontent.com/community-scripts/core/main}/core/build.func")
# Copyright (c) 2021-2026 community-scripts ORG
# Author: dmellok
# License: MIT | https://github.com/community-scripts/ProxmoxVE/raw/main/LICENSE
# Source: https://tesserae.ink/ | Github: https://github.com/dmellok/tesserae

APP="Tesserae"
var_tags="${var_tags:-dashboard;e-ink;iot}"
var_cpu="${var_cpu:-4}"
var_ram="${var_ram:-3072}"
var_disk="${var_disk:-16}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_arm64="${var_arm64:-yes}"
var_unprivileged="${var_unprivileged:-1}"

# Asked here on the host: inside the container every prompt counts as unattended.
if [[ -z "${var_tesserae_channel:-}" ]] && command -v pveversion >/dev/null 2>&1; then
  var_tesserae_channel=$(msg_menu "Choose the Tesserae release channel" \
    "stable" "Newest tagged release" \
    "edge" "Main branch, every commit")
fi
export var_tesserae_channel="${var_tesserae_channel:-stable}"

header_info "$APP"
variables
color
catch_errors

if [[ ! "${var_tesserae_channel}" =~ ^(stable|edge)$ ]]; then
  msg_error "var_tesserae_channel must be stable or edge"
  exit 1
fi

function update_script() {
  header_info
  check_container_storage
  check_container_resources
  if [[ ! -d /opt/tesserae ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  # The channel picked at install time is recorded in the env file: stable follows
  # tagged releases, edge tracks the main branch as a git checkout.
  channel=$(sed -n 's/^TESSERAE_CHANNEL=//p' /opt/tesserae_data/tesserae.env 2>/dev/null)
  if [[ "$channel" == "edge" ]]; then
    check_for_gh_branch "tesserae" "dmellok/tesserae" "main" || exit 0
  else
    check_for_gh_release "tesserae" "dmellok/tesserae" || exit 0
  fi

  msg_info "Stopping Service"
  systemctl stop tesserae
  msg_ok "Stopped Service"

  PYTHON_VERSION="3.13" setup_uv
  if [[ "$channel" == "edge" ]]; then
    fetch_and_deploy_gh_branch "tesserae" "dmellok/tesserae" "main" "/opt/tesserae"
  else
    CLEAN_INSTALL=1 fetch_and_deploy_gh_release "tesserae" "dmellok/tesserae" "tarball" "latest" "/opt/tesserae"
  fi

  msg_info "Updating ${APP}"
  $STD uv venv --clear /opt/tesserae/.venv --python 3.13
  $STD uv pip install --python /opt/tesserae/.venv/bin/python -e /opt/tesserae
  # A release may move the pinned Playwright minor, and each minor wants its own Chromium build.
  $STD env PLAYWRIGHT_BROWSERS_PATH=/opt/playwright /opt/tesserae/.venv/bin/playwright install --with-deps chromium
  msg_ok "Updated ${APP}"

  msg_info "Starting Service"
  systemctl start tesserae
  msg_ok "Started Service"
  msg_ok "Updated successfully!"
  exit
}

start
build_container
description

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW}Access it using the following URL:${CL}"
echo -e "${GATEWAY}${BGN}http://${IP}:8765${CL}"

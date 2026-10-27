# Proxmox VE helper script

A [community-scripts](https://community-scripts.org/) style LXC installer for Tesserae: `ct/tesserae.sh` creates the container on a Proxmox VE host, `install/tesserae-install.sh` runs inside it, and `json/tesserae.json` is the catalog record the community-scripts website reads.

The container ends up with the newest Tesserae release under `/opt/tesserae`, persistent state under `/opt/tesserae_data`, Playwright's Chromium under `/opt/playwright`, and a `tesserae` systemd service on port 8765. The first visit lands on the setup wizard.

## Run it

Until the script is merged upstream, run it straight from this repo. On the Proxmox VE host shell:

```sh
COMMUNITY_SCRIPTS_URL=https://raw.githubusercontent.com/dmellok/tesserae/main/scripts/proxmox \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/dmellok/tesserae/main/scripts/proxmox/ct/tesserae.sh)"
```

`COMMUNITY_SCRIPTS_URL` tells the community-scripts engine where to fetch `install/tesserae-install.sh` from. The engine itself still comes from `community-scripts/core`.

Before the container dialog, the script asks for a release channel; type `edge` or `stable` within ten seconds, or it takes `stable`. `stable` installs the newest tagged release from a tarball; `edge` clones the `main` branch and follows every commit. To answer up front for an unattended run, set `var_tesserae_channel=edge` (or `stable`) in the environment alongside `COMMUNITY_SCRIPTS_URL`. The choice is recorded as `TESSERAE_CHANNEL` in `/opt/tesserae_data/tesserae.env`, and `update` reads it back to decide whether to look for a newer release or a newer commit.

The env file also sets `TESSERAE_INSTALL_METHOD=community-script`, which makes Settings → System in the app point at the `update` command instead of the bare-install advice to re-run the installer.

Inside a container built this way, `update` re-runs the script's `update_script()` step: it stops the service, replaces `/opt/tesserae` with the newest release, rebuilds the venv, refreshes Chromium if the Playwright pin moved, and starts the service again. `/opt/tesserae_data` is untouched.

## Upstream submission

New scripts go to [community-scripts/DevScripts](https://github.com/community-scripts/DevScripts) first, as the same three files at `ct/`, `install/` and `json/`. Keep these copies in sync with whatever is submitted there.

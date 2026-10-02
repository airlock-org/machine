{ config, lib, pkgs, ... }:
let
  cfg = config.airlock.qubeGuest;
  workspaceState = "/home/${cfg.user}/.local/state/airlock";
in
{
  imports = [
    ./app-gui-guest.nix
    ./sys-agent-machine-build.nix
    ./sys-agent-build-store.nix
    ./machine-source-seed.nix
  ];

  # sys-agent is an intentionally privileged USB-lab development domain. It
  # remains the Tailnet entry point for the development-only dom0 qrexec shell.
  services.tailscale = {
    enable = true;
    extraUpFlags = [ "--ssh" ];
  };
  # Keep enrollment on Qubes' private volume across immutable root updates
  # and explicit VM replacements. Both services wait for private storage.
  systemd.services.airlock-agent-tailscale-state = {
    wantedBy = [ "multi-user.target" ];
    requires = [ "airlock-qubes-rw-init.service" ];
    after = [ "airlock-qubes-rw-init.service" ];
    before = [ "tailscaled.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      ${pkgs.coreutils}/bin/install -d -m 0700 /rw/config/tailscale
    '';
  };
  systemd.services.tailscaled = {
    requires = [ "airlock-agent-tailscale-state.service" ];
    after = [ "airlock-agent-tailscale-state.service" ];
    serviceConfig.BindPaths = [ "/rw/config/tailscale:/var/lib/tailscale" ];
    serviceConfig.ExecStartPost = [ "${pkgs.tailscale}/bin/tailscale set --hostname=sys-agent" ];
  };
  environment.systemPackages = [
    pkgs.codex
    pkgs.gnome-console
    pkgs.gnumake
    pkgs.jq
    pkgs.python3
    pkgs.ripgrep
    pkgs.uv
    pkgs.zellij
  ];
  fonts.packages = [ pkgs.dejavu_fonts ];
  airlock.qubeGuest.configRepoPath = lib.mkDefault "/home/${cfg.user}/airlock-machine";
  environment.sessionVariables = {
    CODEX_HOME = cfg.codexHome;
    AIRLOCK_DATA_ROOT = workspaceState;
    AIRLOCK_LOG_DIR = "${workspaceState}/logs";
    AIRLOCK_NIX_ROOT = cfg.configNixStoreRoot;
    # The installed nix wrapper already selects the approved overlay store.
    # Do not let development-host helpers inject a different store setting.
    AIRLOCK_NIX_CONFIGURED = "1";
    UV_PYTHON = "${pkgs.python3}/bin/python3";
    UV_PYTHON_DOWNLOADS = "never";
  };
  environment.etc."airlock/sys-agent/AGENTS.md".source = ./sys-agent/AGENTS.md;
  environment.etc."airlock/sys-agent/home-AGENTS.md".source = ./sys-agent/home-AGENTS.md;
  systemd.services.airlock-sys-agent-workspace = {
    description = "Initialize persistent sys-agent workspace and guidance";
    wantedBy = [ "multi-user.target" ];
    requires = [ "airlock-qubes-rw-init.service" ];
    after = [ "airlock-qubes-rw-init.service" ];
    before = [ "airlock-machine-source-seed.service" "qubes-qrexec-agent.service" ];
    path = [ pkgs.coreutils ];
    script = ''
      ${pkgs.bash}/bin/bash ${./sys-agent/workspace-init.sh} \
        ${lib.escapeShellArg cfg.codexHome} /etc/airlock/sys-agent/AGENTS.md \
        ${lib.escapeShellArg "/home/${cfg.user}"} /etc/airlock/sys-agent/home-AGENTS.md \
        ${lib.escapeShellArg cfg.configRepoPath}
    '';
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      User = cfg.user;
    };
  };
  environment.etc."qubes-rpc/airlock.DevTailscaleUp" = {
    source = pkgs.writeShellScript "airlock-dev-tailscale-up" ''
      export PATH=${pkgs.lib.makeBinPath [ pkgs.coreutils pkgs.glibc.bin pkgs.iproute2 pkgs.curl pkgs.tailscale ]}:$PATH
      ${builtins.readFile ../tools/scripts/dev-tailscale-up.sh}
    '';
    mode = "0555";
  };
  environment.etc."qubes/rpc-config/airlock.DevTailscaleUp" = {
    text = "force-user='root'\n";
    mode = "0444";
  };
  # USB-lab's local dom0 operator needs to perform the one-time Tailnet
  # enrollment before remote access exists. This is intentionally confined to
  # the privileged development domain.
  airlock.qubeGuest.allowQrexecUserSelection = true;
  networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 22 ];
  airlock.devDom0Terminal.guest.enable = true;
}

{
  lib,
  airlockConfigDomains ? { },
  airlockQubeKernel ? null,
  ...
}:
let
  haveController = builtins.hasAttr "sys-ctrl" airlockConfigDomains;
  haveQubeKernel = airlockQubeKernel != null;
in
{
  imports = [ ../misc/modules/machine-deployment.nix ];
  airlock.machineDeployment = { enable = true; profile = "controller-lab"; };
  airlock.poolAdmission = {
    enable = lib.mkDefault true;
    caller = lib.mkDefault "sys-ctrl";
    targets = lib.mkDefault (builtins.attrNames airlockConfigDomains);
  };
  assertions = [
    {
      assertion = haveController;
      message = "controller bootstrap requires a sys-ctrl domain";
    }
    {
      assertion = haveQubeKernel;
      message = "lifecycle/bootstrap/config-system.nix requires airlockQubeKernel specialArg.";
    }
  ];

  airlock.qubes = {
    enable = true;
    setupConfigOnBoot.enable = true;
    startNetworkProvidersOnBoot.enable = true;
    deferredSetupConfigOnBoot.enable = true;
    configCurrentBootstrapOnBoot.enable = false;

    prefs = {
      # Fresh Airlock dom0 images do not have a Fedora/Qubes VM kernel
      # installed. Nix-native domains set their own kernel explicitly.
      default_kernel = "";
      default_guivm = "sys-gui";
      default_netvm = "sys-firewall";
    };

    templates = { };

    kernels = {
      ${if haveQubeKernel then airlockQubeKernel.name else "missing-qubes-vm-kernel"} = {
        source = if haveQubeKernel then "${airlockQubeKernel.package}" else null;
      };
    };

    domains = airlockConfigDomains;

    qrexecPolicy.rules = [ ];

    configAccessPolicy.enable = false;
  };

  # This profile may switch a staged dom0 system generation at runtime. Keep the
  # qcow2 capacity large; it is sparse on the host but gives nested dom0 enough
  # room for closure-backed domain artifacts.
  virtualisation.diskSize = lib.mkDefault 131072;
}

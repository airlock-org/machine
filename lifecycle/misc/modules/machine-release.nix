{
  config,
  lib,
  pkgs,
  airlockMachineGuestEntries,
  airlockSourceRevision,
  airlockSourceLockHash,
  ...
}:
let
  deploymentTopology = config.airlock.qubes.topology // {
    domains = lib.mapAttrs (
      _: domain:
      if domain.backend.type != "nix-closure" then
        domain
      else
        let
          matches = builtins.filter (
            entry: toString entry.configuration.config.system.build.toplevel == domain.backend.system.source
          ) (builtins.attrValues airlockMachineGuestEntries);
          entry =
            if matches == [ ] then
              throw "No closure artifact for machine deployment domain"
            else
              builtins.head matches;
        in
        domain
        // {
          backend = domain.backend // {
            store = domain.backend.store // {
              source = toString entry.storeArtifact;
            };
          };
        }
    ) config.airlock.qubes.topology.domains;
  };
  setup = pkgs.callPackage ../tools/pkgs/airlock-setup-config { };
  engine = pkgs.callPackage ../../runtime/tools/pkgs/airlock-machine-deploy { };
  manifest = {
    schema = "airlock.machine-release.v1";
    profile = config.airlock.machineDeployment.profile;
    host = toString config.system.build.toplevel;
    setup = "${setup}/bin/airlock-setup-config";
    coordinator = "${engine}/bin/airlock-machine-deploy";
    revision = airlockSourceRevision;
    lockHash = airlockSourceLockHash;
    retireDomains = config.airlock.machineDeployment.retireDomains;
    trashRetentionDays = config.airlock.machineDeployment.trashRetentionDays;
  };
  releaseManifest = pkgs.writeText "airlock-machine-release.json" (builtins.toJSON manifest);
  releaseTopology = pkgs.writeText "airlock-machine-topology.json" (
    builtins.toJSON deploymentTopology
  );
  # The controller keeps the full release as its build/GC root. Dom0 imports
  # only this transport output, whose topology is an identity document rather
  # than a Nix reference to every guest payload.
  detachedTopology = pkgs.runCommand "airlock-machine-pool-topology.json" {
    __structuredAttrs = true;
    unsafeDiscardReferences.out = true;
  } ''
    cp ${releaseTopology} "$out"
  '';
  transportManifest = pkgs.writeText "airlock-machine-pool-release.json" (
    builtins.toJSON (manifest // { guestTransport = "direct-pool-v1"; })
  );
in
{
  system.build.airlockMachineRelease = pkgs.runCommand "airlock-machine-release" { } ''
    mkdir -p "$out"
    cp ${releaseManifest} "$out/release.json"
    cp ${releaseTopology} "$out/topology.json"
    ln -s ${config.system.build.airlockMachineTransport} "$out/transport"
  '';
  system.build.airlockMachineTransport = pkgs.runCommand "airlock-machine-transport" { } ''
    mkdir -p "$out"
    cp ${transportManifest} "$out/release.json"
    cp ${detachedTopology} "$out/topology.json"
  '';

  # The host's boot topology also names guest systems. Keep its actual root
  # images and kernels explicitly, while pool retention owns guest packages.
  environment.etc."airlock/qubes-topology.json" = {
    text = lib.mkForce null;
    source = lib.mkDefault detachedTopology;
  };
  system.extraDependencies =
    map (kernel: kernel.source) (builtins.attrValues config.airlock.qubes.kernels)
    ++ map (domain: builtins.dirOf domain.backend.root.source)
      (builtins.filter (domain: domain.backend.type == "nix-closure")
        (builtins.attrValues deploymentTopology));
}

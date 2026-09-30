{
  config,
  lib,
  pkgs,
  airlockMachineGuestEntries,
  airlockSourceRevision,
  airlockSourceTimestamp,
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
    timestamp = airlockSourceTimestamp;
    lockHash = airlockSourceLockHash;
    retireDomains = config.airlock.machineDeployment.retireDomains;
    trashRetentionDays = config.airlock.machineDeployment.trashRetentionDays;
  };
  releaseManifest = pkgs.writeText "airlock-machine-release.json" (builtins.toJSON manifest);
  # Only content descriptors enter the host closure. Payloads stay in the
  # controller build closure and are admitted to dom0's pool separately.
  rootArtifacts = pkgs.runCommand "airlock-machine-root-artifacts.json" { } ''
    echo '{}' > "$out"
    ${lib.concatStringsSep "\n" (lib.mapAttrsToList (name: domain:
      let entry = builtins.head (builtins.filter
        (entry: toString entry.configuration.config.system.build.toplevel == domain.backend.system.source)
        (builtins.attrValues airlockMachineGuestEntries));
      in lib.optionalString (domain.backend.type == "nix-closure") ''
        ${pkgs.jq}/bin/jq --arg name ${lib.escapeShellArg name} \
          --slurpfile artifact ${entry.rootImage.metadata}/root.json \
          '. + {($name): $artifact[0]}' "$out" > next.json
        mv next.json "$out"
      ''
    ) deploymentTopology.domains)}
  '';
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
    cp ${rootArtifacts} "$out/root-artifacts.json"
    ln -s ${config.system.build.airlockMachineTransport} "$out/transport"
  '';
  system.build.airlockMachineTransport = pkgs.runCommand "airlock-machine-transport" { } ''
    mkdir -p "$out"
    cp ${transportManifest} "$out/release.json"
    cp ${detachedTopology} "$out/topology.json"
    cp ${rootArtifacts} "$out/root-artifacts.json"
  '';

  # The pool retains verified immutable guest roots and package generations.
  # The host keeps its own kernel/recovery dependencies, without root payloads.
  environment.etc."airlock/qubes-topology.json" = {
    text = lib.mkForce null;
    source = lib.mkDefault detachedTopology;
  };
  system.extraDependencies =
    map (kernel: kernel.source) (builtins.attrValues config.airlock.qubes.kernels);
}

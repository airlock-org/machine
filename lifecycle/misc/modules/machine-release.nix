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
  releaseManifest = pkgs.writeText "airlock-machine-release.json" (
    builtins.toJSON {
      schema = "airlock.machine-release.v1";
      profile = config.airlock.machineDeployment.profile;
      host = toString config.system.build.toplevel;
      setup = "${setup}/bin/airlock-setup-config";
      coordinator = "${engine}/bin/airlock-machine-deploy";
      revision = airlockSourceRevision;
      lockHash = airlockSourceLockHash;
      retireDomains = config.airlock.machineDeployment.retireDomains;
      trashRetentionDays = config.airlock.machineDeployment.trashRetentionDays;
    }
  );
  releaseTopology = pkgs.writeText "airlock-machine-topology.json" (
    builtins.toJSON deploymentTopology
  );
in
{
  system.build.airlockMachineRelease = pkgs.runCommand "airlock-machine-release" { } ''
    mkdir -p "$out"
    cp ${releaseManifest} "$out/release.json"
    cp ${releaseTopology} "$out/topology.json"
  '';
}

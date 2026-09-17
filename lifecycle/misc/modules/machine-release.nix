{
  config,
  pkgs,
  airlockSourceRevision,
  airlockSourceLockHash,
  ...
}:
let
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
    }
  );
  releaseTopology = pkgs.writeText "airlock-machine-topology.json" (
    builtins.toJSON config.airlock.qubes.topology
  );
in
{
  system.build.airlockMachineRelease = pkgs.runCommand "airlock-machine-release" { } ''
    mkdir -p "$out"
    cp ${releaseManifest} "$out/release.json"
    cp ${releaseTopology} "$out/topology.json"
  '';
}

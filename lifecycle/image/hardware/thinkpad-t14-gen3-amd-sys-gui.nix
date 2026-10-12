{ pkgs, ... }:
{
  # Lab mitigation for the internal 680M/eDP panel's DMCUB/flip stalls.
  # Change only PSR; preserve generated Qubes/Nix closure kernel options.
  boot.kernelParams = [ "amdgpu.dcdebugmask=0x10" ];
  environment.systemPackages = [
    (pkgs.callPackage ../../runtime/tools/pkgs/airlock-gpu-recovery { })
  ];
}

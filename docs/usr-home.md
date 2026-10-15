# Personal applications in usr-home

`usr-home` runs personal applications and holds their persistent files and
settings. Sys-gui displays their windows through Qubes; GNOME Shell and the
physical display remain in sys-gui. Network access follows sys-firewall and
sys-net.

Applications, file associations, and desktop integration are declared in the
[usr-home module](../lifecycle/misc/modules/usr-home-guest.nix). Qubes discovers
standard desktop entries and exposes launchers through GNOME. File opening and
application settings use GNOME/XDG mechanisms inside usr-home. Application data
lives on the private volume and survives system updates and rollback. Removing
an application does not delete or migrate its data.

Install or remove applications by changing the machine configuration in
[usr-assistant](usr-assistant.md) and deploying it through the controller.
Usr-home has no private Nix builder; interactive Nix and Flatpak installation
are disabled in this VM.

Changes limited to usr-home can use `airlock machine guest-plan --target usr-home`
and `airlock machine guest-apply --target usr-home`. Review the affected VMs in
the plan and verify deployment and application behavior afterwards.

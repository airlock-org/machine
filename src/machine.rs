//! Declare the physical USB machine's VMs and explicit network, display and capture relationships.

use crate::{
    components,
    model::{AuthoringError, GiB, HostPlatform, Machine, MachineSnapshot, Vm},
};

/// Connect the six physical-machine VMs before freezing their validated identities and resources.
pub fn machine() -> Result<MachineSnapshot, AuthoringError> {
    let mut machine = Machine::default();
    machine.set_host_platform(HostPlatform::UsbLab);

    let network = Vm::network("sys-net", GiB(2))?;
    machine.add(&network)?;

    let firewall = Vm::firewall("sys-firewall", &network)?;
    machine.add(&firewall)?;

    let home = Vm::personal("usr-home", &firewall, GiB(16))?;
    machine.add(&home)?;

    let gui = Vm::physical_gui("sys-gui", &home)?;
    gui.add_module(components::desktop::gnome());
    components::screen_capture::connect(&gui, &home);
    machine.add(&gui)?;

    let controller = Vm::controller("sys-ctrl", &firewall, GiB(32))?;
    machine.add(&controller)?;

    let assistant = Vm::assistant("usr-assistant", &firewall, GiB(16))?;
    machine.add(&assistant)?;

    machine.set_host(&gui, &firewall, &controller);
    components::diagnostics::enable(&mut machine, &assistant);
    machine.finish()
}

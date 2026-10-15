# Physical GUI input

The reusable module `lifecycle/misc/modules/qubes-input-proxy.nix` discovers
local input devices in dom0 and forwards each independently to the GUI VM.
It uses [upstream Qubes input proxy 1.0.46](https://github.com/QubesOS/qubes-app-linux-input-proxy/tree/ea8710166c6ac29dd4183cdf155bc9a1da34cf4a)
without changing its event protocol. There is no machine-specific input list,
required TrackPoint, device model match, or custom touchpad symlink.

## Discovery and initial trust

`airlock.inputProxy.sender.enable` defaults to false; `sender.target` defaults
to `sys-gui`. The physical GUI profile enables discovery. Hybrid desktops,
console profiles and text recovery do not start a second input path.

Once the GUI receiver is ready, the discovery service subscribes to udev events
before enumerating existing `/dev/input/event*` devices. It accepts devices by
udev input capabilities: keyboards and key devices, mice and pointing sticks,
touchpads, touchscreens, tablets and joystick devices. USB and internal devices
use the same path. Locally synthesized `/sys/devices/virtual/input/` devices
are excluded to avoid recirculating input; switches and sensors without a
supported key or pointer capability are not forwarded.

All discovered supported devices are trusted automatically, including devices
connected later. There is currently no approval or rejection policy. Dom0
records their descriptors and first-seen time in
`/var/lib/airlock/input/trusted-devices.json`, retaining that inventory across
reboots and ignoring changing event numbers for identity. This is an observed
trust inventory, not a cryptographic identity or enforced allowlist. A recording
failure is logged and does not disable input. A fresh USB flash resets the
inventory with the rest of the installation.

Each evdev node gets one Qubes forwarding process. A missing, removed or failed
device cannot stop other devices. Duplicate udev notifications do not duplicate
forwarding; unplugging and reconnecting starts a new attempt. Sender failure is
logged and does not trigger an endless retry loop. The discovery service itself
restarts on failure. No USB controllers are reassigned by discovery.

## Transport and lifecycle

The dom0-only `airlock.InputDevice` RPC invokes the existing upstream receiver
with `--keyboard --mouse --tablet` together. This preserves supported key,
relative and absolute capabilities on composite devices without trying to grab
one device multiple times. The existing `qubes.InputKeyboard`, `qubes.InputMouse`
and `qubes.InputTablet` handlers remain available for older senders. Upstream
protocol limits still apply; discovery does not add force feedback or sensor
transport.

Dom0 uses Qubes' `qrexec-client -l` administrative interface. This interface does
not consult qrexec policy. Receivers require `QREXEC_REMOTE_DOMAIN=dom0`, and
policy denies VM-originated input requests. This grants no new input authority
to application VMs. A future dedicated input VM requires an explicit change to
the trusted source and policy.

In the receiving VM, `airlock.inputProxy.receiver.enable` loads `uinput` and
installs the root RPC handlers. `airlock.InputReceiverReady` checks the device
and handlers. The existing Qubes lifecycle listener requests one bounded
readiness check when the GUI starts, verifies that the same VM instance remains
running, then activates `airlock-gui-input.target` and its discovery service.
A GUI stop releases all forwarding; a GUI restart enumerates again. This covers
boot, manual VM starts and deployment replacements without a separate input
repair path. The coordinator never starts or retries the VM itself.

For diagnostics in **dom0**:

```sh
sudo systemctl start airlock-gui-input-connect.service
sudo systemctl status airlock-gui-input-discovery.service
sudo journalctl -u airlock-gui-input-discovery.service
sudo cat /var/lib/airlock/input/trusted-devices.json
```

The journal identifies each forwarding attempt, removal and sender exit. An
active discovery service or a trusted inventory entry does not prove physical
input reached the desktop. Stop forwarding and release device grabs with:

```sh
sudo systemctl stop airlock-gui-input.target
```

## Boot-time keyboard initialization guard

Physical sender profiles run `airlock-input-boot-guard.service` after topology
and network setup, before GPU handoff and input connection. It reads this boot's
kernel journal and reacts only to the explicit atkbd "Failed to enable keyboard"
error on the physical i8042 port. A missing TrackPoint, idle keyboard, or zero
interrupt counter does not itself trigger a reset. Machines without i8042 are
unaffected.

The guard records the kernel evidence and an attempted marker before performing
one i8042 driver unbind/bind. It waits for the keyboard driver to bind and checks
new kernel messages for another initialization failure. The reset child has a
20-second timeout, driver observation has a 10-second limit, journal operations
have 10-second limits, and the whole service has a 75-second startup limit.
Service restarts cannot repeat a failed or interrupted reset within the same boot.
Timeouts are subject to the kernel being able to terminate a blocked sysfs writer.

Reports and before/after kernel evidence are saved under
`/var/log/airlock/boot-attempts/<boot-id>/input-controller.json` and adjacent
`input-kernel-*.json` files. Outcomes distinguish no detected failure,
not-applicable hardware, reinitialization, and failure. They always report
`physical_input_verified=false`: successful initialization and forwarding do
not establish that the user can type or click.

On failure, the GPU startup dependency cannot proceed. With boot supervision
enabled, the existing failure handler preserves diagnostics and shows the boot
failure console, including the input report path. It does not silently accept
the boot, repeatedly reset hardware, or add any VM control capability. A failed
built-in keyboard can still require an external keyboard at that console.
Independent text recovery has physical forwarding and this guard disabled.

Recorded experiments and boot observations are in the
[archived input notes](history/sys-gui-input-2026-10-15.md).

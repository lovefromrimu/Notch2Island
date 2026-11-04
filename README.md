# Notch2Island

**Old notch. New island.**

Bring a native, Dynamic Island-style HUD experience to MacBooks that already have a notch.

Notch2Island is an experimental command-line project that activates macOS's own CenterHUD in the built-in MacBook notch. Instead of drawing a separate overlay, it temporarily changes runtime behavior inside Apple's existing `MenuBarAgent`. On the tested system, this produces a visible native expanded volume HUD.

> **Experimental compatibility:** Currently tested only on macOS 27.3 Beta, build `26C5035r`. The program rejects other builds. It modifies a running system process and may briefly interrupt the menu bar.

## Requirements

- A MacBook with a built-in notched display, running **macOS 27.3 Beta (build `26C5035r`)**.
- Python **3.10 or newer**.
- Xcode developer tools providing LLDB and Swift via `xcrun`.
- SIP already configured to allow debugging. The tool checks this prerequisite; it does not change SIP settings.
- Run as the logged-in user, **without** `sudo`.

## Get started

Clone this repository and enter its directory:

```sh
git clone https://github.com/lovefromrimu/Notch2Island.git
cd Notch2Island
```

Check the environment before making changes:

```sh
python3 centerhud.py doctor
python3 centerhud.py screens
python3 centerhud.py enable --dry-run
```

`doctor` reports build, debugging and process prerequisites. `screens` lists detected displays. `--dry-run` checks that the tool could attempt attachment without installing overrides.

Enable the native HUD:

```sh
python3 centerhud.py enable
python3 centerhud.py status
```

`enable` attaches LLDB, installs temporary runtime overrides, then detaches. `status` checks the saved process identity; it does not verify visible HUD activity.

## Restore

```sh
python3 centerhud.py disable
```

This ends the tracked `MenuBarAgent` process, letting macOS launch a fresh instance without the overrides. The menu bar may disappear or flicker briefly.

If the original session record has been lost, a manual restart is available:

```sh
python3 centerhud.py disable --force
```

`--force` restarts the verified menu-bar process even without a matching record. This can also clear other modifications in that process.

## Implementation

The program checks the macOS build, target process identity, runtime method signatures and built-in notch geometry before changing methods. Overrides are process-local and disappear when that process exits. System executables on disk are unchanged.

A local session record in `~/Library/Application Support/Notch2Island/` helps identify the process for restoration. No login item or background service is installed.

## Limitations

- The demonstrated experience is a native expanded volume HUD, **not** the full interactive Dynamic Island feature set.
- Only the built-in MacBook notch is targeted. `screens` can identify Sidecar candidates, but Sidecar-only CenterHUD activation is not implemented.
- Full-screen apps, sleep/wake, media interactions and other macOS builds require further live validation.
- Undocumented interfaces and debugger behavior can change or fail.

## Tests

```sh
python3 -B -m unittest discover -s tests -v
```

Automated tests cover command behavior, process targeting, session management and failure handling. They do not replace live testing on a compatible Mac.

## License

MIT — see [LICENSE](LICENSE). Notch2Island is independent and is not affiliated with or endorsed by Apple.

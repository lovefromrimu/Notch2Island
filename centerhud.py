#!/usr/bin/env python3
"""Experimental, process-local CenterHUD overrides for an allowlisted macOS build."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

SUPPORTED_BUILDS = {"26C5035r"}
STATE_PATH = Path.home() / "Library" / "Application Support" / "Notch2Island" / "session.json"
AGENT_PATH = "/System/Library/CoreServices/MenuBarAgent.app/Contents/MacOS/MenuBarAgent"
XCRUN = "/usr/bin/xcrun"
NO_CHANGE_RESULTS = {
    "CENTERHUD_MISSING_METHODS", "CENTERHUD_INCOMPATIBLE_METHODS",
    "CENTERHUD_NO_DISPLAYS", "CENTERHUD_NO_BUILTIN_NOTCH", "CENTERHUD_ALREADY_ACTIVE",
}

# All guards run before the first method replacement. An installed override does
# not establish whether the HUD is visible or whether its interactions work.
SWIFT_EXPRESSION = """
import AppKit;
import CoreGraphics;
import ObjectiveC.runtime;
({
    let cls=NSScreen.self;
    let notchSel=NSSelectorFromString("_notchFrame");
    let hardwareSel=NSSelectorFromString("_centerHUDHardwareFrame");
    let defaultSel=NSSelectorFromString("_centerHUDDefaultFrame");
    let safeSel=NSSelectorFromString("_isSafeAperture");
    guard let notch=class_getInstanceMethod(cls,notchSel),
          let hardware=class_getInstanceMethod(cls,hardwareSel),
          let defaultFrame=class_getInstanceMethod(cls,defaultSel),
          let safe=class_getInstanceMethod(cls,safeSel),
          let frame=class_getInstanceMethod(cls,NSSelectorFromString("frame")),
          let boolMethod=class_getInstanceMethod(NSObject.self,NSSelectorFromString("isProxy"))
    else { return "CENTERHUD_MISSING_METHODS" };
    guard let rectTypes=method_getTypeEncoding(frame),
          let boolTypes=method_getTypeEncoding(boolMethod),
          [notch,hardware,defaultFrame].allSatisfy({ method in
              guard let types=method_getTypeEncoding(method) else { return false };
              return method_getNumberOfArguments(method)==2 &&
                  String(cString:types)==String(cString:rectTypes);
          }),
          let safeTypes=method_getTypeEncoding(safe),
          method_getNumberOfArguments(safe)==2,
          String(cString:safeTypes)==String(cString:boolTypes)
    else { return "CENTERHUD_INCOMPATIBLE_METHODS" };
    let screens=NSScreen.screens;
    guard !screens.isEmpty else { return "CENTERHUD_NO_DISPLAYS" };
    typealias GetRect = @convention(c)(AnyObject, Selector)->CGRect;
    typealias GetBool = @convention(c)(AnyObject, Selector)->Bool;
    let readNotch=unsafeBitCast(method_getImplementation(notch),to:GetRect.self);
    let readHardware=unsafeBitCast(method_getImplementation(hardware),to:GetRect.self);
    let readDefault=unsafeBitCast(method_getImplementation(defaultFrame),to:GetRect.self);
    let readSafe=unsafeBitCast(method_getImplementation(safe),to:GetBool.self);
    let isTarget:(NSScreen)->Bool={ screen in
        guard let number=screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber
        else { return false };
        guard CGDisplayIsBuiltin(number.uint32Value) != 0 else { return false };
        let rect=readNotch(screen,notchSel);
        return rect.width>0 && rect.height>0 && rect.width.isFinite && rect.height.isFinite;
    };
    let targets=screens.filter(isTarget);
    guard !targets.isEmpty else { return "CENTERHUD_NO_BUILTIN_NOTCH" };
    for screen in targets {
        let current=readHardware(screen,hardwareSel);
        if current.width>0 && current.height>0 && current.width.isFinite && current.height.isFinite {
            return "CENTERHUD_ALREADY_ACTIVE";
        };
    };
    let hardwareBlock:@convention(block)(NSScreen)->CGRect={ screen in
        guard let number=screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber,
              CGDisplayIsBuiltin(number.uint32Value) != 0
        else { return readHardware(screen,hardwareSel) };
        let rect=readNotch(screen,notchSel);
        if rect.width>0 && rect.height>0 && rect.width.isFinite && rect.height.isFinite { return rect };
        return readHardware(screen,hardwareSel);
    };
    let defaultBlock:@convention(block)(NSScreen)->CGRect={ screen in
        guard let number=screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber,
              CGDisplayIsBuiltin(number.uint32Value) != 0
        else { return readDefault(screen,defaultSel) };
        let rect=readNotch(screen,notchSel);
        if rect.width>0 && rect.height>0 && rect.width.isFinite && rect.height.isFinite { return rect };
        return readDefault(screen,defaultSel);
    };
    let safeBlock:@convention(block)(NSScreen)->Bool={ screen in
        guard let number=screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber,
              CGDisplayIsBuiltin(number.uint32Value) != 0
        else { return readSafe(screen,safeSel) };
        let rect=readNotch(screen,notchSel);
        if rect.width>0 && rect.height>0 && rect.width.isFinite && rect.height.isFinite { return true };
        return readSafe(screen,safeSel);
    };
    let hardwareIMP=imp_implementationWithBlock(hardwareBlock);
    let defaultIMP=imp_implementationWithBlock(defaultBlock);
    let safeIMP=imp_implementationWithBlock(safeBlock);
    method_setImplementation(hardware,hardwareIMP);
    method_setImplementation(defaultFrame,defaultIMP);
    method_setImplementation(safe,safeIMP);
    for screen in targets {
        NotificationCenter.default.post(name:Notification.Name("_NSScreenDidChangeSafeApertureNotification"),object:screen);
        NotificationCenter.default.post(name:Notification.Name("NSScreenDidChangeSafeApertureNotification"),object:screen);
    };
    return "CENTERHUD_OVERRIDE_INSTALLED";
})()
"""
SWIFT_EXPRESSION = " ".join(line.strip() for line in SWIFT_EXPRESSION.splitlines() if line.strip())


@dataclass(frozen=True)
class Agent:
    pid: int
    birth: str
    uid: int


def command(args: list[str], timeout: float = 16) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, LC_ALL="C", LANG="C")
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)


def require_user() -> None:
    if os.getuid() == 0 or os.geteuid() == 0:
        raise RuntimeError("Run as the logged-in user; root execution is refused.")


def inspect_agent(pid: int) -> Agent | None:
    result = command(["/bin/ps", "-p", str(pid), "-o", "uid=", "-o", "lstart=", "-o", "comm="], timeout=5)
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return None
    if result.returncode:
        raise RuntimeError("Could not inspect the MenuBarAgent process.")
    fields = result.stdout.strip().split(None, 6)
    if len(fields) != 7 or not fields[0].isdigit():
        raise RuntimeError("Unexpected process identity output; no process action is permitted.")
    uid = int(fields[0])
    if uid != os.getuid() or fields[6] != AGENT_PATH:
        return None
    return Agent(pid, " ".join(fields[1:6]), uid)


def find_agent() -> Agent | None:
    result = command(["/usr/bin/pgrep", "-u", str(os.getuid()), "-x", "MenuBarAgent"], timeout=5)
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return None
    if result.returncode:
        raise RuntimeError("Could not enumerate MenuBarAgent processes.")
    ids = result.stdout.split()
    if any(not value.isdigit() or int(value) <= 0 for value in ids):
        raise RuntimeError("Unexpected process list; no process action is permitted.")
    agents = [agent for pid in ids if (agent := inspect_agent(int(pid))) is not None]
    if len(agents) > 1:
        raise RuntimeError("More than one matching MenuBarAgent is running; refusing an ambiguous target.")
    return agents[0] if agents else None


def os_build() -> str:
    result = command(["/usr/bin/sw_vers", "-buildVersion"], timeout=5)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Could not read the macOS build.")
    return result.stdout.strip()


def state_directory(create: bool = False) -> bool:
    directory = STATE_PATH.parent
    try:
        info = directory.lstat()
    except FileNotFoundError:
        if not create:
            return False
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("The session directory must be an owned directory, not a symbolic link.")
    if create:
        directory.chmod(0o700)
    return True


def check_state_file() -> None:
    try:
        info = STATE_PATH.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("The session record must be an owned regular file, not a symbolic link.")


def read_session() -> dict:
    if not state_directory():
        return {}
    check_state_file()
    try:
        fd = os.open(STATE_PATH, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        try:
            text = stream.read(16385)
            state = json.loads(text) if len(text) <= 16384 else None
        except (ValueError, UnicodeError, RecursionError):
            return {}
    if not isinstance(state, dict):
        return {}
    legacy = set(state) == {"pid", "birth", "build", "mode"}
    expected = {"version", "pid", "birth", "uid", "build", "mode", "phase"}
    if not legacy and (set(state) != expected or type(state["version"]) is not int or state["version"] != 1):
        return {}
    if (type(state["pid"]) is not int or not 0 < state["pid"] <= 2147483647 or
            not isinstance(state["birth"], str) or not state["birth"].strip() or len(state["birth"]) > 128 or
            not isinstance(state["build"], str) or re.fullmatch(r"[A-Za-z0-9]{1,32}", state["build"]) is None or
            state["mode"] != "builtin-notch"):
        return {}
    if legacy:
        state = dict(state, version=1, uid=os.getuid(), phase="installed")
    if (type(state["uid"]) is not int or state["uid"] != os.getuid() or
            state["phase"] not in ("pending", "installed")):
        return {}
    state["birth"] = " ".join(state["birth"].split())
    return state


def record_for(agent: Agent, build: str, phase: str) -> dict:
    return {"version": 1, "pid": agent.pid, "birth": agent.birth, "uid": agent.uid,
            "build": build, "mode": "builtin-notch", "phase": phase}


def record_agent(state: dict) -> Agent | None:
    return Agent(state["pid"], state["birth"], state["uid"]) if state else None


def session_matches(state: dict, agent: Agent | None, build: str) -> bool:
    return bool(agent and record_agent(state) == agent and state["build"] == build)


def write_session(state: dict) -> None:
    state_directory(create=True)
    check_state_file()
    fd, name = tempfile.mkstemp(prefix=".session-", suffix=".tmp", dir=STATE_PATH.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        check_state_file()
        os.replace(temporary, STATE_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def remove_session(expected: Agent | None = None) -> None:
    check_state_file()
    if expected is not None and STATE_PATH.exists() and record_agent(read_session()) != expected:
        raise RuntimeError("The session record changed; it was left in place.")
    STATE_PATH.unlink(missing_ok=True)


@contextmanager
def mutation_lock():
    require_user()
    state_directory(create=True)
    check_state_file()
    fd = os.open(STATE_PATH.parent / "session.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("The session lock must be an owned regular file.")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another enable or disable operation is in progress.") from exc
        yield
    finally:
        os.close(fd)


def stop_agent(agent: Agent, timeout: float = 5) -> None:
    """Return only when the original identity no longer exists; never target its replacement."""
    if inspect_agent(agent.pid) != agent:
        return
    try:
        os.kill(agent.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise RuntimeError(f"Could not stop the original MenuBarAgent: {exc}") from exc
    deadline = time.monotonic() + timeout
    while True:
        if inspect_agent(agent.pid) != agent:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("The original MenuBarAgent identity is still running; restoration is unconfirmed.")
        time.sleep(0.1)


def ensure_supported() -> str:
    build = os_build()
    if build not in SUPPORTED_BUILDS:
        raise RuntimeError(f"macOS build {build!r} is not allowlisted; allowed builds: {sorted(SUPPORTED_BUILDS)}")
    if command([XCRUN, "--find", "lldb"], timeout=8).returncode:
        raise RuntimeError("LLDB is not available through xcrun.")
    sip = command(["/usr/bin/csrutil", "status"], timeout=7)
    if sip.returncode or "Debugging Restrictions: disabled" not in sip.stdout:
        raise RuntimeError("The required SIP debugging configuration was not confirmed. This tool never modifies SIP.")
    return build


def tracked_status() -> None:
    state = read_session()
    if session_matches(state, find_agent(), os_build()):
        print(f"TRACKED {state['phase']}: session matches this process; runtime state and HUD visibility are not checked.")
    else:
        print("UNTRACKED: no matching valid session record; runtime state and HUD visibility are unknown.")


def check() -> None:
    build = os_build()
    print("Notch2Island diagnostic")
    print("macOS build:", build, "[allowlisted build]" if build in SUPPORTED_BUILDS else "[not allowlisted]")
    agent = find_agent()
    print("Matching Apple MenuBarAgent PID:", agent.pid if agent else "not found")
    print("LLDB available:", command([XCRUN, "--find", "lldb"], timeout=8).returncode == 0)
    sip = command(["/usr/bin/csrutil", "status"], timeout=7)
    if sip.returncode:
        raise RuntimeError("Could not read SIP status.")
    for line in sip.stdout.splitlines():
        if "Debugging Restrictions" in line or "Filesystem Protections" in line:
            print(line.strip())
    tracked_status()


def enable(dry_run: bool = False) -> None:
    require_user()
    build = ensure_supported()
    if dry_run:
        agent = find_agent()
        if not agent:
            raise RuntimeError("No matching Apple MenuBarAgent is running.")
        print(f"Would attach to PID {agent.pid}; runtime selectors, signatures and display targets are checked only during attachment.")
        print("No session files, locks or process changes were made.")
        return
    with mutation_lock():
        agent = find_agent()
        if not agent:
            raise RuntimeError("No matching Apple MenuBarAgent is running.")
        state = read_session()
        if STATE_PATH.exists() and not state:
            raise RuntimeError("The session record is invalid; inspect it or use disable --force before enabling.")
        if session_matches(state, agent, build):
            if state["phase"] == "pending":
                raise RuntimeError("An incomplete attempt is tracked; use disable before trying again.")
            print("An installed-override record matches this process; HUD visibility has not been checked.")
            return
        if state and record_agent(state) == inspect_agent(state["pid"]):
            raise RuntimeError("A live process has a conflicting session record; restore it before enabling.")
        write_session(record_for(agent, build, "pending"))
        attempted = False
        try:
            time.sleep(2)
            if inspect_agent(agent.pid) != agent:
                raise RuntimeError("MenuBarAgent changed before attachment.")
            cmd = [XCRUN, "lldb", "--no-lldbinit", "--batch", "--attach-pid", str(agent.pid),
                   "--one-line", "expr -l swift -- " + SWIFT_EXPRESSION,
                   "--one-line", "process detach"]
            attempted = True
            result = command(cmd, timeout=20)
            output = result.stdout + "\n" + result.stderr
            marker = re.search(r'\(String\) \$R\d+ = "(CENTERHUD_[A-Z_]+)"', output)
            detached = f"Process {agent.pid} detached" in output
            token = marker.group(1) if marker else None
            if result.returncode == 0 and detached and token in NO_CHANGE_RESULTS:
                attempted = False
                raise RuntimeError(f"Runtime preconditions rejected the attempt ({token}); no methods were replaced.")
            if result.returncode or not detached or token != "CENTERHUD_OVERRIDE_INSTALLED":
                errors=[line.strip() for line in output.splitlines() if line.strip().startswith("error:")]
                detail=errors[-1][:400] if errors else "missing or unrecognized debugger result"
                raise RuntimeError("LLDB did not confirm installation and detachment: " + detail)
            if inspect_agent(agent.pid) != agent:
                raise RuntimeError("The original MenuBarAgent changed after attachment.")
            write_session(record_for(agent, build, "installed"))
        except BaseException as exc:
            try:
                if attempted:
                    stop_agent(agent)
                remove_session(expected=agent)
            except Exception as recovery:
                raise RuntimeError(f"Attempt failed ({type(exc).__name__}: {exc}). Recovery failed ({recovery}); session record retained.") from exc
            raise
        print("Override installation and debugger detachment were confirmed for this process.")
        print("HUD visibility and interactions were not checked. Restore with: python3 centerhud.py disable")


def disable(force: bool = False) -> None:
    require_user()
    if not force and not session_matches(read_session(), find_agent(), os_build()):
        print("No matching tracked session. No process was touched.")
        return
    with mutation_lock():
        state = read_session()
        agent = find_agent()
        if not force and not session_matches(state, agent, os_build()):
            print("The tracked process changed. No process was touched; the record was kept.")
            return
        if agent is None:
            remove_session()
            print("No matching Apple MenuBarAgent was found. The stale record was cleared; no process was touched.")
            return
        stop_agent(agent)
        remove_session()
        print("The original MenuBarAgent identity is no longer running; it can no longer retain this override.")
        print("The replacement process and visual recovery were not checked.")


def screens() -> None:
    script = Path(__file__).resolve().parent / "scripts" / "displays.swift"
    result = command([XCRUN, "swift", str(script)], timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr[-700:])
    print(result.stdout, end="")


def main() -> int:
    parser = argparse.ArgumentParser(description="Experimental process-local CenterHUD overrides.")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor", help="Read-only diagnostics")
    sub.add_parser("screens", help="Print local display diagnostics")
    run = sub.add_parser("enable", help="Install an experimental override on an allowlisted build")
    run.add_argument("--dry-run", action="store_true")
    stop = sub.add_parser("disable", help="End the tracked process to remove its in-memory override")
    stop.add_argument("--force", action="store_true", help="End the matching Apple agent even without a valid record")
    sub.add_parser("status", help="Check session tracking, without inspecting runtime methods or UI")
    args = parser.parse_args()
    try:
        require_user()
        if args.action == "doctor":
            check()
        elif args.action == "screens":
            screens()
        elif args.action == "enable":
            enable(args.dry_run)
        elif args.action == "disable":
            disable(args.force)
        else:
            tracked_status()
        return 0
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"Notch2Island: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Notch2Island: interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

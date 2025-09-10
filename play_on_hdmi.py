#!/usr/bin/env python3
"""
play_on_hdmi.py

Play an MP4 on the Raspberry Pi HDMI output from a console/SSH session
(no X assumed). Uses ffplay (ffmpeg) and attempts DRM/KMS (SDL kmsdrm)
then falls back to framebuffer (fbcon). Optionally shows/uses DRM connector ids
and can call `modetest` to set a mode for a given connector (requires sudo).

Usage:
  ./play_on_hdmi.py --list-connectors
  ./play_on_hdmi.py movie.mp4                    # auto backend
  ./play_on_hdmi.py movie.mp4 --backend kmsdrm   # force kmsdrm
  ./play_on_hdmi.py movie.mp4 --connector-id 29  # attempt to enable connector 29 (if modetest available) before playing
  ./play_on_hdmi.py movie.mp4 --detach           # start ffplay detached (so SSH can disconnect)

Notes:
 - Requires ffplay (part of ffmpeg) installed and SDL compiled with the desired backends.
 - To enable connectors or force modes from userspace you may need libdrm-tests (modetest) and root.
"""

import os
import sys
import argparse
import shutil
import subprocess
import time
import re


def find_ffplay():
    return shutil.which("ffplay")


def list_sys_drm_connectors():
    sysdrm = "/sys/class/drm"
    connectors = []
    if not os.path.isdir(sysdrm):
        return connectors
    for entry in sorted(os.listdir(sysdrm)):
        # typical entries: card0, card0-HDMI-A-1, card0-HDMI-A-2, renderD128, version
        if entry.startswith("card") and "-" in entry:
            path = os.path.join(sysdrm, entry)
            info = {
                "name": entry,
                "path": path,
                "status": "unknown",
                "first_mode": None,
            }
            try:
                with open(os.path.join(path, "status"), "r") as f:
                    info["status"] = f.read().strip()
            except Exception:
                pass
            try:
                with open(os.path.join(path, "modes"), "r") as f:
                    modes = [ln.strip() for ln in f.readlines() if ln.strip()]
                    info["first_mode"] = modes[0] if modes else None
            except Exception:
                pass
            connectors.append(info)
    return connectors


def print_sys_connectors(conn):
    if not conn:
        print("no /sys/class/drm connectors found.")
        return
    print("sysfs DRM connectors (/sys/class/drm):")
    for c in conn:
        print(f"  {c['name']:20} status={c['status']:9} mode={c['first_mode']}")


def modetest_list_connectors():
    modetest = shutil.which("modetest")
    if not modetest:
        return None
    try:
        p = subprocess.run(
            [modetest, "-c"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        out = p.stdout.splitlines()
    except subprocess.CalledProcessError:
        return None

    connectors = []
    i = 0
    # find "Connectors:" header
    while i < len(out) and "Connectors:" not in out[i]:
        i += 1
    if i >= len(out):
        return None
    i += 1  # skip "Connectors:" line
    # skip header line (column names)
    if i < len(out) and out[i].strip().startswith("id"):
        i += 1

    # parse connector blocks
    while i < len(out):
        line = out[i]
        if not line.strip():
            i += 1
            continue
        # expected form: "<id> <encoder> <status> <type> ..."
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\w+)\s+(\S+)", line)
        if m:
            cid = int(m.group(1))
            status = m.group(3)
            ctype = m.group(4)
            modes = []
            i += 1
            # look for the "modes:" block
            while i < len(out) and "modes:" not in out[i]:
                i += 1
            if i < len(out) and "modes:" in out[i]:
                i += 1
                # collect subsequent indented mode lines
                while i < len(out) and out[i].strip():
                    mode_line = out[i].strip()
                    # mode_line often begins with resolution like "1920x1080"
                    mm = re.match(r"(\d+x\d+)", mode_line)
                    if mm:
                        modes.append(mm.group(1))
                    i += 1
            connectors.append(
                {"id": cid, "type": ctype, "status": status, "modes": modes}
            )
        else:
            i += 1
    return connectors


def modetest_set_mode(connector_id, mode):
    modetest = shutil.which("modetest")
    if not modetest:
        raise RuntimeError(
            "modetest not installed (libdrm-tests). Install it to use --connector-id forcing."
        )
    # Use sudo because modetest usually needs root to set modes
    cmd = ["sudo", modetest, "-s", f"{connector_id}:{mode}"]
    print("running:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def try_play_ffplay(video_path, backend_env=None, detach=False, timeout_probe=2):
    ffplay = find_ffplay()
    if not ffplay:
        raise RuntimeError("ffplay not found. Install ffmpeg (ffplay).")
    env = os.environ.copy()
    if backend_env:
        env.update(backend_env)
    cmd = [ffplay, "-fs", "-autoexit", "-hide_banner", "-loglevel", "error", video_path]
    devnull = open(os.devnull, "wb")
    preexec = None
    if detach:
        # detach subprocess so it survives SSH logout
        preexec = os.setsid
    print(
        "launching ffplay with SDL_VIDEODRIVER="
        + env.get("SDL_VIDEODRIVER", "<default>")
    )
    proc = subprocess.Popen(
        cmd, env=env, stdout=devnull, stderr=devnull, preexec_fn=preexec
    )
    # probe quickly: if process exits immediately it's probably an error in the chosen backend
    time.sleep(timeout_probe)
    code = proc.poll()
    if code is None:
        # process is still running -> likely successful
        if detach:
            print(
                f"ffplay started (pid {proc.pid}) detached; you can safely close the SSH session."
            )
            return 0
        else:
            # wait until finished
            return proc.wait()
    else:
        # exited quickly
        return code


def main():
    parser = argparse.ArgumentParser(
        description="Play MP4 on HDMI from console (ffplay)."
    )
    parser.add_argument("video", nargs="?", help="video file (MP4)")
    parser.add_argument(
        "--list-connectors",
        action="store_true",
        help="list /sys/class/drm connectors and (if available) modetest connector ids",
    )
    parser.add_argument(
        "--connector-id",
        type=int,
        help="(optional) modetest connector id to enable before playing (requires modetest and sudo)",
    )
    parser.add_argument(
        "--force-mode",
        action="store_true",
        help="when used with --connector-id try to set the connector's first mode via modetest (requires sudo)",
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "kmsdrm", "fbcon", "default"],
        default="auto",
        help="video backend to try",
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help="detach ffplay so it keeps running after SSH logout",
    )
    args = parser.parse_args()

    if args.list_connectors:
        syscon = list_sys_drm_connectors()
        print_sys_connectors(syscon)
        mt = modetest_list_connectors()
        if mt is None:
            print(
                "\nmodetest not available or parsing failed; install libdrm-tests to get connector ids (optional)."
            )
        else:
            print("\nmodetest connectors (ids):")
            for c in mt:
                print(
                    f"  id={c['id']:3}  type={c['type']:8}  status={c['status']:10}  modes={c['modes'][:3]}"
                )
        return

    if not args.video:
        parser.print_help()
        sys.exit(1)

    if not os.path.exists(args.video):
        print("video not found:", args.video)
        sys.exit(2)

    # If user asked to set a connector id before play, try to get its mode then set it via modetest
    if args.connector_id is not None:
        mt = modetest_list_connectors()
        if mt is None:
            print(
                "modetest not available or could not parse connectors. Install libdrm-tests and re-run."
            )
            print(
                "You can still try to play; the OS may already route output to the correct HDMI."
            )
        else:
            found = [c for c in mt if c["id"] == args.connector_id]
            if not found:
                print(f"connector id {args.connector_id} not listed by modetest.")
            else:
                c = found[0]
                if c["status"] != "connected":
                    print(
                        f"connector id {args.connector_id} status={c['status']}; forcing mode may still enable it."
                    )
                if not c["modes"]:
                    print(
                        f"connector id {args.connector_id} has no reported modes; cannot set."
                    )
                else:
                    chosen_mode = c["modes"][0]  # pick the first available mode
                    print(f"connector {args.connector_id} first mode: {chosen_mode}")
                    if args.force_mode:
                        print("attempting to set mode via modetest (will use sudo)...")
                        try:
                            modetest_set_mode(args.connector_id, chosen_mode)
                            print(
                                "modetest mode set returned (should be visible on HDMI)."
                            )
                            time.sleep(0.8)  # small pause for display to settle
                        except subprocess.CalledProcessError as e:
                            print("modetest failed:", e)
                        except Exception as e:
                            print("could not run modetest:", e)
                    else:
                        print(
                            "pass --force-mode to call modetest and set the mode before playing."
                        )

    # decide backends to try
    backends = []
    if args.backend == "auto":
        backends = ["kmsdrm", "fbcon", "default"]
    else:
        backends = [args.backend]

    last_err = None
    for b in backends:
        env = {}
        if b == "kmsdrm":
            env["SDL_VIDEODRIVER"] = "kmsdrm"
        elif b == "fbcon":
            env["SDL_VIDEODRIVER"] = "fbcon"
            if os.path.exists("/dev/fb0"):
                env["SDL_FBDEV"] = "/dev/fb0"
        else:
            env = {}  # default SDL selection

        try:
            rc = try_play_ffplay(args.video, backend_env=env, detach=args.detach)
        except Exception as e:
            last_err = e
            rc = 1

        if rc == 0:
            # succeeded
            return
        else:
            print(f"backend {b} appeared to fail (rc={rc}); trying next backend.")
    # all backends failed
    print("all backends failed to start ffplay.")
    if last_err:
        print("last error:", last_err)
    sys.exit(3)


if __name__ == "__main__":
    main()

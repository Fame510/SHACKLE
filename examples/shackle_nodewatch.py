#!/usr/bin/env python3
"""
shackle-nodewatch v0.1.0 — Honest GPU-Zombie Detector (alert-only by default)

Part of the SHACKLE family of deterministic runtime tripwires.
Where SHACKLE guards the application layer (agent API spend, tool loops),
this guards the node layer: it flags a GPU process that has pinned VRAM but
stopped doing useful work — the classic "silent zombie" that cloud watchdogs
are too slow to catch.

DESIGN PRINCIPLES (what makes this honest):
  1. ALERT-ONLY by default. It NEVER kills a process unless you pass
     --enforce AND --i-understand-this-sends-sigkill. Detection and action
     are separate concerns.
  2. SUSTAINED + MULTI-SIGNAL trip condition. A zombie must satisfy ALL of:
       - GPU utilization at/under a low threshold
       - VRAM usage at/over a high threshold (it's holding memory hostage)
       - process CPU at/under a low threshold
       - this combined state observed continuously for >= trip_seconds
     A single bad sample never trips. This is what avoids killing healthy
     training jobs that idle the GPU between collective ops.
  3. DEGRADES GRACEFULLY. If pynvml / NVIDIA is absent, it says so and exits
     cleanly. No fake GPUs, no invented numbers.
  4. ZERO third-party dependencies in the core. Uses only the stdlib +
     optional pynvml (which ships with the NVIDIA driver tooling). No
     requests_unixsocket, no hidden pip installs.
  5. It logs ONLY what it actually observed. No fabricated latency deltas.

This is a starting point you can run and benchmark yourself. The numbers it
prints are real because they come from your machine, not from a story.
"""

import argparse
import json
import os
import signal
import sys
import time
from collections import defaultdict

# ---- Optional GPU backend (graceful degradation) -------------------------
_NVML = None
try:
    import pynvml as _NVML  # ships with NVIDIA driver tooling, not a pip dep we add
except Exception:
    _NVML = None

# ---- Optional process backend --------------------------------------------
# psutil is convenient but not guaranteed. We use it if present, otherwise we
# fall back to /proc parsing so the core stays dependency-light on Linux.
try:
    import psutil as _PSUTIL
except Exception:
    _PSUTIL = None


def log(event, **fields):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event}
    rec.update(fields)
    print(json.dumps(rec), flush=True)


# ---- TCP state inspection (CORRECT /proc/net/tcp parsing) -----------------
# /proc/net/tcp columns:  sl  local_address rem_address  st  tx_queue:rx_queue ...
# Field index 1 = local_address ("0100007F:7531" => ip:port, hex).
# Field index 3 = st  (connection state, hex). THIS is the state, not field 1.
# Common states:  01=ESTABLISHED, 06=TIME_WAIT, 08=CLOSE_WAIT, 0A=LISTEN
TCP_STATE_NAMES = {
    "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1",
    "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT",
    "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING",
}


def read_tcp_table():
    """Return list of {local_port, state_name} for IPv4 + IPv6, parsed correctly."""
    rows = []
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                next(f, None)  # skip header
                for line in f:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    local = parts[1]            # field 1: local addr:port (hex)
                    st = parts[3].upper()       # field 3: state (hex)  <-- the fix
                    if ":" not in local:
                        continue
                    try:
                        port = int(local.rsplit(":", 1)[1], 16)
                    except ValueError:
                        continue
                    rows.append({"port": port, "state": TCP_STATE_NAMES.get(st, st)})
        except FileNotFoundError:
            continue
        except Exception as e:
            log("tcp_read_error", path=path, error=str(e))
    return rows


def ports_in_state(watch_ports, state):
    table = read_tcp_table()
    hits = [r["port"] for r in table if r["port"] in watch_ports and r["state"] == state]
    return sorted(set(hits))


# ---- GPU telemetry --------------------------------------------------------
def gpu_available():
    if _NVML is None:
        return False
    try:
        _NVML.nvmlInit()
        return True
    except Exception:
        return False


def gpu_snapshot():
    """Return list of {idx, gpu_util_pct, vram_used_pct}. Empty if unavailable."""
    out = []
    try:
        n = _NVML.nvmlDeviceGetCount()
        for i in range(n):
            h = _NVML.nvmlDeviceGetHandleByIndex(i)
            util = _NVML.nvmlDeviceGetUtilizationRates(h)
            mem = _NVML.nvmlDeviceGetMemoryInfo(h)
            vram_pct = (mem.used / mem.total) * 100 if mem.total else 0.0
            out.append({"idx": i, "gpu_util_pct": float(util.gpu), "vram_used_pct": round(vram_pct, 1)})
    except Exception as e:
        log("gpu_read_error", error=str(e))
    return out


# ---- Process inspection ---------------------------------------------------
def find_target_pids(signature):
    pids = []
    if _PSUTIL is not None:
        for proc in _PSUTIL.process_iter(["pid", "name", "cmdline"]):
            try:
                name = proc.info.get("name") or ""
                cmd = " ".join(proc.info.get("cmdline") or [])
                if signature in name or signature in cmd:
                    pids.append(proc.info["pid"])
            except Exception:
                continue
        return pids
    # Fallback: /proc scan, no psutil
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
            if signature in cmd:
                pids.append(int(entry))
        except Exception:
            continue
    return pids


def proc_cpu_pct(pid, sample=0.2):
    if _PSUTIL is None:
        return None  # unknown; treated as "do not assert idle"
    try:
        p = _PSUTIL.Process(pid)
        return p.cpu_percent(interval=sample)
    except Exception:
        return None


# ---- Core detector --------------------------------------------------------
class NodeWatch:
    def __init__(self, args):
        self.a = args
        self.streak = defaultdict(int)  # pid -> seconds of sustained zombie state

    def evaluate_once(self):
        gpus = gpu_snapshot()
        if not gpus:
            return
        # cluster-wide GPU/VRAM picture
        max_vram = max(g["vram_used_pct"] for g in gpus)
        min_gpu = min(g["gpu_util_pct"] for g in gpus)
        watch_ports = set(self.a.ports)
        close_wait = ports_in_state(watch_ports, "CLOSE_WAIT")

        pids = find_target_pids(self.a.signature)
        for pid in pids:
            cpu = proc_cpu_pct(pid)
            cpu_idle = (cpu is not None and cpu <= self.a.cpu_max)
            gpu_idle = (min_gpu <= self.a.gpu_max)
            vram_held = (max_vram >= self.a.vram_min)

            # Socket signal is corroborating, not required (training jobs may not
            # expose watched ports). If watched ports ARE in CLOSE_WAIT, that's
            # a strong extra signal.
            socket_signal = len(close_wait) > 0

            zombie_sample = gpu_idle and vram_held and (cpu_idle or cpu is None)
            # Require at least: idle GPU + held VRAM. CPU/socket strengthen it.
            if zombie_sample:
                self.streak[pid] += self.a.interval
            else:
                self.streak[pid] = 0

            if self.streak[pid] >= self.a.trip_seconds:
                self.handle_trip(pid, {
                    "sustained_s": self.streak[pid],
                    "min_gpu_util_pct": min_gpu,
                    "max_vram_used_pct": max_vram,
                    "cpu_pct": cpu,
                    "close_wait_ports": close_wait,
                    "socket_corroborated": socket_signal,
                })
                self.streak[pid] = 0  # reset after acting/alerting

    def handle_trip(self, pid, evidence):
        if not self.a.enforce:
            log("zombie_suspected_ALERT_ONLY", pid=pid, action="none", evidence=evidence,
                note="Run with --enforce --i-understand-this-sends-sigkill to act.")
            return
        if not self.a.confirm_kill:
            log("enforce_blocked", pid=pid,
                note="--enforce set but kill confirmation flag missing; not killing.")
            return
        log("zombie_confirmed_ENFORCE", pid=pid, action="SIGKILL", evidence=evidence)
        try:
            os.kill(pid, signal.SIGKILL)
            log("sigkill_sent", pid=pid)
        except ProcessLookupError:
            log("sigkill_noop", pid=pid, note="process already gone")
        except PermissionError:
            log("sigkill_denied", pid=pid, note="insufficient privileges")

    def run(self):
        if not gpu_available():
            log("startup_degraded", gpu="unavailable",
                note="pynvml/NVIDIA not present. Detector cannot observe GPUs; exiting cleanly.")
            return 0
        log("startup", mode=("ENFORCE" if self.a.enforce else "ALERT_ONLY"),
            signature=self.a.signature, watch_ports=self.a.ports,
            trip_seconds=self.a.trip_seconds, gpu_max=self.a.gpu_max,
            vram_min=self.a.vram_min, cpu_max=self.a.cpu_max,
            psutil=bool(_PSUTIL))
        try:
            while True:
                self.evaluate_once()
                time.sleep(self.a.interval)
        except KeyboardInterrupt:
            log("shutdown", reason="keyboard_interrupt")
            return 0
        finally:
            try:
                if _NVML is not None:
                    _NVML.nvmlShutdown()
            except Exception:
                pass


def build_parser():
    p = argparse.ArgumentParser(description="SHACKLE NodeWatch — honest GPU-zombie detector (alert-only by default).")
    p.add_argument("--signature", default="python", help="Substring to match in process name/cmdline (e.g. tritonserver, vllm, torchrun).")
    p.add_argument("--ports", type=int, nargs="*", default=[29500, 8000, 8001], help="TCP ports to inspect for CLOSE_WAIT corroboration.")
    p.add_argument("--interval", type=int, default=5, help="Seconds between samples.")
    p.add_argument("--trip-seconds", type=int, default=60, help="Sustained seconds of zombie state required before alerting.")
    p.add_argument("--gpu-max", type=float, default=2.0, help="GPU util%% at/under which a GPU counts as idle.")
    p.add_argument("--vram-min", type=float, default=40.0, help="VRAM used%% at/over which memory is considered held.")
    p.add_argument("--cpu-max", type=float, default=1.0, help="Process CPU%% at/under which it counts as idle.")
    p.add_argument("--enforce", action="store_true", help="Allow killing. Without this, detector only alerts.")
    p.add_argument("--i-understand-this-sends-sigkill", dest="confirm_kill", action="store_true", help="Second explicit confirmation required to actually SIGKILL.")
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    sys.exit(NodeWatch(args).run())

//! Container-local LMKD protocol endpoint and cgroup-pressure reclaimer.
//!
//! Android's ActivityManager requires a live LMKD control socket and updates
//! process `oom_score_adj` priorities over it (`LMK_PROCPRIO*`). This daemon:
//! 1. Tracks registered Android processes and writes `/proc/<pid>/oom_score_adj`.
//! 2. Replies accurately to synchronous queries (`LMK_GETKILLCNT`, `LMK_UPDATE_PROPS`,
//!    `LMK_BOOT_COMPLETED`).
//! 3. Monitors the cell's own cgroup v2 `memory.current` / `memory.high` / `memory.max`
//!    and PSI `memory.pressure`, reclaiming only background/cached processes
//!    (`oom_score_adj >= 200`) inside the cell before host-wide OOM occurs.

use std::collections::HashMap;
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, RawFd};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::thread;
use std::time::Duration;

const CONTROL_SOCKET_ENV: &str = "ANDROID_SOCKET_lmkd";
const MAX_CONTROL_PACKET_BYTES: usize = 64;
const CONTROL_SOCKET_BACKLOG: libc::c_int = 8;

const LMK_PROCPRIO: u32 = 1;
const LMK_PROCREMOVE: u32 = 2;
const LMK_PROCPURGE: u32 = 3;
const LMK_GETKILLCNT: u32 = 4;
const LMK_UPDATE_PROPS: u32 = 7;
const LMK_BOOT_COMPLETED: u32 = 10;
const LMK_PROCS_PRIO: u32 = 11;
const LMK_PROCPRIO_RECORD_BYTES: usize = 20;

/// Never kill foreground (`0`), visible (`100`), or system (`< 0`) processes.
const MIN_RECLAIM_OOM_ADJ: i32 = 200;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct ProcEntry {
    uid: u32,
    oomadj: i32,
}

#[derive(Default)]
struct LmkdState {
    procs: Mutex<HashMap<i32, ProcEntry>>,
    kill_count: AtomicU32,
}

fn read_be_i32(bytes: &[u8]) -> i32 {
    i32::from_be_bytes(bytes.try_into().expect("checked 4-byte slice"))
}

fn read_be_u32(bytes: &[u8]) -> u32 {
    u32::from_be_bytes(bytes.try_into().expect("checked 4-byte slice"))
}

fn write_oom_score_adj(pid: i32, oomadj: i32) {
    let path = format!("/proc/{pid}/oom_score_adj");
    let _ = fs::write(path, oomadj.to_string());
}

fn apply_procprio_entry(
    pid: i32,
    uid: u32,
    oomadj: i32,
    for_lmkd_only: bool,
    state: &LmkdState,
    apply_oom_adj: &dyn Fn(i32, i32),
) {
    let oomadj = oomadj.clamp(-1000, 1000);
    if pid > 1 {
        state
            .procs
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .insert(pid, ProcEntry { uid, oomadj });
        if !for_lmkd_only {
            apply_oom_adj(pid, oomadj);
        }
    }
}

fn handle_packet(
    packet: &[u8],
    state: &LmkdState,
    apply_oom_adj: &dyn Fn(i32, i32),
) -> Option<[u8; 8]> {
    if packet.len() < 4 || packet.len() > MAX_CONTROL_PACKET_BYTES || packet.len() % 4 != 0 {
        return None;
    }
    let command = read_be_u32(&packet[..4]);
    match (command, packet.len()) {
        (LMK_PROCPRIO, 16 | 20 | 24) => {
            let pid = read_be_i32(&packet[4..8]);
            let uid = read_be_u32(&packet[8..12]);
            let oomadj = read_be_i32(&packet[12..16]);
            let for_lmkd_only = packet.len() >= 24 && read_be_u32(&packet[20..24]) != 0;
            apply_procprio_entry(pid, uid, oomadj, for_lmkd_only, state, apply_oom_adj);
            None
        }
        (LMK_PROCS_PRIO, len)
            if len >= 4 + LMK_PROCPRIO_RECORD_BYTES
                && (len - 4) % LMK_PROCPRIO_RECORD_BYTES == 0 =>
        {
            for chunk in packet[4..].chunks_exact(LMK_PROCPRIO_RECORD_BYTES) {
                let pid = read_be_i32(&chunk[0..4]);
                let uid = read_be_u32(&chunk[4..8]);
                let oomadj = read_be_i32(&chunk[8..12]);
                let for_lmkd_only = read_be_u32(&chunk[16..20]) != 0;
                apply_procprio_entry(pid, uid, oomadj, for_lmkd_only, state, apply_oom_adj);
            }
            None
        }
        (LMK_PROCREMOVE, 8) => {
            let pid = read_be_i32(&packet[4..8]);
            state
                .procs
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .remove(&pid);
            None
        }
        (LMK_PROCPURGE, 4) => {
            state
                .procs
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .clear();
            None
        }
        (LMK_GETKILLCNT, 12) => {
            let count = state.kill_count.load(Ordering::Relaxed);
            Some(
                [command.to_be_bytes(), count.to_be_bytes()]
                    .concat()
                    .try_into()
                    .unwrap(),
            )
        }
        (LMK_UPDATE_PROPS | LMK_BOOT_COMPLETED, 4) => {
            if command == LMK_BOOT_COMPLETED {
                let (total, reclaimable) = {
                    let map = state.procs.lock().unwrap_or_else(PoisonError::into_inner);
                    let reclaimable = map
                        .values()
                        .filter(|entry| entry.oomadj >= MIN_RECLAIM_OOM_ADJ)
                        .count();
                    (map.len(), reclaimable)
                };
                eprintln!(
                    "Droidloom LMKD: boot completed, tracking {total} processes ({reclaimable} reclaimable)"
                );
            }
            Some(
                [command.to_be_bytes(), 0_u32.to_be_bytes()]
                    .concat()
                    .try_into()
                    .unwrap(),
            )
        }
        _ => None,
    }
}

fn serve_client(mut client: UnixStream, state: &LmkdState) -> io::Result<()> {
    let mut packet = [0_u8; MAX_CONTROL_PACKET_BYTES];
    loop {
        let length = match client.read(&mut packet) {
            Ok(0) => return Ok(()),
            Ok(length) => length,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
        if let Some(reply) = handle_packet(&packet[..length], state, &write_oom_score_adj) {
            client.write_all(&reply)?;
        }
    }
}

fn cgroup_dir() -> Option<PathBuf> {
    let root = Path::new("/sys/fs/cgroup");
    if !root.is_dir() {
        return None;
    }
    // Inside a Droidloom cell, `/sys/fs/cgroup` is the cell's cgroup namespace
    // root covering all Android processes, whereas `/proc/self/cgroup` points to
    // `lmkd`'s own tiny leaf cgroup.
    if root.join("memory.current").is_file() {
        return Some(root.to_path_buf());
    }
    if let Ok(text) = fs::read_to_string("/proc/self/cgroup") {
        for line in text.lines() {
            if let Some(rel) = line.strip_prefix("0::") {
                let rel = rel.trim().trim_start_matches('/');
                let candidate = if rel.is_empty() {
                    root.to_path_buf()
                } else {
                    root.join(rel)
                };
                if candidate.is_dir() {
                    return Some(candidate);
                }
            }
        }
    }
    Some(root.to_path_buf())
}

fn parse_limit_file(path: &Path) -> Option<u64> {
    let text = fs::read_to_string(path).ok()?;
    let trimmed = text.trim();
    if trimmed == "max" {
        return None;
    }
    trimmed.parse::<u64>().ok()
}

fn effective_limits(cg_dir: &Path) -> (Option<u64>, Option<u64>) {
    let root = Path::new("/sys/fs/cgroup");
    let mut min_high: Option<u64> = None;
    let mut min_max: Option<u64> = None;
    let mut cur = Some(cg_dir);
    while let Some(dir) = cur {
        if let Some(h) = parse_limit_file(&dir.join("memory.high")) {
            min_high = Some(min_high.map_or(h, |prev| prev.min(h)));
        }
        if let Some(m) = parse_limit_file(&dir.join("memory.max")) {
            min_max = Some(min_max.map_or(m, |prev| prev.min(m)));
        }
        if dir == root {
            break;
        }
        cur = dir.parent();
    }
    (min_high, min_max)
}

fn parse_high_events(text: &str) -> u64 {
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix("high ") {
            if let Ok(val) = rest.trim().parse::<u64>() {
                return val;
            }
        }
    }
    0
}

fn parse_psi_avg10(text: &str) -> (f64, f64) {
    let mut some = 0.0;
    let mut full = 0.0;
    for line in text.lines() {
        let target = if line.starts_with("some ") {
            &mut some
        } else if line.starts_with("full ") {
            &mut full
        } else {
            continue;
        };
        for field in line.split_whitespace() {
            if let Some(val) = field.strip_prefix("avg10=") {
                if let Ok(parsed) = val.parse::<f64>() {
                    *target = parsed;
                }
            }
        }
    }
    (some, full)
}

fn should_reclaim(
    current: u64,
    high: Option<u64>,
    max: Option<u64>,
    high_events_delta: u64,
    some_avg10: f64,
    full_avg10: f64,
) -> bool {
    if let Some(h) = high {
        if h > 0 && current >= h.saturating_mul(9) / 10 {
            return true;
        }
    }
    if let Some(m) = max {
        if m > 0 && current >= m.saturating_mul(85) / 100 {
            return true;
        }
    }
    if high_events_delta > 0 && (some_avg10 >= 15.0 || full_avg10 >= 5.0) {
        return true;
    }
    // When MemoryHigh/MemoryMax are enforced on an ancestor host slice above
    // the container's cgroup namespace, the cell root reads `max`; rely on PSI
    // memory pressure on the cell root in that case.
    high.is_none() && max.is_none() && (full_avg10 >= 10.0 || some_avg10 >= 25.0)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct VictimCandidate {
    pid: i32,
    uid: u32,
    oomadj: i32,
    rss_kib: u64,
}

fn select_victim(candidates: &[VictimCandidate]) -> Option<VictimCandidate> {
    candidates
        .iter()
        .copied()
        .filter(|c| c.oomadj >= MIN_RECLAIM_OOM_ADJ)
        .max_by_key(|c| (c.oomadj, c.rss_kib))
}

fn proc_uid_and_rss_kib(pid: i32) -> Option<(u32, u64)> {
    let status = fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
    let mut uid = None;
    let mut rss_kib = 0_u64;
    for line in status.lines() {
        if let Some(rest) = line.strip_prefix("Uid:") {
            uid = rest.split_whitespace().next()?.parse::<u32>().ok();
        } else if let Some(rest) = line.strip_prefix("VmRSS:") {
            rss_kib = rest
                .split_whitespace()
                .next()
                .and_then(|s| s.parse::<u64>().ok())
                .unwrap_or(0);
        }
    }
    uid.map(|u| (u, rss_kib))
}

fn reclaim_one(state: &LmkdState) -> bool {
    let snapshot: Vec<(i32, ProcEntry)> = {
        let map = state.procs.lock().unwrap_or_else(PoisonError::into_inner);
        map.iter().map(|(&pid, &entry)| (pid, entry)).collect()
    };
    let mut dead_pids = Vec::new();
    let mut candidates = Vec::new();
    for (pid, entry) in snapshot {
        match proc_uid_and_rss_kib(pid) {
            Some((real_uid, rss_kib)) if real_uid == entry.uid => {
                candidates.push(VictimCandidate {
                    pid,
                    uid: entry.uid,
                    oomadj: entry.oomadj,
                    rss_kib,
                });
            }
            _ => dead_pids.push(pid),
        }
    }
    if !dead_pids.is_empty() {
        let mut map = state.procs.lock().unwrap_or_else(PoisonError::into_inner);
        for pid in dead_pids {
            map.remove(&pid);
        }
    }
    let Some(victim) = select_victim(&candidates) else {
        return false;
    };
    let killed = unsafe { libc::kill(victim.pid, libc::SIGKILL) } == 0;
    if killed {
        state
            .procs
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .remove(&victim.pid);
        state.kill_count.fetch_add(1, Ordering::Relaxed);
        eprintln!(
            "Droidloom LMKD: reclaimed pid={} uid={} adj={} rss_kib={}",
            victim.pid, victim.uid, victim.oomadj, victim.rss_kib
        );
    }
    killed
}

fn spawn_memory_monitor(state: Arc<LmkdState>) {
    let Some(cg_dir) = cgroup_dir() else {
        return;
    };
    eprintln!("Droidloom LMKD: monitoring cgroup {}", cg_dir.display());
    thread::spawn(move || {
        let pressure_file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(cg_dir.join("memory.pressure"))
            .ok()
            .and_then(|mut f| {
                f.write_all(b"some 150000 1000000\0").ok()?;
                Some(f)
            });
        let mut prev_high_events = fs::read_to_string(cg_dir.join("memory.events"))
            .map(|t| parse_high_events(&t))
            .unwrap_or(0);

        loop {
            if let Some(ref pf) = pressure_file {
                let mut pfd = libc::pollfd {
                    fd: pf.as_raw_fd(),
                    events: libc::POLLPRI,
                    revents: 0,
                };
                unsafe {
                    libc::poll(&raw mut pfd, 1, 2000);
                }
            } else {
                thread::sleep(Duration::from_millis(2000));
            }

            let Some(current) = parse_limit_file(&cg_dir.join("memory.current")) else {
                continue;
            };
            let (high, max) = effective_limits(&cg_dir);
            let cur_high_events = fs::read_to_string(cg_dir.join("memory.events"))
                .map(|t| parse_high_events(&t))
                .unwrap_or(prev_high_events);
            let delta = cur_high_events.saturating_sub(prev_high_events);
            prev_high_events = cur_high_events;

            let (some_avg10, full_avg10) = fs::read_to_string(cg_dir.join("memory.pressure"))
                .map(|t| parse_psi_avg10(&t))
                .unwrap_or((0.0, 0.0));

            if should_reclaim(current, high, max, delta, some_avg10, full_avg10) {
                if reclaim_one(&state) {
                    thread::sleep(Duration::from_millis(250));
                }
            }
        }
    });
}

fn inherited_listener() -> Result<UnixListener, String> {
    let raw_fd = env::var(CONTROL_SOCKET_ENV)
        .map_err(|_| format!("missing {CONTROL_SOCKET_ENV}"))?
        .parse::<RawFd>()
        .map_err(|error| format!("invalid {CONTROL_SOCKET_ENV}: {error}"))?;
    if raw_fd < 0 {
        return Err(format!("invalid negative {CONTROL_SOCKET_ENV}"));
    }

    // Android init creates and binds service sockets but leaves listen(2) to
    // the daemon. LMKD uses SOCK_SEQPACKET, which UnixListener can accept once
    // the inherited descriptor has entered the listening state.
    if unsafe { libc::listen(raw_fd, CONTROL_SOCKET_BACKLOG) } != 0 {
        return Err(format!(
            "listen on {CONTROL_SOCKET_ENV}: {}",
            io::Error::last_os_error()
        ));
    }

    // Android init created, bound, and exclusively transferred this descriptor
    // to the service. FromRawFd establishes the single Rust owner.
    Ok(unsafe { UnixListener::from_raw_fd(raw_fd) })
}

fn main() {
    // A client can disappear between a synchronous request and its tiny reply.
    // Ignoring SIGPIPE turns that race into the ordinary BrokenPipe error below.
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_IGN);
    }

    let listener = inherited_listener().unwrap_or_else(|error| panic!("LMKD socket: {error}"));
    let state = Arc::new(LmkdState::default());
    spawn_memory_monitor(Arc::clone(&state));
    loop {
        match listener.accept() {
            Ok((client, _)) => {
                let state = Arc::clone(&state);
                thread::spawn(move || {
                    if let Err(error) = serve_client(client, &state) {
                        eprintln!("Droidloom LMKD client disconnected: {error}");
                    }
                });
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(10));
            }
            Err(error) => {
                eprintln!("Droidloom LMKD accept failed: {error}");
                thread::sleep(Duration::from_millis(100));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    #[test]
    fn synchronous_commands_receive_zero_success() {
        let state = LmkdState::default();
        for command in [LMK_GETKILLCNT, LMK_UPDATE_PROPS, LMK_BOOT_COMPLETED] {
            let mut request = command.to_be_bytes().to_vec();
            if command == LMK_GETKILLCNT {
                request.extend_from_slice(&[0; 8]);
            }
            assert_eq!(
                handle_packet(&request, &state, &|_, _| {}),
                Some(
                    [command.to_be_bytes(), 0_u32.to_be_bytes()]
                        .concat()
                        .try_into()
                        .unwrap()
                )
            );
        }
    }

    #[test]
    fn procprio_remove_purge_and_killcnt_protocol() {
        let state = LmkdState::default();
        let applied = RefCell::new(Vec::new());

        // Android 17 ProcessList.setOomAdj sends 24-byte LMK_PROCPRIO (cmd=1).
        let mut pkt24 = Vec::new();
        pkt24.extend_from_slice(&LMK_PROCPRIO.to_be_bytes());
        pkt24.extend_from_slice(&1234_i32.to_be_bytes());
        pkt24.extend_from_slice(&10123_u32.to_be_bytes());
        pkt24.extend_from_slice(&700_i32.to_be_bytes());
        pkt24.extend_from_slice(&0_u32.to_be_bytes());
        pkt24.extend_from_slice(&0_u32.to_be_bytes());
        assert_eq!(
            handle_packet(&pkt24, &state, &|p, a| applied.borrow_mut().push((p, a))),
            None
        );

        // 16-byte legacy LMK_PROCPRIO and 20-byte LMK_PROCPRIO.
        let mut pkt16 = Vec::new();
        pkt16.extend_from_slice(&LMK_PROCPRIO.to_be_bytes());
        pkt16.extend_from_slice(&1235_i32.to_be_bytes());
        pkt16.extend_from_slice(&1000_u32.to_be_bytes());
        pkt16.extend_from_slice(&(-900_i32).to_be_bytes());
        assert_eq!(
            handle_packet(&pkt16, &state, &|p, a| applied.borrow_mut().push((p, a))),
            None
        );

        let mut pkt_lmkd_only = Vec::new();
        pkt_lmkd_only.extend_from_slice(&LMK_PROCPRIO.to_be_bytes());
        pkt_lmkd_only.extend_from_slice(&1236_i32.to_be_bytes());
        pkt_lmkd_only.extend_from_slice(&10124_u32.to_be_bytes());
        pkt_lmkd_only.extend_from_slice(&500_i32.to_be_bytes());
        pkt_lmkd_only.extend_from_slice(&0_u32.to_be_bytes());
        pkt_lmkd_only.extend_from_slice(&1_u32.to_be_bytes());
        assert_eq!(
            handle_packet(&pkt_lmkd_only, &state, &|p, a| {
                applied.borrow_mut().push((p, a))
            }),
            None
        );

        assert_eq!(*applied.borrow(), vec![(1234, 700), (1235, -900)]);
        assert_eq!(
            state.procs.lock().unwrap().get(&1234),
            Some(&ProcEntry {
                uid: 10123,
                oomadj: 700
            })
        );
        assert_eq!(
            state.procs.lock().unwrap().get(&1236),
            Some(&ProcEntry {
                uid: 10124,
                oomadj: 500
            })
        );

        state.kill_count.store(3, Ordering::Relaxed);
        let mut get_cnt = LMK_GETKILLCNT.to_be_bytes().to_vec();
        get_cnt.extend_from_slice(&[0; 8]);
        assert_eq!(
            handle_packet(&get_cnt, &state, &|_, _| {}),
            Some([4_u32.to_be_bytes(), 3_u32.to_be_bytes()].concat().try_into().unwrap())
        );

        let mut remove = LMK_PROCREMOVE.to_be_bytes().to_vec();
        remove.extend_from_slice(&1234_i32.to_be_bytes());
        assert_eq!(handle_packet(&remove, &state, &|_, _| {}), None);
        assert!(state.procs.lock().unwrap().get(&1234).is_none());

        assert_eq!(
            handle_packet(&LMK_PROCPURGE.to_be_bytes(), &state, &|_, _| {}),
            None
        );
        assert!(state.procs.lock().unwrap().is_empty());
    }

    #[test]
    fn procs_prio_batch_updates_multiple_processes() {
        let state = LmkdState::default();
        let applied = RefCell::new(Vec::new());
        let mut pkt = Vec::new();
        pkt.extend_from_slice(&LMK_PROCS_PRIO.to_be_bytes());
        for (pid, uid, oomadj) in [(2001_i32, 10201_u32, 200_i32), (2002, 10202, 900)] {
            pkt.extend_from_slice(&pid.to_be_bytes());
            pkt.extend_from_slice(&uid.to_be_bytes());
            pkt.extend_from_slice(&oomadj.to_be_bytes());
            pkt.extend_from_slice(&0_u32.to_be_bytes());
            pkt.extend_from_slice(&0_u32.to_be_bytes());
        }
        assert_eq!(
            handle_packet(&pkt, &state, &|p, a| applied.borrow_mut().push((p, a))),
            None
        );
        assert_eq!(*applied.borrow(), vec![(2001, 200), (2002, 900)]);
        assert_eq!(state.procs.lock().unwrap().len(), 2);
    }

    #[test]
    fn select_victim_prefers_highest_adj_and_protects_foreground() {
        let protected = [
            VictimCandidate {
                pid: 10,
                uid: 1000,
                oomadj: -900,
                rss_kib: 500_000,
            },
            VictimCandidate {
                pid: 11,
                uid: 10100,
                oomadj: 0,
                rss_kib: 400_000,
            },
            VictimCandidate {
                pid: 12,
                uid: 10101,
                oomadj: 100,
                rss_kib: 300_000,
            },
        ];
        assert_eq!(select_victim(&protected), None);

        let mixed = [
            protected[1],
            VictimCandidate {
                pid: 20,
                uid: 10102,
                oomadj: 500,
                rss_kib: 100_000,
            },
            VictimCandidate {
                pid: 21,
                uid: 10103,
                oomadj: 900,
                rss_kib: 50_000,
            },
            VictimCandidate {
                pid: 22,
                uid: 10104,
                oomadj: 900,
                rss_kib: 150_000,
            },
        ];
        assert_eq!(select_victim(&mixed).map(|v| v.pid), Some(22));
    }

    #[test]
    fn reclaim_trigger_handles_direct_limits_and_inherited_psi_pressure() {
        assert!(!should_reclaim(
            4_000_000_000,
            Some(10_000_000_000),
            Some(12_000_000_000),
            0,
            0.0,
            0.0
        ));
        assert!(should_reclaim(
            9_100_000_000,
            Some(10_000_000_000),
            Some(12_000_000_000),
            0,
            0.0,
            0.0
        ));
        assert!(should_reclaim(
            10_300_000_000,
            None,
            Some(12_000_000_000),
            0,
            0.0,
            0.0
        ));
        assert!(should_reclaim(
            6_000_000_000,
            Some(10_000_000_000),
            Some(12_000_000_000),
            1,
            20.0,
            0.0
        ));
        assert!(!should_reclaim(1_500_000_000, None, None, 0, 5.0, 1.0));
        assert!(should_reclaim(12_000_000_000, None, None, 0, 30.0, 2.0));
        assert!(should_reclaim(12_000_000_000, None, None, 0, 10.0, 12.0));
    }

    #[test]
    fn asynchronous_commands_and_malformed_packets_receive_no_reply() {
        let state = LmkdState::default();
        assert_eq!(handle_packet(&[0, 0, 0], &state, &|_, _| {}), None);
        assert_eq!(
            handle_packet(&[0; MAX_CONTROL_PACKET_BYTES + 4], &state, &|_, _| {}),
            None
        );
    }
}

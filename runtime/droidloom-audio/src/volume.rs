//! Android's media volume on the host's default output.
//!
//! Droidloom's SystemUI makes Android's output an absolute-volume device:
//! Android plays at full scale and sends its music volume here, where it
//! becomes the volume of the host's default output, as the desktop's own
//! controls would set it. The host's level goes back to Android when it
//! connects and on every change made on the host, so the apps' sliders show
//! it. Levels are what the host's mixers show, the cube root of the linear
//! gain. Without a default output Android hears there is none.

use droidloom_device_protocol::{MAX_RECORD, Message};
use std::collections::{HashMap, VecDeque};
use std::ffi::{CString, c_char, c_int, c_void};
use std::io;
use std::os::fd::{AsFd, AsRawFd};
use std::ptr::NonNull;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, Instant};

use crate::{IpcError, SeqPacket};

type Report = unsafe extern "C" fn(*mut c_void, c_int, f32, c_int);

unsafe extern "C" {
    fn dl_volume_open(
        remote: *const c_char,
        report: Report,
        user: *mut c_void,
        output: *mut *mut c_void,
    ) -> i32;
    fn dl_volume_set(volume: *mut c_void, linear: f32, muted: c_int) -> i32;
    fn dl_volume_close(volume: *mut c_void);
    fn dl_pw_set_volume(stream: *mut c_void, linear: f32, muted: c_int) -> i32;
}

/// How long a level Android set may still come back from PipeWire.
const ECHO: Duration = Duration::from_secs(2);
/// Levels closer than this are the same level.
const SAME: f32 = 0.002;

/// What the connection last told Android, and what Android set lately.
struct Reporter {
    socket: Arc<SeqPacket>,
    state: Mutex<Heard>,
}

#[derive(Default)]
struct Heard {
    /// The last record sent: a level and mute, or `None` for unavailable.
    told: Option<Option<(f32, bool)>>,
    /// Levels Android set, newest last, while PipeWire may still report them.
    asked: VecDeque<((f32, bool), Instant)>,
}

impl Heard {
    /// The record for what PipeWire reports, unless Android knows it already.
    fn report(&mut self, level: Option<(f32, bool)>, now: Instant) -> Option<Message> {
        self.asked
            .retain(|(_, when)| now.saturating_duration_since(*when) < ECHO);
        if let Some((level, muted)) = level {
            // Android set it moments ago: a slider dragged on would jump back.
            if self.asked.iter().any(|((asked, asked_muted), _)| {
                (asked - level).abs() < SAME && *asked_muted == muted
            }) {
                self.told = Some(Some((level, muted)));
                return None;
            }
        }
        let same = match (self.told, level) {
            (Some(Some((told, told_muted))), Some((level, muted))) => {
                (told - level).abs() < SAME && told_muted == muted
            }
            (Some(None), None) => true,
            _ => false,
        };
        if same {
            return None;
        }
        self.told = Some(level);
        Some(match level {
            Some((level, muted)) => Message::VolumeState { level, muted },
            None => Message::VolumeUnavailable,
        })
    }

    fn asked(&mut self, level: f32, muted: bool, now: Instant) {
        self.asked.push_back(((level, muted), now));
        if self.asked.len() > 64 {
            self.asked.pop_front();
        }
    }
}

/// The level a host mixer shows for a linear gain.
fn level_of(linear: f32) -> f32 {
    linear.max(0.0).cbrt().clamp(0.0, 1.0)
}

unsafe extern "C" fn report(user: *mut c_void, available: c_int, linear: f32, muted: c_int) {
    // SAFETY: `user` is the Reporter the connection keeps alive until
    // dl_volume_close has stopped the loop that calls this.
    let reporter = unsafe { &*user.cast::<Reporter>() };
    if available < 0 {
        // PipeWire is gone: Android reconnects and finds it again.
        // SAFETY: shutdown takes the socket's open descriptor.
        unsafe { libc::shutdown(reporter.socket.as_fd().as_raw_fd(), libc::SHUT_RDWR) };
        return;
    }
    let level = (available > 0).then(|| (level_of(linear), muted != 0));
    let message = reporter
        .state
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .report(level, Instant::now());
    if let Some(message) = message {
        let _ = reporter.socket.send_record(&message.encode(), &[]);
    }
}

struct Volume(NonNull<c_void>);

impl Drop for Volume {
    fn drop(&mut self) {
        // SAFETY: unique owner; stops the loop before the reporter goes.
        unsafe { dl_volume_close(self.0.as_ptr()) };
    }
}

/// Serves one Android connection until it closes.
pub fn serve_volume(socket: SeqPacket, remote: Option<&str>) -> io::Result<()> {
    crate::init_pipewire();
    let reporter = Arc::new(Reporter {
        socket: Arc::new(socket),
        state: Mutex::new(Heard::default()),
    });
    let remote = remote
        .map(CString::new)
        .transpose()
        .map_err(|_| io::Error::other("PipeWire remote contains NUL"))?;
    let mut native = std::ptr::null_mut();
    // SAFETY: the callback and the reporter outlive the native object, which
    // `Volume` closes before `reporter` is dropped.
    let status = unsafe {
        dl_volume_open(
            remote
                .as_ref()
                .map_or(std::ptr::null(), |remote| remote.as_ptr()),
            report,
            Arc::as_ptr(&reporter).cast_mut().cast(),
            &raw mut native,
        )
    };
    if status < 0 {
        return Err(io::Error::from_raw_os_error(-status));
    }
    let volume = Volume(NonNull::new(native).ok_or_else(|| io::Error::other("no volume"))?);
    let mut bytes = vec![0; MAX_RECORD];
    let served = loop {
        let record = match reporter.socket.receive_record_into(&mut bytes) {
            Ok(record) => record,
            Err(IpcError::PeerClosed) => break Ok(()),
            Err(IpcError::Io(error)) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => break Err(io::Error::other(error)),
        };
        let Message::VolumeSet { level, muted } =
            Message::decode(record.bytes).map_err(io::Error::other)?
        else {
            break Err(io::Error::other("record for another endpoint"));
        };
        reporter
            .state
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .asked(level, muted, Instant::now());
        // SAFETY: the native object is open; it copies the values.
        let status = unsafe { dl_volume_set(volume.0.as_ptr(), level.powi(3), c_int::from(muted)) };
        if status < 0 {
            eprintln!(
                "droidloom-audio: host volume not set: {}",
                io::Error::from_raw_os_error(-status)
            );
        }
    };
    drop(volume);
    served
}

/// Serves one Android connection when host volume bridging is disabled.
pub fn serve_volume_disabled(socket: SeqPacket) -> io::Result<()> {
    socket
        .send_record(&Message::VolumeUnavailable.encode(), &[])
        .map_err(io::Error::other)?;
    let mut bytes = [0; MAX_RECORD];
    loop {
        match socket.receive_record_into(&mut bytes) {
            Ok(_) => {}
            Err(IpcError::PeerClosed) => return Ok(()),
            Err(IpcError::Io(error)) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error) => return Err(io::Error::other(error)),
        }
    }
}

struct StreamVolumeState {
    level: f32,
    muted: bool,
    streams: HashMap<u64, Arc<Mutex<Option<usize>>>>,
    peer: Option<(Arc<SeqPacket>, Heard)>,
}

/// Shared per-stream volume coordinator for desktop mode.
///
/// Android's media volume controls only Droidloom's own PipeWire playback streams,
/// never the host's default sink master volume.
pub struct StreamVolumeHub {
    state: Mutex<StreamVolumeState>,
}

impl StreamVolumeHub {
    /// Create a new per-stream volume coordinator starting at full unmuted level.
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            state: Mutex::new(StreamVolumeState {
                level: 1.0,
                muted: false,
                streams: HashMap::new(),
                peer: None,
            }),
        })
    }

    pub(crate) fn register_stream(
        &self,
        generation: u64,
        handle: Arc<Mutex<Option<usize>>>,
    ) -> (f32, bool) {
        let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
        state.streams.insert(generation, handle);
        (state.level.powi(3), state.muted)
    }

    pub(crate) fn unregister_stream(&self, generation: u64) {
        let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
        state.streams.remove(&generation);
    }

    /// Notify Android when an external desktop mixer adjusts a Droidloom stream's volume.
    pub(crate) fn on_stream_changed(&self, linear: f32, muted: bool) {
        let level = level_of(linear);
        let muted = muted;
        let send = {
            let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
            let now = Instant::now();
            let mut update_levels = false;
            let result = if let Some((socket, heard)) = state.peer.as_mut() {
                if let Some(msg) = heard.report(Some((level, muted)), now) {
                    update_levels = true;
                    Some((Arc::clone(socket), msg))
                } else {
                    None
                }
            } else {
                update_levels = true;
                None
            };
            if update_levels {
                state.level = level;
                state.muted = muted;
            }
            result
        };
        if let Some((socket, msg)) = send {
            let _ = socket.send_record(&msg.encode(), &[]);
        }
    }

    /// Serves Android's media volume on Droidloom's own playback streams.
    pub fn serve_stream_volume(self: &Arc<Self>, socket: SeqPacket) -> io::Result<()> {
        let socket = Arc::new(socket);
        let initial = {
            let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
            let mut heard = Heard::default();
            let msg = heard.report(Some((state.level, state.muted)), Instant::now());
            state.peer = Some((Arc::clone(&socket), heard));
            msg
        };
        if let Some(msg) = initial {
            socket
                .send_record(&msg.encode(), &[])
                .map_err(io::Error::other)?;
        }

        let mut bytes = vec![0; MAX_RECORD];
        let served = loop {
            let record = match socket.receive_record_into(&mut bytes) {
                Ok(record) => record,
                Err(IpcError::PeerClosed) => break Ok(()),
                Err(IpcError::Io(error)) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => break Err(io::Error::other(error)),
            };
            let Message::VolumeSet { level, muted } =
                Message::decode(record.bytes).map_err(io::Error::other)?
            else {
                break Err(io::Error::other("record for another endpoint"));
            };
            let level = level.clamp(0.0, 1.0);
            let handles: Vec<Arc<Mutex<Option<usize>>>> = {
                let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
                state.level = level;
                state.muted = muted;
                if let Some((_, heard)) = state.peer.as_mut() {
                    heard.asked(level, muted, Instant::now());
                }
                state.streams.values().cloned().collect()
            };
            let linear = level.powi(3);
            for handle in handles {
                let guard = handle.lock().unwrap_or_else(PoisonError::into_inner);
                if let Some(ptr) = *guard {
                    // SAFETY: `Stream::drop` clears this handle to `None` before destroying `dl_pw`.
                    let status = unsafe { dl_pw_set_volume(ptr as *mut c_void, linear, c_int::from(muted)) };
                    if status < 0 {
                        eprintln!(
                            "droidloom-audio: stream volume not set: {}",
                            io::Error::from_raw_os_error(-status)
                        );
                    }
                }
            }
        };

        let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
        if state
            .peer
            .as_ref()
            .is_some_and(|(current, _)| Arc::ptr_eq(current, &socket))
        {
            state.peer = None;
        }
        served
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_changes_reach_android_once() {
        let now = Instant::now();
        let mut heard = Heard::default();
        assert!(matches!(
            heard.report(None, now),
            Some(Message::VolumeUnavailable)
        ));
        assert!(heard.report(None, now).is_none());
        assert!(matches!(
            heard.report(Some((0.4, false)), now),
            Some(Message::VolumeState { level, muted: false }) if (level - 0.4).abs() < 1e-6
        ));
        assert!(heard.report(Some((0.4001, false)), now).is_none());
        assert!(heard.report(Some((0.4, true)), now).is_some());
    }

    #[test]
    fn androids_own_levels_do_not_come_back() {
        let now = Instant::now();
        let mut heard = Heard::default();
        heard.report(Some((0.2, false)), now);
        for level in [0.3, 0.4, 0.5] {
            heard.asked(level, false, now);
        }
        // PipeWire reports the drag's steps after Android moved on.
        for level in [0.3, 0.4, 0.5] {
            assert!(heard.report(Some((level, false)), now).is_none());
        }
        // Long after, the same level from the host is news again.
        let later = now + ECHO + Duration::from_millis(1);
        assert!(heard.report(Some((0.3, false)), later).is_some());
    }

    #[test]
    fn mixer_levels_are_cube_roots() {
        assert!((level_of(0.125) - 0.5).abs() < 1e-6);
        assert_eq!(level_of(2.0), 1.0);
        assert_eq!(level_of(-1.0), 0.0);
    }

    #[test]
    fn stream_volume_hub_tracks_level_and_suppresses_echo() {
        let hub = StreamVolumeHub::new();
        let (a, b) = SeqPacket::pair().unwrap();
        let hub_worker = Arc::clone(&hub);
        let thread = std::thread::spawn(move || hub_worker.serve_stream_volume(a));

        let mut buf = [0u8; MAX_RECORD];
        let initial = Message::decode(b.receive_record_into(&mut buf).unwrap().bytes).unwrap();
        assert!(matches!(
            initial,
            Message::VolumeState { level, muted: false } if (level - 1.0).abs() < 1e-6
        ));

        // Android sets 0.5; echo of ~0.125 linear (0.5^3) from PipeWire is suppressed.
        b.send_record(
            &Message::VolumeSet {
                level: 0.5,
                muted: false,
            }
            .encode(),
            &[],
        )
        .unwrap();
        let handle = Arc::new(Mutex::new(None));
        let deadline = Instant::now() + Duration::from_secs(2);
        let (linear, muted) = loop {
            let cur = hub.register_stream(42, Arc::clone(&handle));
            if (cur.0 - 0.125).abs() < 1e-6 || Instant::now() >= deadline {
                break cur;
            }
            std::thread::sleep(Duration::from_millis(5));
        };
        assert!((linear - 0.125).abs() < 1e-6);
        assert!(!muted);

        hub.on_stream_changed(0.125, false);
        // External mixer change to 0.25^3 = 0.015625 must reach Android.
        hub.on_stream_changed(0.015625, true);
        let external = Message::decode(b.receive_record_into(&mut buf).unwrap().bytes).unwrap();
        assert!(matches!(
            external,
            Message::VolumeState { level, muted: true } if (level - 0.25).abs() < 1e-3
        ));

        hub.unregister_stream(42);
        drop(b);
        thread.join().unwrap().unwrap();
    }
}

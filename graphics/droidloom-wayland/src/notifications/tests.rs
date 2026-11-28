use super::*;
use std::{
    io::{BufRead, BufReader},
    process::{Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicU32, Ordering},
    },
};
use zbus::zvariant::OwnedValue;
#[derive(Debug)]
enum Observed {
    Posted(
        u32,
        u32,
        String,
        Vec<String>,
        HashMap<String, OwnedValue>,
        i32,
    ),
    Closed(u32),
}
struct Server {
    next: Arc<AtomicU32>,
    events: async_channel::Sender<Observed>,
}
#[zbus::interface(name = "org.freedesktop.Notifications")]
impl Server {
    fn get_capabilities(&self) -> Vec<&str> {
        vec!["actions", "body", "body-markup", "icon-static"]
    }
    async fn notify(
        &self,
        _app: &str,
        replaces: u32,
        _icon: &str,
        _title: &str,
        body: &str,
        actions: Vec<String>,
        hints: HashMap<String, OwnedValue>,
        expire_timeout: i32,
    ) -> u32 {
        let id = if replaces != 0 {
            replaces
        } else {
            self.next.fetch_add(1, Ordering::SeqCst)
        };
        self.events
            .send(Observed::Posted(
                id,
                replaces,
                body.to_string(),
                actions,
                hints,
                expire_timeout,
            ))
            .await
            .unwrap();
        id
    }
    async fn close_notification(&self, id: u32) {
        self.events.send(Observed::Closed(id)).await.unwrap();
    }
}
struct Bus(std::process::Child);
impl Drop for Bus {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
fn note(version: &str) -> Note {
    Note {
        key: "0|test.app|1".into(),
        version: version.into(),
        package: "test.app".into(),
        app: "Test".into(),
        title: "Hello".into(),
        body: "<b>text & more</b>".into(),
        actions: vec![
            Action {
                key: "default".into(),
                label: "Open".into(),
            },
            Action {
                key: "action-0".into(),
                label: "Action".into(),
            },
        ],
        urgency: 1,
        ongoing: false,
        clearable: true,
        icon: "7f".repeat(48 * 48 * 4),
        alert: true,
        group: String::new(),
        summary: false,
        when: 1_700_000_000_000,
        progress: -1,
        category: String::new(),
        user: 0,
        component: String::new(),
        large_icon: String::new(),
        messages: vec![],
    }
}
async fn frame(socket: &Async<UnixStream>, value: serde_json::Value) {
    let bytes = serde_json::to_vec(&value).unwrap();
    let header = (bytes.len() as u32).to_be_bytes();
    // Exercise headers and bodies split at non-record boundaries.
    for b in header {
        (&*socket).write_all(&[b]).await.unwrap();
    }
    for chunk in bytes.chunks(317) {
        (&*socket).write_all(chunk).await.unwrap();
    }
}
async fn snapshot(socket: &Async<UnixStream>, notes: &[Note]) {
    frame(socket, serde_json::json!({"type":"begin"})).await;
    for n in notes {
        frame(socket, serde_json::json!({"type":"post","notification":n})).await;
    }
    frame(socket, serde_json::json!({"type":"end"})).await;
}
async fn timeout<T>(f: impl std::future::Future<Output = T>) -> T {
    future::race(f, async {
        async_io::Timer::after(Duration::from_secs(5)).await;
        panic!("notification test timed out")
    })
    .await
}
async fn reply(socket: &Async<UnixStream>) -> serde_json::Value {
    let mut header = [0; 4];
    timeout((&*socket).read_exact(&mut header)).await.unwrap();
    let mut bytes = vec![0; u32::from_be_bytes(header) as usize];
    timeout((&*socket).read_exact(&mut bytes)).await.unwrap();
    serde_json::from_slice(&bytes).unwrap()
}
async fn quiet(rx: &async_channel::Receiver<Observed>) {
    future::race(
        async { panic!("unexpected desktop event: {:?}", rx.recv().await) },
        async {
            async_io::Timer::after(Duration::from_millis(100)).await;
        },
    )
    .await
}
#[test]
fn dbus_and_android_socket_lifecycle() {
    let mut child = Command::new("dbus-daemon")
        .args(["--session", "--nofork", "--print-address=1"])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut address = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut address)
        .unwrap();
    let _bus = Bus(child);
    future::block_on(async {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("notification.sock");
        let listener = Async::new(UnixListener::bind(&path).unwrap()).unwrap();
        let (tx, rx) = async_channel::bounded(128);
        let server = zbus::connection::Builder::address(address.trim())
            .unwrap()
            .name(NAME)
            .unwrap()
            .serve_at(
                PATH,
                Server {
                    next: Arc::new(AtomicU32::new(100)),
                    events: tx.clone(),
                },
            )
            .unwrap()
            .build()
            .await
            .unwrap();
        let conn = zbus::connection::Builder::address(address.trim())
            .unwrap()
            .build()
            .await
            .unwrap();
        let state = dir.path().join("state");
        let grants = HostGrants::default();
        let bridge = serve(&listener, &state, conn, unsafe { libc::getuid() }, &grants);
        let checks = async {
            let socket = Async::<UnixStream>::connect(&path).await.unwrap();
            frame(&socket, serde_json::json!({"type":"hello","abi":3})).await;
            assert_eq!(
                reply(&socket).await,
                serde_json::json!({"type":"hello","abi":3})
            );
            snapshot(&socket, &[note("v1")]).await;
            match timeout(rx.recv()).await.unwrap() {
                Observed::Posted(100, 0, body, actions, hints, expire_timeout) => {
                    assert_eq!(expire_timeout, -1);
                    assert_eq!(body, "&lt;b&gt;text &amp; more&lt;/b&gt;");
                    assert_eq!(actions, ["default", "Open", "action-0", "Action"]);
                    assert!(hints.contains_key("image-data"));
                    assert!(bool::try_from(hints.get("suppress-sound").unwrap()).unwrap());
                    // A snapshot replays what Android already showed.
                    assert!(!bool::try_from(hints.get("x-denial-alert").unwrap()).unwrap());
                }
                other => panic!("{other:?}"),
            }
            frame(
                &socket,
                serde_json::json!({"type":"post","notification":note("v1")}),
            )
            .await;
            quiet(&rx).await;
            frame(
                &socket,
                serde_json::json!({"type":"post","notification":note("v2")}),
            )
            .await;
            match timeout(rx.recv()).await.unwrap() {
                Observed::Posted(100, 100, _, _, hints, _) => {
                    // A live post carries Android's own heads-up decision.
                    assert!(bool::try_from(hints.get("x-denial-alert").unwrap()).unwrap());
                    assert_eq!(
                        i64::try_from(hints.get("x-denial-time").unwrap()).unwrap(),
                        1_700_000_000_000
                    );
                    assert!(!hints.contains_key("desktop-entry"));
                }
                other => panic!("{other:?}"),
            }
            // The host's token for this tap goes to the app's next activation.
            for (member, body) in [
                ("ActivationToken", (100u32, "host-token")),
                ("ActionInvoked", (100u32, "action-0")),
            ] {
                server
                    .emit_signal(None::<&str>, PATH, NAME, member, &body)
                    .await
                    .unwrap();
            }
            let action = reply(&socket).await;
            assert_eq!(action["type"], "action");
            assert_eq!(action["version"], "v2");
            assert_eq!(action["action"], "action-0");
            assert_eq!(grants.take("test.app").as_deref(), Some("host-token"));
            assert_eq!(grants.take("test.app"), None);
            // A different bus peer cannot forge notification-server actions.
            let stranger = zbus::connection::Builder::address(address.trim())
                .unwrap()
                .build()
                .await
                .unwrap();
            stranger
                .emit_signal(
                    None::<&str>,
                    PATH,
                    NAME,
                    "ActionInvoked",
                    &(100u32, "default"),
                )
                .await
                .unwrap();
            server
                .emit_signal(
                    None::<&str>,
                    PATH,
                    NAME,
                    "NotificationClosed",
                    &(100u32, 1u32),
                )
                .await
                .unwrap();
            snapshot(&socket, &[note("v2")]).await;
            quiet(&rx).await;
            snapshot(&socket, &[note("v3")]).await;
            assert!(matches!(
                timeout(rx.recv()).await.unwrap(),
                Observed::Posted(101, 0, ..)
            ));
            server
                .emit_signal(
                    None::<&str>,
                    PATH,
                    NAME,
                    "NotificationClosed",
                    &(101u32, 2u32),
                )
                .await
                .unwrap();
            let dismissal = reply(&socket).await;
            assert_eq!(dismissal["type"], "dismiss");
            assert_eq!(dismissal["version"], "v3");
            snapshot(&socket, &[]).await;
            quiet(&rx).await;
            snapshot(&socket, &[note("v4")]).await;
            assert!(matches!(
                timeout(rx.recv()).await.unwrap(),
                Observed::Posted(102, 0, ..)
            ));
            // Reconnecting Android sends a snapshot without duplicating an unchanged item.
            drop(socket);
            let socket = Async::<UnixStream>::connect(&path).await.unwrap();
            frame(&socket, serde_json::json!({"type":"hello","abi":3})).await;
            assert_eq!(
                reply(&socket).await,
                serde_json::json!({"type":"hello","abi":3})
            );
            snapshot(&socket, &[note("v4")]).await;
            quiet(&rx).await;
            frame(
                &socket,
                serde_json::json!({"type":"remove","key":note("v4").key,"version":"v4"}),
            )
            .await;
            assert!(matches!(
                timeout(rx.recv()).await.unwrap(),
                Observed::Closed(102)
            ));
            snapshot(&socket, &[note("v5")]).await;
            assert!(matches!(
                timeout(rx.recv()).await.unwrap(),
                Observed::Posted(103, 0, ..)
            ));
            server.release_name(NAME).await.unwrap();
            let replacement = zbus::connection::Builder::address(address.trim())
                .unwrap()
                .name(NAME)
                .unwrap()
                .serve_at(
                    PATH,
                    Server {
                        next: Arc::new(AtomicU32::new(200)),
                        events: tx,
                    },
                )
                .unwrap()
                .build()
                .await
                .unwrap();
            assert!(matches!(
                timeout(rx.recv()).await.unwrap(),
                Observed::Posted(200, 0, ..)
            ));
            replacement
                .emit_signal(
                    None::<&str>,
                    PATH,
                    NAME,
                    "ActionInvoked",
                    &(200u32, "default"),
                )
                .await
                .unwrap();
            assert_eq!(reply(&socket).await["action"], "default");
            // Android's serial-console notice is ongoing and not clearable, but its
            // desktop popup must still use the desktop's ordinary timeout.
            let mut ongoing = note("ongoing-v6");
            ongoing.ongoing = true;
            ongoing.clearable = false;
            ongoing.urgency = 0;
            snapshot(&socket, &[ongoing.clone()]).await;
            match timeout(rx.recv()).await.unwrap() {
                Observed::Posted(200, 200, _, _, hints, expire_timeout) => {
                    assert_eq!(expire_timeout, -1);
                    assert!(bool::try_from(hints.get("resident").unwrap()).unwrap());
                }
                other => panic!("{other:?}"),
            }
            replacement
                .emit_signal(
                    None::<&str>,
                    PATH,
                    NAME,
                    "NotificationClosed",
                    &(200u32, 1u32),
                )
                .await
                .unwrap();
            snapshot(&socket, &[ongoing]).await;
            quiet(&rx).await;
            // An unrelated snapshot does not replay the expired ongoing popup.
            // Nor does expiry produce an Android cancellation command.
            let mut byte = [0];
            future::race(
                async {
                    panic!(
                        "unexpected Android command: {:?}",
                        (&socket).read(&mut byte).await
                    )
                },
                async {
                    async_io::Timer::after(Duration::from_millis(100)).await;
                },
            )
            .await;
        };
        future::race(
            async {
                bridge.await.unwrap();
                panic!("bridge stopped")
            },
            checks,
        )
        .await;
    });
}
#[test]
fn rejects_unbounded_or_invalid_frames() {
    let (a, _b) = UnixStream::pair().unwrap();
    let mut peer = Peer {
        socket: Async::new(a).unwrap(),
        buffer: vec![],
        hello: false,
        snapshot: None,
        known: BTreeMap::new(),
        synced: false,
    };
    assert!(
        peer.ingest(&((MAX_FRAME + 1) as u32).to_be_bytes())
            .is_err()
    );
    let mut n = note("v1");
    assert!(n.valid());
    n.icon.push('0');
    assert!(!n.valid());
    n.icon.clear();
    n.component = "other.app/.Main".into();
    assert!(!n.valid());
    n.component = "test.app/.Main".into();
    assert!(n.valid());
    n.actions[0].key = "arbitrary-intent".into();
    assert!(!n.valid());
}

#[test]
fn deltas_require_sync_and_stale_removal_cannot_delete_newer_versions() {
    fn encoded(value: serde_json::Value) -> Vec<u8> {
        let bytes = serde_json::to_vec(&value).unwrap();
        let mut frame = (bytes.len() as u32).to_be_bytes().to_vec();
        frame.extend(bytes);
        frame
    }
    let (socket, _other) = UnixStream::pair().unwrap();
    let mut peer = Peer {
        socket: Async::new(socket).unwrap(),
        buffer: vec![],
        hello: false,
        snapshot: None,
        known: BTreeMap::new(),
        synced: false,
    };
    peer.ingest(&encoded(serde_json::json!({"type":"hello","abi":3})))
        .unwrap();
    assert!(
        peer.ingest(&encoded(
            serde_json::json!({"type":"post","notification":note("v1")})
        ))
        .is_err()
    );
    peer.ingest(&encoded(serde_json::json!({"type":"begin"})))
        .unwrap();
    peer.ingest(&encoded(
        serde_json::json!({"type":"post","notification":note("v1")}),
    ))
    .unwrap();
    assert!(matches!(
        peer.ingest(&encoded(serde_json::json!({"type":"end"})))
            .unwrap()
            .as_slice(),
        [Update::Snapshot(_)]
    ));
    let key = note("v1").key;
    let updates = peer
        .ingest(&encoded(
            serde_json::json!({"type":"post","notification":note("v2")}),
        ))
        .unwrap();
    assert!(matches!(updates.as_slice(), [Update::Post(n)] if n.version == "v2"));
    assert!(
        peer.ingest(&encoded(
            serde_json::json!({"type":"remove","key":key,"version":"v1"})
        ))
        .unwrap()
        .is_empty()
    );
    assert_eq!(peer.known.get(&key).unwrap(), "v2");
    let update = encoded(serde_json::json!({"type":"remove","key":key,"version":"v2"}));
    for byte in &update[..update.len() - 1] {
        assert!(peer.ingest(&[*byte]).unwrap().is_empty());
    }
    assert!(matches!(
        peer.ingest(&update[update.len() - 1..]).unwrap().as_slice(),
        [Update::Remove { .. }]
    ));
    assert!(peer.known.is_empty());
    for index in 0..MAX_ACTIVE {
        let mut n = note("v1");
        n.key = format!("note-{index}");
        n.icon.clear();
        peer.ingest(&encoded(
            serde_json::json!({"type":"post","notification":n}),
        ))
        .unwrap();
    }
    assert_eq!(peer.known.len(), MAX_ACTIVE);
    // SystemUI retires the evicted entry first when replacing a full set.
    peer.ingest(&encoded(
        serde_json::json!({"type":"remove","key":"note-0","version":"v1"}),
    ))
    .unwrap();
    let mut replacement = note("v1");
    replacement.key = "replacement".into();
    replacement.icon.clear();
    peer.ingest(&encoded(
        serde_json::json!({"type":"post","notification":replacement}),
    ))
    .unwrap();
    assert_eq!(peer.known.len(), MAX_ACTIVE);
    replacement.key = "overflow".into();
    assert!(
        peer.ingest(&encoded(
            serde_json::json!({"type":"post","notification":replacement})
        ))
        .is_err()
    );
}

#[test]
fn launcher_activity_names_the_droidloom_desktop_entry_and_groups_cross() {
    let mut n = note("v1");
    n.component = "test.app/test.app.Main".into();
    n.icon.clear();
    n.large_icon = "ff".repeat(96 * 96 * 4);
    n.group = "0|test.app|g:chat".into();
    n.summary = true;
    n.progress = 40;
    n.clearable = false;
    n.messages = vec![ChatMessage {
        sender: "Ada".into(),
        text: "Hi".into(),
    }];
    assert!(n.valid());
    let (name, hints) = notify_hints(&n, false);
    assert_eq!(name, "");
    assert_eq!(
        <&str>::try_from(hints.get("desktop-entry").unwrap()).unwrap(),
        format!(
            "droidloom-{}",
            droidloom_contracts::applications::application_id(0, "test.app/test.app.Main")
        )
    );
    assert_eq!(
        <&str>::try_from(hints.get("x-denial-group").unwrap()).unwrap(),
        "0|test.app|g:chat"
    );
    assert!(bool::try_from(hints.get("x-denial-group-summary").unwrap()).unwrap());
    assert_eq!(i32::try_from(hints.get("value").unwrap()).unwrap(), 40);
    assert!(hints.contains_key("image-data"));
    assert!(hints.contains_key("x-denial-messages"));
    assert!(!bool::try_from(hints.get("x-denial-clearable").unwrap()).unwrap());
}

#[derive(Debug, zbus::DBusError)]
#[zbus(prefix = "org.freedesktop.Notifications.Error")]
enum NotifyError {
    #[zbus(error)]
    ZBus(zbus::Error),
    ExcessNotificationGeneration(String),
}

struct RateLimitedServer {
    next: Arc<AtomicU32>,
    reject: Arc<std::sync::atomic::AtomicBool>,
    events: async_channel::Sender<Observed>,
}
#[zbus::interface(name = "org.freedesktop.Notifications")]
impl RateLimitedServer {
    fn get_capabilities(&self) -> Vec<&str> {
        vec!["actions", "body", "body-markup", "icon-static"]
    }
    async fn notify(
        &self,
        _app: &str,
        replaces: u32,
        _icon: &str,
        _title: &str,
        body: &str,
        actions: Vec<String>,
        hints: HashMap<String, OwnedValue>,
        expire_timeout: i32,
    ) -> std::result::Result<u32, NotifyError> {
        if self.reject.load(Ordering::SeqCst) {
            return Err(NotifyError::ExcessNotificationGeneration(
                "Created too many similar notifications in quick succession".into(),
            ));
        }
        let id = if replaces != 0 {
            replaces
        } else {
            self.next.fetch_add(1, Ordering::SeqCst)
        };
        self.events
            .send(Observed::Posted(
                id,
                replaces,
                body.to_string(),
                actions,
                hints,
                expire_timeout,
            ))
            .await
            .unwrap();
        Ok(id)
    }
    async fn close_notification(&self, id: u32) {
        self.events.send(Observed::Closed(id)).await.unwrap();
    }
}

#[test]
fn actions_survive_rate_limit_and_popup_expiry_and_dedup_unchanged_posts() {
    let mut child = Command::new("dbus-daemon")
        .args(["--session", "--nofork", "--print-address=1"])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut address = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut address)
        .unwrap();
    let _bus = Bus(child);
    future::block_on(async {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("notification.sock");
        let listener = Async::new(UnixListener::bind(&path).unwrap()).unwrap();
        let (tx, rx) = async_channel::bounded(128);
        let reject = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let server = zbus::connection::Builder::address(address.trim())
            .unwrap()
            .name(NAME)
            .unwrap()
            .serve_at(
                PATH,
                RateLimitedServer {
                    next: Arc::new(AtomicU32::new(300)),
                    reject: reject.clone(),
                    events: tx,
                },
            )
            .unwrap()
            .build()
            .await
            .unwrap();
        let conn = zbus::connection::Builder::address(address.trim())
            .unwrap()
            .build()
            .await
            .unwrap();
        let state = dir.path().join("state");
        let grants = HostGrants::default();
        let bridge = serve(&listener, &state, conn, unsafe { libc::getuid() }, &grants);
        let checks = async {
            let socket = Async::<UnixStream>::connect(&path).await.unwrap();
            frame(&socket, serde_json::json!({"type":"hello","abi":3})).await;
            assert_eq!(
                reply(&socket).await,
                serde_json::json!({"type":"hello","abi":3})
            );
            let mut media = note("v1");
            media.alert = false;
            media.ongoing = true;
            media.clearable = false;
            snapshot(&socket, &[media.clone()]).await;
            assert!(matches!(
                timeout(rx.recv()).await.unwrap(),
                Observed::Posted(300, 0, ..)
            ));
            // Identical non-alerting update skips redundant Notify while updating version.
            media.version = "v2".into();
            media.when += 1000;
            frame(
                &socket,
                serde_json::json!({"type":"post","notification":media}),
            )
            .await;
            quiet(&rx).await;
            // Rate-limited update must preserve desktop ID 300 so buttons keep working.
            reject.store(true, Ordering::SeqCst);
            media.version = "v3".into();
            media.title = "Next Track".into();
            frame(
                &socket,
                serde_json::json!({"type":"post","notification":media}),
            )
            .await;
            quiet(&rx).await;
            server
                .emit_signal(
                    None::<&str>,
                    PATH,
                    NAME,
                    "ActionInvoked",
                    &(300u32, "action-0"),
                )
                .await
                .unwrap();
            let action = reply(&socket).await;
            assert_eq!(action["type"], "action");
            assert_eq!(action["version"], "v3");
            assert_eq!(action["action"], "action-0");
            // Popup expiry (reason=1) keeps last_id so history actions still work.
            server
                .emit_signal(
                    None::<&str>,
                    PATH,
                    NAME,
                    "NotificationClosed",
                    &(300u32, 1u32),
                )
                .await
                .unwrap();
            server
                .emit_signal(
                    None::<&str>,
                    PATH,
                    NAME,
                    "ActionInvoked",
                    &(300u32, "action-0"),
                )
                .await
                .unwrap();
            let history_action = reply(&socket).await;
            assert_eq!(history_action["type"], "action");
            assert_eq!(history_action["version"], "v3");
            assert_eq!(history_action["action"], "action-0");
        };
        future::race(
            async {
                bridge.await.unwrap();
                panic!("bridge stopped")
            },
            checks,
        )
        .await;
    });
}

#[test]
fn panel_dbus_interface_snapshot_actions_and_forwarding_toggle() {
    let mut child = Command::new("dbus-daemon")
        .args(["--session", "--nofork", "--print-address=1"])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut address = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut address)
        .unwrap();
    let _bus = Bus(child);
    future::block_on(async {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("notification.sock");
        let listener = Async::new(UnixListener::bind(&path).unwrap()).unwrap();
        let (tx, rx) = async_channel::bounded(128);
        let server = zbus::connection::Builder::address(address.trim())
            .unwrap()
            .name(NAME)
            .unwrap()
            .serve_at(
                PATH,
                Server {
                    next: Arc::new(AtomicU32::new(500)),
                    events: tx.clone(),
                },
            )
            .unwrap()
            .build()
            .await
            .unwrap();
        let _keep_server = &server;
        let conn = zbus::connection::Builder::address(address.trim())
            .unwrap()
            .build()
            .await
            .unwrap();
        let client_conn = zbus::connection::Builder::address(address.trim())
            .unwrap()
            .build()
            .await
            .unwrap();
        let state = dir.path().join("state");
        let grants = HostGrants::default();
        let bridge = serve(&listener, &state, conn, unsafe { libc::getuid() }, &grants);
        let checks = async {
            let panel = Proxy::new(&client_conn, PANEL_NAME, PANEL_PATH, PANEL_NAME)
                .await
                .unwrap();
            let socket = Async::<UnixStream>::connect(&path).await.unwrap();
            frame(&socket, serde_json::json!({"type":"hello","abi":3})).await;
            assert_eq!(
                reply(&socket).await,
                serde_json::json!({"type":"hello","abi":3})
            );

            let mut ongoing = note("v1");
            ongoing.key = "0|music.app|1".into();
            ongoing.package = "music.app".into();
            ongoing.ongoing = true;
            ongoing.clearable = false;
            ongoing.when = 1_700_000_000_100;

            let mut normal = note("v2");
            normal.key = "0|chat.app|2".into();
            normal.package = "chat.app".into();
            normal.ongoing = false;
            normal.clearable = true;
            normal.when = 1_700_000_000_500;

            snapshot(&socket, &[normal.clone(), ongoing.clone()]).await;
            let _ = timeout(rx.recv()).await.unwrap();
            let _ = timeout(rx.recv()).await.unwrap();

            let raw: String = panel.call("GetSnapshot", &()).await.unwrap();
            let parsed: serde_json::Value = serde_json::from_str(&raw).unwrap();
            assert_eq!(parsed["connected"], true);
            assert_eq!(parsed["desktop_forwarding"], true);
            let list = parsed["notifications"].as_array().unwrap();
            assert_eq!(list.len(), 2);
            // Ongoing notifications come before normal ones even with older timestamp.
            assert_eq!(list[0]["key"], "0|music.app|1");
            assert_eq!(list[1]["key"], "0|chat.app|2");

            // InvokeAction forwards to Android socket.
            let invoked: bool = panel
                .call("InvokeAction", &("0|chat.app|2", "action-0"))
                .await
                .unwrap();
            assert!(invoked);
            let act = reply(&socket).await;
            assert_eq!(act["type"], "action");
            assert_eq!(act["action"], "action-0");

            // DismissAll only cancels clearable notification, sparing ongoing one.
            let dismissed_count: u32 = panel.call("DismissAll", &()).await.unwrap();
            assert_eq!(dismissed_count, 1);
            let dismiss_msg = reply(&socket).await;
            assert_eq!(dismiss_msg["type"], "dismiss");
            assert_eq!(dismiss_msg["key"], "0|chat.app|2");
            match timeout(rx.recv()).await.unwrap() {
                Observed::Closed(_) => {}
                other @ Observed::Posted(..) => panic!("expected desktop close, got {other:?}"),
            }
            let after_dismiss: serde_json::Value =
                serde_json::from_str(&panel.call::<_, _, String>("GetSnapshot", &()).await.unwrap())
                    .unwrap();
            let after_list = after_dismiss["notifications"].as_array().unwrap();
            assert_eq!(after_list.len(), 1);
            assert_eq!(after_list[0]["key"], "0|music.app|1");

            // Disabling desktop forwarding suppresses new desktop Notify calls.
            let fwd: bool = panel.call("SetDesktopForwarding", &(false)).await.unwrap();
            assert!(!fwd);
            match timeout(rx.recv()).await.unwrap() {
                Observed::Closed(_) => {}
                other @ Observed::Posted(..) => {
                    panic!("expected ongoing desktop close, got {other:?}")
                }
            }

            let mut third = note("v3");
            third.key = "0|mail.app|3".into();
            third.package = "mail.app".into();
            frame(
                &socket,
                serde_json::json!({"type":"post","notification":third}),
            )
            .await;
            quiet(&rx).await;

            let raw2: String = panel.call("GetSnapshot", &()).await.unwrap();
            let parsed2: serde_json::Value = serde_json::from_str(&raw2).unwrap();
            assert_eq!(parsed2["desktop_forwarding"], false);
            assert!(
                parsed2["notifications"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|n| n["key"] == "0|mail.app|3")
            );
        };
        future::race(
            async {
                bridge.await.unwrap();
                panic!("bridge stopped")
            },
            checks,
        )
        .await;
    });
}

#[test]
fn rate_limited_update_retries_and_replaces_with_latest_artwork() {
    let mut child = Command::new("dbus-daemon")
        .args(["--session", "--nofork", "--print-address=1"])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut address = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut address)
        .unwrap();
    let _bus = Bus(child);
    future::block_on(async {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("notification.sock");
        let listener = Async::new(UnixListener::bind(&path).unwrap()).unwrap();
        let (tx, rx) = async_channel::bounded(128);
        let reject = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let server = zbus::connection::Builder::address(address.trim())
            .unwrap()
            .name(NAME)
            .unwrap()
            .serve_at(
                PATH,
                RateLimitedServer {
                    next: Arc::new(AtomicU32::new(300)),
                    reject: reject.clone(),
                    events: tx,
                },
            )
            .unwrap()
            .build()
            .await
            .unwrap();
        let conn = zbus::connection::Builder::address(address.trim())
            .unwrap()
            .build()
            .await
            .unwrap();
        let state = dir.path().join("state");
        let grants = HostGrants::default();
        let bridge = serve_with_retry_delay(
            &listener,
            &state,
            conn,
            unsafe { libc::getuid() },
            &grants,
            Duration::from_millis(60),
        );
        let checks = async {
            let socket = Async::<UnixStream>::connect(&path).await.unwrap();
            frame(&socket, serde_json::json!({"type":"hello","abi":3})).await;
            assert_eq!(
                reply(&socket).await,
                serde_json::json!({"type":"hello","abi":3})
            );

            let mut v1 = note("v1");
            v1.alert = false;
            v1.ongoing = true;
            v1.clearable = true;
            v1.large_icon = "11".repeat(96 * 96 * 4);
            snapshot(&socket, &[v1.clone()]).await;
            assert!(matches!(
                timeout(rx.recv()).await.unwrap(),
                Observed::Posted(300, 0, ..)
            ));

            // Rate-limit the artwork update.
            reject.store(true, Ordering::SeqCst);
            let mut v2 = v1.clone();
            v2.version = "v2".into();
            v2.large_icon = "22".repeat(96 * 96 * 4);
            frame(
                &socket,
                serde_json::json!({"type":"post","notification":v2}),
            )
            .await;
            assert!(
                future::race(
                    async {
                        let _ = rx.recv().await;
                        true
                    },
                    async {
                        async_io::Timer::after(Duration::from_millis(25)).await;
                        false
                    },
                )
                .await
                    == false
            );

            // An identical presentation bump while retry is pending must preserve the retry.
            let mut v3 = v2.clone();
            v3.version = "v3".into();
            v3.when += 500;
            frame(
                &socket,
                serde_json::json!({"type":"post","notification":v3}),
            )
            .await;

            // Unblock desktop Notify and verify the retry replaces ID 300 with v2/v3 artwork.
            reject.store(false, Ordering::SeqCst);
            match timeout(rx.recv()).await.unwrap() {
                Observed::Posted(id, replaces, _, _, hints, _) => {
                    assert_eq!(id, 300);
                    assert_eq!(replaces, 300);
                    let (_, _, _, _, _, _, pixels) =
                        <(i32, i32, i32, bool, i32, i32, Vec<u8>)>::try_from(
                            hints.get("image-data").unwrap().clone(),
                        )
                        .unwrap();
                    assert_eq!(pixels, hex::decode(&v2.large_icon).unwrap());
                }
                other @ Observed::Closed(_) => panic!("expected retry Post, got {other:?}"),
            }

            // Closing the notification cancels any pending retry so it does not resurface.
            reject.store(true, Ordering::SeqCst);
            let mut v4 = v3.clone();
            v4.version = "v4".into();
            v4.large_icon = "33".repeat(96 * 96 * 4);
            frame(
                &socket,
                serde_json::json!({"type":"post","notification":v4}),
            )
            .await;
            server
                .emit_signal(
                    None::<&str>,
                    PATH,
                    NAME,
                    "NotificationClosed",
                    &(300u32, 2u32),
                )
                .await
                .unwrap();
            let _ = reply(&socket).await;
            reject.store(false, Ordering::SeqCst);
            quiet(&rx).await;
        };
        future::race(
            async {
                bridge.await.unwrap();
                panic!("bridge stopped")
            },
            checks,
        )
        .await;
    });
}

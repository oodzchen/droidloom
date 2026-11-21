//! Exercise `xdg_toplevel_icon_v1` client calls against an in-process Wayland server.
#![allow(dead_code, clippy::all, missing_docs)]

use std::io::Write;
use std::os::fd::AsFd;
use std::os::unix::net::UnixStream;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;

use wayland_client::globals::{GlobalListContents, registry_queue_init};
use wayland_client::protocol::{wl_compositor, wl_registry, wl_shm, wl_surface};
use wayland_client::{Connection, Dispatch as ClientDispatch, QueueHandle};
use wayland_protocols::xdg::shell::client::{xdg_surface, xdg_toplevel, xdg_wm_base};
use wayland_protocols::xdg::shell::server::{
    xdg_surface as s_xdg_surface, xdg_toplevel as s_xdg_toplevel, xdg_wm_base as s_xdg_wm_base,
};
use wayland_protocols::xdg::toplevel_icon::v1::client::{
    xdg_toplevel_icon_manager_v1::{self, XdgToplevelIconManagerV1},
    xdg_toplevel_icon_v1::XdgToplevelIconV1,
};
use wayland_protocols::xdg::toplevel_icon::v1::server::{
    xdg_toplevel_icon_manager_v1 as s_icon_mgr, xdg_toplevel_icon_v1 as s_icon,
};
use wayland_server::protocol::{
    wl_buffer as s_wl_buffer, wl_compositor as s_wl_compositor, wl_shm as s_wl_shm,
    wl_shm_pool as s_wl_shm_pool, wl_surface as s_wl_surface,
};
use wayland_server::{
    Client, DataInit, Dispatch, Display, DisplayHandle, GlobalDispatch, New,
};

struct TestClient;

impl ClientDispatch<wl_registry::WlRegistry, GlobalListContents> for TestClient {
    fn event(
        _: &mut Self,
        _: &wl_registry::WlRegistry,
        _: wl_registry::Event,
        _: &GlobalListContents,
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

wayland_client::delegate_noop!(TestClient: ignore wl_compositor::WlCompositor);
wayland_client::delegate_noop!(TestClient: ignore wl_surface::WlSurface);
wayland_client::delegate_noop!(TestClient: ignore wl_shm::WlShm);
wayland_client::delegate_noop!(TestClient: ignore wayland_client::protocol::wl_shm_pool::WlShmPool);
wayland_client::delegate_noop!(TestClient: ignore wayland_client::protocol::wl_buffer::WlBuffer);
wayland_client::delegate_noop!(TestClient: ignore xdg_wm_base::XdgWmBase);
wayland_client::delegate_noop!(TestClient: ignore xdg_surface::XdgSurface);
wayland_client::delegate_noop!(TestClient: ignore xdg_toplevel::XdgToplevel);
wayland_client::delegate_noop!(TestClient: ignore XdgToplevelIconV1);

impl ClientDispatch<XdgToplevelIconManagerV1, ()> for TestClient {
    fn event(
        _: &mut Self,
        _: &XdgToplevelIconManagerV1,
        _: xdg_toplevel_icon_manager_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

#[derive(Default)]
struct ServerLog {
    events: Vec<String>,
    buffers: Vec<(i32, i32, i32, u32)>,
}

struct Server {
    log: Arc<Mutex<ServerLog>>,
}

struct Peer;
impl wayland_server::backend::ClientData for Peer {}

impl GlobalDispatch<s_wl_compositor::WlCompositor, ()> for Server {
    fn bind(
        _: &mut Self,
        _: &DisplayHandle,
        _: &Client,
        new: New<s_wl_compositor::WlCompositor>,
        _: &(),
        init: &mut DataInit<'_, Self>,
    ) {
        init.init(new, ());
    }
}

impl Dispatch<s_wl_compositor::WlCompositor, ()> for Server {
    fn request(
        _: &mut Self,
        _: &Client,
        _: &s_wl_compositor::WlCompositor,
        request: s_wl_compositor::Request,
        _: &(),
        _: &DisplayHandle,
        init: &mut DataInit<'_, Self>,
    ) {
        if let s_wl_compositor::Request::CreateSurface { id } = request {
            init.init::<s_wl_surface::WlSurface, _>(id, ());
        }
    }
}

impl Dispatch<s_wl_surface::WlSurface, ()> for Server {
    fn request(
        _: &mut Self,
        _: &Client,
        _: &s_wl_surface::WlSurface,
        _: s_wl_surface::Request,
        _: &(),
        _: &DisplayHandle,
        _: &mut DataInit<'_, Self>,
    ) {
    }
}

impl GlobalDispatch<s_wl_shm::WlShm, ()> for Server {
    fn bind(
        _: &mut Self,
        _: &DisplayHandle,
        _: &Client,
        new: New<s_wl_shm::WlShm>,
        _: &(),
        init: &mut DataInit<'_, Self>,
    ) {
        let shm = init.init(new, ());
        shm.format(s_wl_shm::Format::Argb8888);
    }
}

impl Dispatch<s_wl_shm::WlShm, ()> for Server {
    fn request(
        _: &mut Self,
        _: &Client,
        _: &s_wl_shm::WlShm,
        request: s_wl_shm::Request,
        _: &(),
        _: &DisplayHandle,
        init: &mut DataInit<'_, Self>,
    ) {
        if let s_wl_shm::Request::CreatePool { id, .. } = request {
            init.init::<s_wl_shm_pool::WlShmPool, _>(id, ());
        }
    }
}

impl Dispatch<s_wl_shm_pool::WlShmPool, ()> for Server {
    fn request(
        state: &mut Self,
        _: &Client,
        _: &s_wl_shm_pool::WlShmPool,
        request: s_wl_shm_pool::Request,
        _: &(),
        _: &DisplayHandle,
        init: &mut DataInit<'_, Self>,
    ) {
        if let s_wl_shm_pool::Request::CreateBuffer {
            id,
            width,
            height,
            stride,
            format,
            ..
        } = request
        {
            state
                .log
                .lock()
                .unwrap()
                .buffers
                .push((width, height, stride, u32::from(format)));
            init.init::<s_wl_buffer::WlBuffer, _>(id, ());
        }
    }
}

impl Dispatch<s_wl_buffer::WlBuffer, ()> for Server {
    fn request(
        state: &mut Self,
        _: &Client,
        _: &s_wl_buffer::WlBuffer,
        request: s_wl_buffer::Request,
        _: &(),
        _: &DisplayHandle,
        _: &mut DataInit<'_, Self>,
    ) {
        if matches!(request, s_wl_buffer::Request::Destroy) {
            state.log.lock().unwrap().events.push("buffer.destroy".into());
        }
    }
}

impl GlobalDispatch<s_xdg_wm_base::XdgWmBase, ()> for Server {
    fn bind(
        _: &mut Self,
        _: &DisplayHandle,
        _: &Client,
        new: New<s_xdg_wm_base::XdgWmBase>,
        _: &(),
        init: &mut DataInit<'_, Self>,
    ) {
        init.init(new, ());
    }
}

impl Dispatch<s_xdg_wm_base::XdgWmBase, ()> for Server {
    fn request(
        _: &mut Self,
        _: &Client,
        _: &s_xdg_wm_base::XdgWmBase,
        request: s_xdg_wm_base::Request,
        _: &(),
        _: &DisplayHandle,
        init: &mut DataInit<'_, Self>,
    ) {
        if let s_xdg_wm_base::Request::GetXdgSurface { id, .. } = request {
            init.init::<s_xdg_surface::XdgSurface, _>(id, ());
        }
    }
}

impl Dispatch<s_xdg_surface::XdgSurface, ()> for Server {
    fn request(
        _: &mut Self,
        _: &Client,
        _: &s_xdg_surface::XdgSurface,
        request: s_xdg_surface::Request,
        _: &(),
        _: &DisplayHandle,
        init: &mut DataInit<'_, Self>,
    ) {
        if let s_xdg_surface::Request::GetToplevel { id } = request {
            init.init::<s_xdg_toplevel::XdgToplevel, _>(id, ());
        }
    }
}

impl Dispatch<s_xdg_toplevel::XdgToplevel, ()> for Server {
    fn request(
        state: &mut Self,
        _: &Client,
        _: &s_xdg_toplevel::XdgToplevel,
        request: s_xdg_toplevel::Request,
        _: &(),
        _: &DisplayHandle,
        _: &mut DataInit<'_, Self>,
    ) {
        if let s_xdg_toplevel::Request::SetAppId { app_id } = request {
            state
                .log
                .lock()
                .unwrap()
                .events
                .push(format!("toplevel.set_app_id:{app_id}"));
        }
    }
}

impl GlobalDispatch<s_icon_mgr::XdgToplevelIconManagerV1, ()> for Server {
    fn bind(
        _: &mut Self,
        _: &DisplayHandle,
        _: &Client,
        new: New<s_icon_mgr::XdgToplevelIconManagerV1>,
        _: &(),
        init: &mut DataInit<'_, Self>,
    ) {
        let mgr = init.init(new, ());
        mgr.icon_size(64);
        mgr.done();
    }
}

impl Dispatch<s_icon_mgr::XdgToplevelIconManagerV1, ()> for Server {
    fn request(
        state: &mut Self,
        _: &Client,
        _: &s_icon_mgr::XdgToplevelIconManagerV1,
        request: s_icon_mgr::Request,
        _: &(),
        _: &DisplayHandle,
        init: &mut DataInit<'_, Self>,
    ) {
        match request {
            s_icon_mgr::Request::CreateIcon { id } => {
                state.log.lock().unwrap().events.push("icon.create".into());
                init.init::<s_icon::XdgToplevelIconV1, _>(id, ());
            }
            s_icon_mgr::Request::SetIcon { icon, .. } => {
                state
                    .log
                    .lock()
                    .unwrap()
                    .events
                    .push(format!("icon.set:{}", icon.is_some()));
            }
            _ => {}
        }
    }
}

impl Dispatch<s_icon::XdgToplevelIconV1, ()> for Server {
    fn request(
        state: &mut Self,
        _: &Client,
        _: &s_icon::XdgToplevelIconV1,
        request: s_icon::Request,
        _: &(),
        _: &DisplayHandle,
        _: &mut DataInit<'_, Self>,
    ) {
        match request {
            s_icon::Request::SetName { icon_name } => {
                state
                    .log
                    .lock()
                    .unwrap()
                    .events
                    .push(format!("icon.set_name:{icon_name}"));
            }
            s_icon::Request::AddBuffer { scale, .. } => {
                state
                    .log
                    .lock()
                    .unwrap()
                    .events
                    .push(format!("icon.add_buffer:{scale}"));
            }
            s_icon::Request::Destroy => {
                state.log.lock().unwrap().events.push("icon.destroy".into());
            }
            _ => {}
        }
    }
}

struct Stop(Arc<AtomicBool>);
impl Drop for Stop {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Release);
    }
}

#[test]
fn xdg_toplevel_icon_destroys_icon_before_shm_buffer_and_supports_name_fallback() {
    let mut display: Display<Server> = Display::new().unwrap();
    let mut dh = display.handle();
    dh.create_global::<Server, s_wl_compositor::WlCompositor, _>(1, ());
    dh.create_global::<Server, s_wl_shm::WlShm, _>(1, ());
    dh.create_global::<Server, s_xdg_wm_base::XdgWmBase, _>(1, ());
    dh.create_global::<Server, s_icon_mgr::XdgToplevelIconManagerV1, _>(1, ());

    let log = Arc::new(Mutex::new(ServerLog::default()));
    let mut server = Server { log: log.clone() };
    let (s_sock, c_sock) = UnixStream::pair().unwrap();
    dh.insert_client(s_sock, Arc::new(Peer)).unwrap();

    let stop = Stop(Arc::new(AtomicBool::new(false)));
    let stopping = stop.0.clone();
    let server_thread = std::thread::spawn(move || {
        while !stopping.load(Ordering::Acquire) {
            display.dispatch_clients(&mut server).unwrap();
            display.flush_clients().unwrap();
            std::thread::sleep(Duration::from_millis(1));
        }
    });

    let conn = Connection::from_socket(c_sock).unwrap();
    let (globals, mut queue) = registry_queue_init::<TestClient>(&conn).unwrap();
    let qh = queue.handle();
    let mut client = TestClient;

    let compositor: wl_compositor::WlCompositor = globals.bind(&qh, 1..=1, ()).unwrap();
    let shm: wl_shm::WlShm = globals.bind(&qh, 1..=1, ()).unwrap();
    let wm_base: xdg_wm_base::XdgWmBase = globals.bind(&qh, 1..=1, ()).unwrap();
    let icon_mgr: XdgToplevelIconManagerV1 = globals.bind(&qh, 1..=1, ()).unwrap();

    let surface = compositor.create_surface(&qh, ());
    let xdg_surface = wm_base.get_xdg_surface(&surface, &qh, ());
    let toplevel = xdg_surface.get_toplevel(&qh, ());

    let mut tmp = tempfile::tempfile().unwrap();
    tmp.write_all(&[0xffu8; 2 * 2 * 4]).unwrap();
    let pool = shm.create_pool(tmp.as_fd(), 16, &qh, ());
    let buffer = pool.create_buffer(0, 2, 2, 8, wl_shm::Format::Argb8888, &qh, ());
    pool.destroy();

    let icon = icon_mgr.create_icon(&qh, ());
    icon.add_buffer(&buffer, 1);
    icon_mgr.set_icon(&toplevel, Some(&icon));
    toplevel.set_app_id("droidloom-test".into());
    surface.commit();

    icon.destroy();
    buffer.destroy();

    let fallback = icon_mgr.create_icon(&qh, ());
    fallback.set_name("application-x-executable".into());
    icon_mgr.set_icon(&toplevel, Some(&fallback));
    surface.commit();
    fallback.destroy();

    queue.roundtrip(&mut client).unwrap();
    drop(stop);
    server_thread.join().unwrap();

    let recorded = log.lock().unwrap();
    assert_eq!(recorded.buffers, vec![(2, 2, 8, 0)]);
    assert_eq!(
        recorded.events,
        vec![
            "icon.create",
            "icon.add_buffer:1",
            "icon.set:true",
            "toplevel.set_app_id:droidloom-test",
            "icon.destroy",
            "buffer.destroy",
            "icon.create",
            "icon.set_name:application-x-executable",
            "icon.set:true",
            "icon.destroy",
        ]
    );
}

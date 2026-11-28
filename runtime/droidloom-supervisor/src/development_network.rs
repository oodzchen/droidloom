//! Bounded network plumbing for the executable development cell.
//!
//! Linux remains the network implementation. This module only creates a veth
//! pair, assigns the host gateway, answers DHCP on it, and installs narrowly
//! named nftables rules for outbound forwarding/NAT. Android sees its end as
//! `wlan0`, the link Droidloom's wificond and supplicant report as one Wi-Fi
//! access point, and runs its normal networking services on it inside the
//! private namespace. Each side is one `ip -batch` and the rules are one `nft`
//! transaction, so a cell start spawns three helpers.

use std::fmt::Write as _;
use std::fs;
use std::io::Write;
use std::net::Ipv4Addr;
use std::path::{Path, PathBuf};
use std::process::Stdio;

use crate::development::DevelopmentError;
use crate::dhcp_responder::{DhcpResponder, Lease};
use crate::{CellSpec, HostAccessPort, NetworkMode, PortForward};

const DEFAULT_HOST_IPV4: Ipv4Addr = Ipv4Addr::new(10, 177, 0, 1);
const DEFAULT_CELL_IPV4: Ipv4Addr = Ipv4Addr::new(10, 177, 0, 2);
const DEFAULT_PREFIX_LEN: u8 = 30;
const CELL_INTERFACE: &str = "wlan0";
// Locally administered, so it never names real hardware. Android keeps one
// address for its Wi-Fi interface across cell starts.
const DEFAULT_CELL_MAC: &str = "02:64:6c:00:00:02";
const DEFAULT_DNS: [Ipv4Addr; 2] = [Ipv4Addr::new(1, 1, 1, 1), Ipv4Addr::new(8, 8, 8, 8)];
const IPV4_FORWARD_PATH: &str = "/proc/sys/net/ipv4/ip_forward";
const IPV4_FORWARD_STATE_FILE: &str = "host-ipv4-forward.before";
const NETNS_DIR: &str = "/run/netns";
const NET_CLASS_DIR: &str = "/sys/class/net";

#[derive(Clone, Debug, Eq, PartialEq)]
struct EffectiveNetwork {
    mode: NetworkMode,
    host_ipv4: Ipv4Addr,
    cell_ipv4: Ipv4Addr,
    prefix_len: u8,
    cell_mac: String,
    dns: [Option<Ipv4Addr>; 4],
    allow_host_ports: Vec<HostAccessPort>,
    port_forwards: Vec<PortForward>,
    bridge_interface: Option<String>,
}

impl EffectiveNetwork {
    fn from_spec(spec: &CellSpec) -> Self {
        let cfg = spec.network.as_ref();
        let host_ipv4 = cfg.and_then(|c| c.host_ipv4).unwrap_or(DEFAULT_HOST_IPV4);
        let cell_ipv4 = cfg.and_then(|c| c.cell_ipv4).unwrap_or(DEFAULT_CELL_IPV4);
        let prefix_len = cfg.and_then(|c| c.prefix_len).unwrap_or(DEFAULT_PREFIX_LEN);
        let cell_mac = cfg
            .and_then(|c| c.cell_mac.as_deref())
            .unwrap_or(DEFAULT_CELL_MAC)
            .to_owned();
        let mut dns = [None; 4];
        match cfg.map(|c| c.dns.as_slice()) {
            Some(list) if !list.is_empty() => {
                for (slot, addr) in dns.iter_mut().zip(list.iter().copied()) {
                    *slot = Some(addr);
                }
            }
            _ => {
                dns[0] = Some(DEFAULT_DNS[0]);
                dns[1] = Some(DEFAULT_DNS[1]);
            }
        }
        Self {
            mode: cfg.map_or(NetworkMode::Nat, |c| c.mode),
            host_ipv4,
            cell_ipv4,
            prefix_len,
            cell_mac,
            dns,
            allow_host_ports: cfg
                .map(|c| c.allow_host_ports.clone())
                .unwrap_or_default(),
            port_forwards: cfg.map(|c| c.port_forwards.clone()).unwrap_or_default(),
            bridge_interface: cfg.and_then(|c| c.bridge_interface.clone()),
        }
    }

    fn host_cidr(&self) -> String {
        format!("{}/{}", self.host_ipv4, self.prefix_len)
    }

    fn cell_cidr(&self) -> String {
        format!("{}/32", self.cell_ipv4)
    }

    fn lease(&self) -> Lease {
        Lease {
            server: self.host_ipv4,
            client: self.cell_ipv4,
            prefix_len: self.prefix_len,
            dns: self.dns,
        }
    }

    fn needs_nat_table(&self) -> bool {
        self.mode == NetworkMode::Nat || !self.port_forwards.is_empty()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct NetworkNames {
    namespace: String,
    host_interface: String,
    filter_table: String,
    nat_table: String,
}

impl NetworkNames {
    fn for_spec(spec: &CellSpec) -> Self {
        let uid = spec.host_uid;
        Self {
            namespace: format!("droidloom-u{uid}"),
            host_interface: format!("dlh{uid}"),
            filter_table: format!("droidloom_u{uid}"),
            nat_table: format!("droidloom_u{uid}_nat"),
        }
    }
}

/// Live host-owned networking objects for one development cell.
pub(crate) struct DevelopmentNetwork {
    names: NetworkNames,
    effective: EffectiveNetwork,
    runtime_dir: PathBuf,
    resources: Vec<NetworkResource>,
    previous_ipv4_forward: Option<String>,
    dhcp: Option<DhcpResponder>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum NetworkResource {
    Namespace,
    HostInterface,
    FilterTable,
    NatTable,
    UfwForwardRule,
}

impl DevelopmentNetwork {
    /// Create the private namespace and its single outbound kernel path.
    pub(crate) fn create(spec: &CellSpec) -> Result<Self, DevelopmentError> {
        let mut network = Self {
            names: NetworkNames::for_spec(spec),
            effective: EffectiveNetwork::from_spec(spec),
            runtime_dir: spec.runtime_dir.clone(),
            resources: Vec::new(),
            previous_ipv4_forward: None,
            dhcp: None,
        };

        if let Err(error) = network.setup() {
            if let Err(cleanup_error) = network.teardown() {
                eprintln!(
                    "droidloom-supervisor: network setup failed: {error}; cleanup also failed: {cleanup_error}"
                );
            }
            return Err(error);
        }
        Ok(network)
    }

    pub(crate) fn namespace(&self) -> Option<&str> {
        (self.effective.mode != NetworkMode::Host).then_some(self.names.namespace.as_str())
    }

    fn setup(&mut self) -> Result<(), DevelopmentError> {
        match self.effective.mode {
            NetworkMode::Host => self.setup_host_anchor_link(),
            NetworkMode::Bridge => self.setup_bridge_links(),
            NetworkMode::Nat | NetworkMode::Offline => {
                self.setup_links()?;
                let lease = self.effective.lease();
                self.dhcp = Some(
                    DhcpResponder::start(&self.names.host_interface, lease)
                        .map_err(|source| io_error("answer DHCP on the cell link", source))?,
                );
                if self.effective.needs_nat_table() {
                    self.enable_forwarding()?;
                }
                self.setup_rules()?;
                if self.effective.mode == NetworkMode::Nat {
                    self.setup_ufw_forwarding()?;
                }
                Ok(())
            }
        }
    }

    fn setup_host_anchor_link(&mut self) -> Result<(), DevelopmentError> {
        if link_exists(CELL_INTERFACE) {
            return Err(DevelopmentError::InvalidInput(format!(
                "host network mode requires interface name {CELL_INTERFACE} to be free on the host"
            )));
        }
        let host_script = render_host_anchor_link_script(&self.names, &self.effective);
        let result = run_script("ip", ["-batch", "-"], &host_script);
        if link_exists(&self.names.host_interface) {
            self.resources.push(NetworkResource::HostInterface);
        }
        result?;

        for iface in [CELL_INTERFACE, self.names.host_interface.as_str()] {
            for (family, control, value) in [
                ("ipv4", "accept_local", "1\n"),
                ("ipv4", "arp_accept", "1\n"),
                ("ipv6", "accept_ra", "0\n"),
            ] {
                let path = PathBuf::from(format!("/proc/sys/net/{family}/conf/{iface}/{control}"));
                if path.exists() {
                    fs::write(&path, value).map_err(|source| {
                        io_error(&format!("configure host anchor sysctl {}", path.display()), source)
                    })?;
                }
            }
        }

        let lease = self.effective.lease();
        self.dhcp = Some(
            DhcpResponder::start(&self.names.host_interface, lease)
                .map_err(|source| io_error("answer DHCP on the host anchor link", source))?,
        );
        Ok(())
    }

    fn setup_bridge_links(&mut self) -> Result<(), DevelopmentError> {
        let Some(bridge) = self.effective.bridge_interface.as_deref() else {
            return Err(DevelopmentError::InvalidInput(
                "bridge network mode requires bridge_interface".into(),
            ));
        };
        if !link_exists(bridge) {
            return Err(DevelopmentError::InvalidInput(format!(
                "host bridge interface {bridge} does not exist"
            )));
        }
        let host_script = render_bridge_host_link_script(&self.names, bridge);
        let host = run_script("ip", ["-batch", "-"], &host_script);
        if namespace_exists(&self.names.namespace) {
            self.resources.push(NetworkResource::Namespace);
        }
        if link_exists(&self.names.host_interface) {
            self.resources.push(NetworkResource::HostInterface);
        }
        host?;
        let cell_mac = &self.effective.cell_mac;
        run_script(
            "ip",
            ["-n", &self.names.namespace, "-batch", "-"],
            &format!(
                "link set lo up\n\
                 link set {CELL_INTERFACE} address {cell_mac}\n\
                 link set {CELL_INTERFACE} up\n"
            ),
        )
    }

    fn setup_ufw_forwarding(&mut self) -> Result<(), DevelopmentError> {
        // A separate nftables accept verdict cannot override UFW's later drop.
        // Add only our exact outbound source/interface rule to its user chain.
        // It is transient and never modifies UFW's persistent configuration.
        if ufw_chain_exists()? {
            let cell_cidr = self.effective.cell_cidr();
            run("iptables", ufw_rule_arguments("-I", &self.names, &cell_cidr))?;
            self.resources.push(NetworkResource::UfwForwardRule);
        }
        Ok(())
    }

    fn setup_links(&mut self) -> Result<(), DevelopmentError> {
        let NetworkNames {
            namespace,
            host_interface,
            ..
        } = &self.names;
        let host_cidr = self.effective.host_cidr();
        let cell_mac = &self.effective.cell_mac;
        // The peer is created inside the namespace as wlan0, so it never
        // appears on the host under a temporary name.
        let host = run_script(
            "ip",
            ["-batch", "-"],
            &format!(
                "netns add {namespace}\n\
                 link add {host_interface} type veth peer name {CELL_INTERFACE} netns {namespace}\n\
                 address add {host_cidr} dev {host_interface}\n\
                 link set {host_interface} up\n"
            ),
        );
        // A batch stops at its first failing line; own whatever it created.
        if namespace_exists(namespace) {
            self.resources.push(NetworkResource::Namespace);
        }
        if link_exists(host_interface) {
            self.resources.push(NetworkResource::HostInterface);
        }
        host?;
        run_script(
            "ip",
            ["-n", namespace, "-batch", "-"],
            &format!(
                "link set lo up\n\
                 link set {CELL_INTERFACE} address {cell_mac}\n\
                 link set {CELL_INTERFACE} up\n"
            ),
        )
    }

    fn enable_forwarding(&mut self) -> Result<(), DevelopmentError> {
        let forwarding = fs::read_to_string(IPV4_FORWARD_PATH)
            .map_err(|source| io_error("read host IPv4 forwarding state", source))?;
        fs::write(self.forwarding_state_path(), forwarding.as_bytes())
            .map_err(|source| io_error("record host IPv4 forwarding state", source))?;
        self.previous_ipv4_forward = Some(forwarding.clone());
        if forwarding.trim() != "1" {
            fs::write(IPV4_FORWARD_PATH, "1\n")
                .map_err(|source| io_error("enable host IPv4 forwarding", source))?;
        }
        Ok(())
    }

    /// Install both tables in one nftables transaction: all of it or nothing.
    fn setup_rules(&mut self) -> Result<(), DevelopmentError> {
        let script = render_nftables_rules(&self.names, &self.effective);
        run_script("nft", ["-f", "-"], &script)?;
        self.resources.push(NetworkResource::FilterTable);
        if self.effective.needs_nat_table() {
            self.resources.push(NetworkResource::NatTable);
        }
        Ok(())
    }

    /// Remove every object created by this instance in reverse ownership order.
    pub(crate) fn teardown(&mut self) -> Result<(), DevelopmentError> {
        let mut first_error = None;
        // Stop answering before the link goes away.
        self.dhcp = None;
        let cell_cidr = self.effective.cell_cidr();

        while let Some(resource) = self.resources.pop() {
            let result = match resource {
                NetworkResource::UfwForwardRule => remove_ufw_rule(&self.names, &cell_cidr),
                NetworkResource::Namespace => run("ip", ["netns", "delete", &self.names.namespace]),
                NetworkResource::HostInterface => {
                    run("ip", ["link", "delete", &self.names.host_interface])
                }
                NetworkResource::FilterTable => {
                    run("nft", ["delete", "table", "inet", &self.names.filter_table])
                }
                NetworkResource::NatTable => {
                    run("nft", ["delete", "table", "ip", &self.names.nat_table])
                }
            };
            record_error(&mut first_error, result);
        }
        if let Some(previous) = self.previous_ipv4_forward.take() {
            if previous.trim() != "1"
                && !has_other_forwarding_interfaces(
                    Path::new(NET_CLASS_DIR),
                    &self.names.host_interface,
                )
            {
                record_error(
                    &mut first_error,
                    fs::write(IPV4_FORWARD_PATH, previous)
                        .map_err(|source| io_error("restore host IPv4 forwarding", source)),
                );
            }
            record_error(
                &mut first_error,
                remove_file_if_present(
                    &self.forwarding_state_path(),
                    "remove host IPv4 forwarding state",
                ),
            );
        }

        first_error.map_or(Ok(()), Err)
    }

    /// Remove exact, deterministically named objects left by an interrupted
    /// daemon. This never enumerates or deletes unrelated host networking.
    pub(crate) fn recover(spec: &CellSpec) -> Result<(), DevelopmentError> {
        let names = NetworkNames::for_spec(spec);
        let cell_cidr = EffectiveNetwork::from_spec(spec).cell_cidr();
        let mut first_error = None;

        record_error(&mut first_error, remove_ufw_rule(&names, &cell_cidr));
        record_error(
            &mut first_error,
            run_script(
                "nft",
                ["-f", "-"],
                &format!(
                    "destroy table ip {}\ndestroy table inet {}\n",
                    names.nat_table, names.filter_table
                ),
            ),
        );
        if link_exists(&names.host_interface) {
            record_error(
                &mut first_error,
                run("ip", ["link", "delete", &names.host_interface]),
            );
        }
        if namespace_exists(&names.namespace) {
            record_error(
                &mut first_error,
                run("ip", ["netns", "delete", &names.namespace]),
            );
        }

        let forwarding_state = spec.runtime_dir.join(IPV4_FORWARD_STATE_FILE);
        match fs::read_to_string(&forwarding_state) {
            Ok(previous) => {
                if previous.trim() != "1"
                    && !has_other_forwarding_interfaces(
                        Path::new(NET_CLASS_DIR),
                        &names.host_interface,
                    )
                {
                    record_error(
                        &mut first_error,
                        fs::write(IPV4_FORWARD_PATH, previous).map_err(|source| {
                            io_error("restore stale host IPv4 forwarding state", source)
                        }),
                    );
                }
                record_error(
                    &mut first_error,
                    remove_file_if_present(
                        &forwarding_state,
                        "remove stale host IPv4 forwarding state",
                    ),
                );
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(source) => {
                record_error(
                    &mut first_error,
                    Err(io_error("read stale host IPv4 forwarding state", source)),
                );
            }
        }

        first_error.map_or(Ok(()), Err)
    }

    fn forwarding_state_path(&self) -> PathBuf {
        // `runtime_dir` has already been created mode 0700 before networking
        // starts, so this recovery record is not exposed to the Android cell.
        self.runtime_dir.join(IPV4_FORWARD_STATE_FILE)
    }
}

fn render_host_anchor_link_script(names: &NetworkNames, effective: &EffectiveNetwork) -> String {
    let host_interface = &names.host_interface;
    let host_cidr = effective.host_cidr();
    let cell_mac = &effective.cell_mac;
    format!(
        "link add {host_interface} type veth peer name {CELL_INTERFACE}\n\
         address add {host_cidr} dev {host_interface} noprefixroute\n\
         link set {host_interface} up\n\
         link set {CELL_INTERFACE} address {cell_mac}\n\
         link set {CELL_INTERFACE} up\n"
    )
}

fn render_bridge_host_link_script(names: &NetworkNames, bridge: &str) -> String {
    let NetworkNames {
        namespace,
        host_interface,
        ..
    } = names;
    format!(
        "netns add {namespace}\n\
         link add {host_interface} type veth peer name {CELL_INTERFACE} netns {namespace}\n\
         link set {host_interface} master {bridge}\n\
         link set {host_interface} up\n"
    )
}

fn render_nftables_rules(names: &NetworkNames, effective: &EffectiveNetwork) -> String {
    let host = &names.host_interface;
    let filter_table = &names.filter_table;
    let nat_table = &names.nat_table;
    let cell_cidr = effective.cell_cidr();
    let host_ip = effective.host_ipv4;
    let cell_ip = effective.cell_ipv4;

    let mut script = String::new();
    let _ = writeln!(script, "table inet {filter_table} {{");
    let _ = writeln!(script, "\tchain input {{");
    let _ = writeln!(script, "\t\ttype filter hook input priority filter; policy accept;");
    let _ = writeln!(
        script,
        "\t\tiifname \"{host}\" ip saddr {cell_cidr} tcp sport 5555 ct state established accept"
    );
    for port in &effective.allow_host_ports {
        let proto = port.protocol.as_nft_str();
        let _ = writeln!(
            script,
            "\t\tiifname \"{host}\" ip saddr {cell_cidr} ip daddr {host_ip}/32 {proto} dport {} accept",
            port.port
        );
    }
    let _ = writeln!(script, "\t\tiifname \"{host}\" drop");
    let _ = writeln!(script, "\t}}");

    let _ = writeln!(script, "\tchain forward {{");
    let _ = writeln!(script, "\t\ttype filter hook forward priority filter; policy accept;");
    let _ = writeln!(script, "\t\tiifname \"{host}\" ip saddr != {cell_cidr} drop");
    if effective.mode == NetworkMode::Nat {
        let _ = writeln!(script, "\t\tiifname \"{host}\" ip saddr {cell_cidr} accept");
    }
    let _ = writeln!(
        script,
        "\t\toifname \"{host}\" ip daddr {cell_cidr} ct state established,related accept"
    );
    for forward in &effective.port_forwards {
        let proto = forward.protocol.as_nft_str();
        let _ = writeln!(
            script,
            "\t\toifname \"{host}\" ip daddr {cell_cidr} {proto} dport {} ct state new accept",
            forward.cell_port
        );
    }
    let _ = writeln!(script, "\t\toifname \"{host}\" drop");
    if effective.mode == NetworkMode::Offline {
        let _ = writeln!(script, "\t\tiifname \"{host}\" drop");
    }
    let _ = writeln!(script, "\t}}");
    let _ = writeln!(script, "}}");

    if effective.needs_nat_table() {
        let _ = writeln!(script, "table ip {nat_table} {{");
        if !effective.port_forwards.is_empty() {
            for (chain, hook) in [("prerouting", "prerouting"), ("output", "output")] {
                let _ = writeln!(script, "\tchain {chain} {{");
                let _ = writeln!(script, "\t\ttype nat hook {hook} priority dstnat; policy accept;");
                for forward in &effective.port_forwards {
                    let bind = forward.bind_address.unwrap_or(Ipv4Addr::LOCALHOST);
                    let proto = forward.protocol.as_nft_str();
                    let _ = writeln!(
                        script,
                        "\t\tip daddr {bind} {proto} dport {} dnat to {cell_ip}:{}",
                        forward.host_port, forward.cell_port
                    );
                }
                let _ = writeln!(script, "\t}}");
            }
        }
        let _ = writeln!(script, "\tchain postrouting {{");
        let _ = writeln!(script, "\t\ttype nat hook postrouting priority srcnat; policy accept;");
        if effective.mode == NetworkMode::Nat {
            let _ = writeln!(script, "\t\tip saddr {cell_cidr} masquerade");
        }
        if !effective.port_forwards.is_empty() {
            let _ = writeln!(
                script,
                "\t\toifname \"{host}\" ip daddr {cell_cidr} masquerade"
            );
        }
        let _ = writeln!(script, "\t}}");
        let _ = writeln!(script, "}}");
    }
    script
}

fn ufw_rule_arguments<'a>(
    operation: &'a str,
    names: &'a NetworkNames,
    cell_cidr: &'a str,
) -> [&'a str; 13] {
    [
        "-w",
        operation,
        "ufw-user-forward",
        "-i",
        &names.host_interface,
        "-s",
        cell_cidr,
        "-m",
        "comment",
        "--comment",
        &names.namespace,
        "-j",
        "ACCEPT",
    ]
}

fn ufw_chain_exists() -> Result<bool, DevelopmentError> {
    match droidloom_cpu_placement::command("iptables")
        .args(["-w", "-S", "ufw-user-forward"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
    {
        Ok(status) => Ok(status.success()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(io_error("inspect UFW forwarding chain", error)),
    }
}

fn remove_ufw_rule(names: &NetworkNames, cell_cidr: &str) -> Result<(), DevelopmentError> {
    if ufw_chain_exists()?
        && command_succeeds("iptables", ufw_rule_arguments("-C", names, cell_cidr))?
    {
        run("iptables", ufw_rule_arguments("-D", names, cell_cidr))?;
    }
    Ok(())
}

fn command_succeeds<const N: usize>(
    program: &str,
    args: [&str; N],
) -> Result<bool, DevelopmentError> {
    droidloom_cpu_placement::command(program)
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .map_err(|source| io_error(&format!("execute {program}"), source))
}

fn namespace_exists(namespace: &str) -> bool {
    Path::new(NETNS_DIR).join(namespace).exists()
}

fn link_exists(interface: &str) -> bool {
    Path::new(NET_CLASS_DIR).join(interface).exists()
}

fn has_other_forwarding_interfaces(net_class_dir: &Path, own_interface: &str) -> bool {
    let Ok(entries) = fs::read_dir(net_class_dir) else {
        return false;
    };
    entries.filter_map(Result::ok).any(|entry| {
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            return false;
        };
        if name == own_interface {
            return false;
        }
        [
            "dlh",
            "docker",
            "podman",
            "cni-",
            "virbr",
            "wg",
            "tailscale",
            "br-",
        ]
        .iter()
        .any(|prefix| name.starts_with(prefix))
    })
}

fn remove_file_if_present(path: &Path, context: &str) -> Result<(), DevelopmentError> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(source) => Err(io_error(context, source)),
    }
}

fn run<const N: usize>(program: &str, args: [&str; N]) -> Result<(), DevelopmentError> {
    let status = droidloom_cpu_placement::command(program)
        .args(args)
        .status()
        .map_err(|source| io_error(&format!("execute {program}"), source))?;
    command_status(program, status)
}

/// Run `program` once with `script` on its standard input.
fn run_script<const N: usize>(
    program: &str,
    args: [&str; N],
    script: &str,
) -> Result<(), DevelopmentError> {
    let mut child = droidloom_cpu_placement::command(program)
        .args(args)
        .stdin(Stdio::piped())
        .spawn()
        .map_err(|source| io_error(&format!("execute {program}"), source))?;
    let written = child
        .stdin
        .take()
        .map_or(Ok(()), |mut stdin| stdin.write_all(script.as_bytes()));
    let status = child
        .wait()
        .map_err(|source| io_error(&format!("wait for {program}"), source))?;
    written.map_err(|source| io_error(&format!("write {program} script"), source))?;
    command_status(program, status)
}

fn command_status(program: &str, status: std::process::ExitStatus) -> Result<(), DevelopmentError> {
    if status.success() {
        Ok(())
    } else {
        Err(DevelopmentError::Command {
            program: program.into(),
            status,
        })
    }
}

fn record_error(slot: &mut Option<DevelopmentError>, result: Result<(), DevelopmentError>) {
    if let Err(error) = result
        && slot.is_none()
    {
        *slot = Some(error);
    }
}

fn io_error(context: &str, source: std::io::Error) -> DevelopmentError {
    DevelopmentError::Io {
        context: context.into(),
        source,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::IdRange;
    use std::path::PathBuf;

    fn spec(host_uid: u32) -> CellSpec {
        CellSpec {
            host_uid,
            subordinate_uids: IdRange {
                start: 100_000,
                count: 100_000,
            },
            subordinate_gids: IdRange {
                start: 200_000,
                count: 100_000,
            },
            image_dir: PathBuf::from("/images"),
            gapps_dir: None,
            vendor_image: PathBuf::from("/images/vendor.img"),
            android_init: None,
            android_file_overrides: Vec::new(),
            android_runtime_directories: Vec::new(),
            shared_storage_directories: Vec::new(),
            share_user_folders: false,
            data_dir: PathBuf::from("/data"),
            runtime_dir: PathBuf::from("/run/droidloom/cells/test"),
            gpu: Default::default(),
            render_node: PathBuf::from("/dev/dri/renderD128"),
            graphics_backend: crate::GraphicsBackend::default(),
            denial_socket: PathBuf::from("/run/user/1001/denial.sock"),
            network: None,
            device_identity: None,
        }
    }

    #[test]
    fn network_objects_are_stable_and_interface_names_fit_linux() {
        let names = NetworkNames::for_spec(&spec(1001));
        assert_eq!(names.namespace, "droidloom-u1001");
        assert_eq!(names.host_interface, "dlh1001");
        assert!(names.host_interface.len() < libc::IF_NAMESIZE);
    }

    #[test]
    fn cell_and_gateway_use_the_same_bounded_subnet() {
        let effective = EffectiveNetwork::from_spec(&spec(1000));
        let lease = effective.lease();
        assert_eq!(effective.host_cidr(), "10.177.0.1/30");
        assert_eq!(effective.cell_cidr(), "10.177.0.2/32");
        assert_eq!(format!("{}/{}", lease.server, lease.prefix_len), "10.177.0.1/30");
        assert_eq!(format!("{}/32", lease.client), "10.177.0.2/32");
        assert_eq!(lease.dns_servers(), vec![DEFAULT_DNS[0], DEFAULT_DNS[1]]);
    }

    #[test]
    fn nftables_rules_support_offline_host_proxy_and_port_forwards() {
        use crate::{NetworkConfig, NetworkProtocol};
        let mut cell = spec(1000);
        cell.network = Some(NetworkConfig {
            mode: NetworkMode::Offline,
            host_ipv4: Some(Ipv4Addr::new(10, 178, 0, 1)),
            cell_ipv4: Some(Ipv4Addr::new(10, 178, 0, 2)),
            prefix_len: Some(29),
            cell_mac: None,
            dns: vec![Ipv4Addr::new(9, 9, 9, 9)],
            allow_host_ports: vec![HostAccessPort {
                protocol: NetworkProtocol::Tcp,
                port: 7897,
            }],
            port_forwards: vec![PortForward {
                protocol: NetworkProtocol::Tcp,
                host_port: 15555,
                cell_port: 5555,
                bind_address: None,
            }],
            bridge_interface: None,
        });
        let names = NetworkNames::for_spec(&cell);
        let effective = EffectiveNetwork::from_spec(&cell);
        let rules = render_nftables_rules(&names, &effective);
        assert!(rules.contains("ip daddr 10.178.0.1/32 tcp dport 7897 accept"));
        assert!(rules.contains("iifname \"dlh1000\" drop\n\t}"));
        assert!(rules.contains("ip daddr 127.0.0.1 tcp dport 15555 dnat to 10.178.0.2:5555"));
        assert!(!rules.contains("ip saddr 10.178.0.2/32 accept"));
    }

    #[test]
    fn bridge_link_script_attaches_veth_to_host_bridge() {
        let names = NetworkNames::for_spec(&spec(1000));
        let script = render_bridge_host_link_script(&names, "br0");
        assert!(script.contains("netns add droidloom-u1000"));
        assert!(script.contains("link add dlh1000 type veth peer name wlan0 netns droidloom-u1000"));
        assert!(script.contains("link set dlh1000 master br0"));
        assert!(script.contains("link set dlh1000 up"));
    }

    #[test]
    fn host_network_mode_uses_host_netns_and_anchor_veth() {
        use crate::NetworkConfig;
        let mut cell = spec(1000);
        cell.network = Some(NetworkConfig {
            mode: NetworkMode::Host,
            ..NetworkConfig::default()
        });
        let names = NetworkNames::for_spec(&cell);
        let effective = EffectiveNetwork::from_spec(&cell);
        assert!(!effective.needs_nat_table());
        let script = render_host_anchor_link_script(&names, &effective);
        assert!(!script.contains("netns add"));
        assert!(script.contains("link add dlh1000 type veth peer name wlan0"));
        assert!(script.contains("address add 10.177.0.1/30 dev dlh1000 noprefixroute"));
        assert_eq!(
            crate::adb::endpoint_for(&cell).as_deref(),
            Some("127.0.0.1:5555")
        );
        assert!(
            crate::adb::init_policy_for(&cell)
                .contains("setprop service.adb.listen_addrs tcp:localhost:5555")
        );
    }

    #[test]
    fn forwarding_teardown_respects_other_forwarding_interfaces() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join("dlh1000")).unwrap();
        fs::create_dir(dir.path().join("wlo1")).unwrap();
        fs::create_dir(dir.path().join("lo")).unwrap();
        assert!(!has_other_forwarding_interfaces(dir.path(), "dlh1000"));

        for other in ["docker0", "virbr0", "wg0", "dlh1001"] {
            let path = dir.path().join(other);
            fs::create_dir(&path).unwrap();
            assert!(has_other_forwarding_interfaces(dir.path(), "dlh1000"));
            fs::remove_dir(&path).unwrap();
        }
    }
}

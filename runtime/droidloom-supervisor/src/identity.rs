//! Android names the device after its host.
//!
//! The model comes from the device tree, as the host's own About page shows
//! it. The maker is the model's first word when the device tree's first
//! compatible names the same vendor, as in `oneplus,iceland` for "OnePlus Pad
//! 4"; Android reports it as manufacturer and brand. The device name is the host's pretty hostname, else its hostname;
//! Android's system server copies it into `Settings.Global.DEVICE_NAME`.

use std::collections::BTreeMap;
use std::fs;

use crate::CellSpec;

const DEVICE_TREE_MODEL: &str = "/sys/firmware/devicetree/base/model";
const DEVICE_TREE_COMPATIBLE: &str = "/sys/firmware/devicetree/base/compatible";
const MACHINE_INFO: &str = "/etc/machine-info";
const HOSTNAME: &str = "/proc/sys/kernel/hostname";
const DEFAULT_MODEL: &str = "Droidloom";
/// Android's limit for a property value, without the terminating NUL.
const VALUE_BYTES: usize = 91;
const MODEL_PROPERTIES: [&str; 2] = ["ro.product.model", "ro.product.product.model"];
const MANUFACTURER_PROPERTIES: [&str; 2] = [
    "ro.product.manufacturer",
    "ro.product.product.manufacturer",
];
const BRAND_PROPERTIES: [&str; 2] = ["ro.product.brand", "ro.product.product.brand"];
pub(crate) const DEVICE_NAME_PROPERTY: &str = "ro.droidloom.device_name";

#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Identity {
    pub model: String,
    pub manufacturer: Option<String>,
    pub brand: Option<String>,
    pub name: Option<String>,
    pub extra_properties: BTreeMap<String, String>,
}

pub(crate) fn host() -> Identity {
    let read = |path| fs::read_to_string(path).ok();
    let tree_model = read(DEVICE_TREE_MODEL).and_then(|model| value(&model));
    let maker = tree_model.as_deref().zip(read(DEVICE_TREE_COMPATIBLE)).and_then(
        |(model, compatible)| maker(model, &compatible),
    );
    let model = tree_model.unwrap_or_else(|| DEFAULT_MODEL.to_owned());
    let name = read(MACHINE_INFO)
        .and_then(|info| pretty_hostname(&info))
        .or_else(|| read(HOSTNAME).and_then(|name| value(&name)));
    Identity {
        model,
        manufacturer: maker.clone(),
        brand: maker,
        name,
        extra_properties: BTreeMap::new(),
    }
}

pub(crate) fn resolve(spec: &CellSpec) -> Identity {
    let mut identity = host();
    if let Some(override_cfg) = spec.device_identity.as_ref() {
        if let Some(model) = override_cfg.model.as_deref().and_then(value) {
            identity.model = model;
        }
        if let Some(manufacturer) = override_cfg.manufacturer.as_deref().and_then(value) {
            identity.manufacturer = Some(manufacturer);
        }
        if let Some(brand) = override_cfg.brand.as_deref().and_then(value) {
            identity.brand = Some(brand);
        }
        if let Some(name) = override_cfg.device_name.as_deref().and_then(value) {
            identity.name = Some(name);
        }
        for (key, raw_value) in &override_cfg.extra_properties {
            if let Some(clean_value) = value(raw_value) {
                identity.extra_properties.insert(key.clone(), clean_value);
            } else if raw_value.is_empty() {
                identity.extra_properties.insert(key.clone(), String::new());
            }
        }
    }
    identity
}

/// The model's first word, when the first compatible's vendor prefix is that
/// word in lower case.
fn maker(model: &str, compatible: &str) -> Option<String> {
    let word = model.split_whitespace().next()?;
    let (vendor, _) = compatible.split('\0').next()?.split_once(',')?;
    (vendor == word.to_lowercase()).then(|| word.to_owned())
}

/// Replaces the image's model and device name in product properties, which
/// Android loads last.
pub(crate) fn product_properties(original: &str, identity: &Identity) -> String {
    let mut output = String::with_capacity(original.len() + 192);
    for line in original.lines() {
        if line.split_once('=').is_some_and(|(key, _)| {
            let key = key.trim();
            MODEL_PROPERTIES.contains(&key)
                || (identity.manufacturer.is_some() && MANUFACTURER_PROPERTIES.contains(&key))
                || (identity.brand.is_some() && BRAND_PROPERTIES.contains(&key))
                || key == DEVICE_NAME_PROPERTY
                || identity.extra_properties.contains_key(key)
        }) {
            continue;
        }
        output.push_str(line);
        output.push('\n');
    }
    for key in MODEL_PROPERTIES {
        output.push_str(&format!("{key}={}\n", identity.model));
    }
    if let Some(manufacturer) = &identity.manufacturer {
        for key in MANUFACTURER_PROPERTIES {
            output.push_str(&format!("{key}={manufacturer}\n"));
        }
    }
    if let Some(brand) = &identity.brand {
        for key in BRAND_PROPERTIES {
            output.push_str(&format!("{key}={brand}\n"));
        }
    }
    if let Some(name) = &identity.name {
        output.push_str(&format!("{DEVICE_NAME_PROPERTY}={name}\n"));
    }
    for (key, val) in &identity.extra_properties {
        output.push_str(&format!("{key}={val}\n"));
    }
    output
}

/// `PRETTY_HOSTNAME` from machine-info(5), which is environment-file syntax.
fn pretty_hostname(info: &str) -> Option<String> {
    let raw = info.lines().find_map(|line| {
        let (key, raw) = line.trim().split_once('=')?;
        (key.trim() == "PRETTY_HOSTNAME").then_some(raw.trim())
    })?;
    let unquoted = match raw.as_bytes() {
        [b'"', .., b'"'] if raw.len() >= 2 => {
            let mut text = String::new();
            let mut escaped = false;
            for c in raw[1..raw.len() - 1].chars() {
                if escaped || c != '\\' {
                    text.push(c);
                    escaped = false;
                } else {
                    escaped = true;
                }
            }
            text
        }
        [b'\'', .., b'\''] if raw.len() >= 2 => raw[1..raw.len() - 1].to_owned(),
        _ => raw.to_owned(),
    };
    value(&unquoted)
}

/// One property line: no control characters, at most Android's length.
fn value(text: &str) -> Option<String> {
    let clean: String = text
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect();
    let mut clean = clean.trim();
    while clean.len() > VALUE_BYTES {
        let mut end = VALUE_BYTES;
        while !clean.is_char_boundary(end) {
            end -= 1;
        }
        clean = clean[..end].trim_end();
    }
    (!clean.is_empty()).then(|| clean.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn device_tree_model_is_cleaned() {
        assert_eq!(value("OnePlus 12R\0").as_deref(), Some("OnePlus 12R"));
        assert_eq!(value("\0\n"), None);
        let long = "é".repeat(60);
        assert!(value(&long).unwrap().len() <= VALUE_BYTES);
    }

    #[test]
    fn pretty_hostname_reads_quoted_and_plain_values() {
        let info = "CHASSIS=handset\nPRETTY_HOSTNAME=\"Kai's \\\"phone\\\"\"\n";
        assert_eq!(pretty_hostname(info).as_deref(), Some("Kai's \"phone\""));
        assert_eq!(
            pretty_hostname("PRETTY_HOSTNAME=Aston").as_deref(),
            Some("Aston")
        );
        assert_eq!(pretty_hostname("PRETTY_HOSTNAME=''"), None);
        assert_eq!(pretty_hostname("CHASSIS=handset"), None);
    }

    #[test]
    fn replaces_the_image_identity_and_keeps_everything_else() {
        let identity = Identity {
            model: "OnePlus 12R".into(),
            manufacturer: Some("OnePlus".into()),
            brand: Some("OnePlus".into()),
            name: Some("AstonDenial".into()),
            extra_properties: BTreeMap::new(),
        };
        let input = "# image\nro.product.product.model=Cuttlefish arm64 phone\nro.product.product.brand=generic\nro.product.product.name=aosp_cf\n";
        let result = product_properties(input, &identity);
        assert_eq!(
            result,
            "# image\nro.product.product.name=aosp_cf\nro.product.model=OnePlus 12R\nro.product.product.model=OnePlus 12R\nro.product.manufacturer=OnePlus\nro.product.product.manufacturer=OnePlus\nro.product.brand=OnePlus\nro.product.product.brand=OnePlus\nro.droidloom.device_name=AstonDenial\n"
        );
        assert_eq!(product_properties(&result, &identity), result);
        let unnamed = Identity {
            model: DEFAULT_MODEL.into(),
            manufacturer: None,
            brand: None,
            name: None,
            extra_properties: BTreeMap::new(),
        };
        let result = product_properties(input, &unnamed);
        assert!(!result.contains(DEVICE_NAME_PROPERTY));
        assert!(result.contains("ro.product.product.brand=generic\n"));
    }

    #[test]
    fn resolve_applies_device_identity_overrides_and_extra_properties() {
        let spec = CellSpec {
            host_uid: 1000,
            subordinate_uids: crate::IdRange {
                start: 200_000,
                count: 100_000,
            },
            subordinate_gids: crate::IdRange {
                start: 300_000,
                count: 100_000,
            },
            image_dir: "/var/lib/droidloom/images/current".into(),
            gapps_dir: None,
            vendor_image: "/var/lib/droidloom/images/current/images/vendor.raw.img".into(),
            android_init: None,
            android_file_overrides: Vec::new(),
            android_runtime_directories: Vec::new(),
            shared_storage_directories: Vec::new(),
            share_user_folders: false,
            data_dir: "/var/lib/droidloom/users/1000/data".into(),
            runtime_dir: "/run/droidloom/cells/u1000".into(),
            gpu: Default::default(),
            render_node: "/dev/dri/renderD128".into(),
            graphics_backend: crate::GraphicsBackend::default(),
            denial_socket: "/run/user/1000/denial/native-bridge.sock".into(),
            network: None,
            device_identity: Some(crate::DeviceIdentityOverride {
                model: Some("Pixel 9 Pro".into()),
                manufacturer: Some("Google".into()),
                brand: Some("google".into()),
                device_name: Some("MyDroidloom".into()),
                extra_properties: BTreeMap::from([(
                    "ro.soc.model".into(),
                    "SM8750".into(),
                )]),
            }),
        };
        let resolved = resolve(&spec);
        assert_eq!(resolved.model, "Pixel 9 Pro");
        assert_eq!(resolved.manufacturer.as_deref(), Some("Google"));
        assert_eq!(resolved.brand.as_deref(), Some("google"));
        assert_eq!(resolved.name.as_deref(), Some("MyDroidloom"));
        assert_eq!(
            resolved.extra_properties.get("ro.soc.model").map(String::as_str),
            Some("SM8750")
        );
        let props = product_properties("ro.soc.model=old\n", &resolved);
        assert!(props.contains("ro.product.model=Pixel 9 Pro\n"));
        assert!(props.contains("ro.soc.model=SM8750\n"));
        assert!(!props.contains("ro.soc.model=old"));
    }

    #[test]
    fn maker_needs_the_compatible_vendor() {
        let compatible = "oneplus,iceland\0qcom,kaanapali\0";
        assert_eq!(maker("OnePlus Pad 4", compatible).as_deref(), Some("OnePlus"));
        assert_eq!(maker("Pad 4", compatible), None);
        assert_eq!(maker("OnePlus Pad 4", "qcom,kaanapali\0"), None);
        assert_eq!(maker("OnePlus Pad 4", "oneplus\0"), None);
    }
}

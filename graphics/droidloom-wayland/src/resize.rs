//! Latest window geometry awaiting application after a Wayland dispatch batch.

use std::time::{Duration, Instant};

use droidloom_window_policy::LogicalSize;

/// How long an interactive edge-drag resize must pause before sending the
/// intermediate geometry to Android and reallocating render targets. Releasing
/// the drag (`resizing == false`) applies immediately with zero delay.
pub const INTERACTIVE_RESIZE_DEBOUNCE: Duration = Duration::from_millis(150);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Configure {
    pub size: LogicalSize,
    pub scale_120: u32,
    pub remember: bool,
}

impl Configure {
    pub fn changes_geometry(self, size: Option<(u32, u32)>, scale_120: u32) -> bool {
        size != Some((self.size.width, self.size.height)) || scale_120 != self.scale_120
    }
}

pub struct Pending {
    size: Option<LogicalSize>,
    scale_120: u32,
    remember: bool,
    apply_after: Option<Instant>,
}

impl Pending {
    pub fn new(scale_120: u32) -> Self {
        Self {
            size: None,
            scale_120,
            remember: false,
            apply_after: None,
        }
    }

    // A zero XDG axis inherits the latest requested size, even before that
    // size has been applied. The caller resolves policy only for the first size.
    pub fn logical_size(&self, applied: Option<(u32, u32)>) -> Option<LogicalSize> {
        self.size
            .or_else(|| applied.map(|(width, height)| LogicalSize { width, height }))
    }

    #[cfg(test)]
    pub fn configure(&mut self, size: LogicalSize, remember: bool) {
        self.size = Some(size);
        self.remember = remember;
        self.apply_after = None;
    }

    /// Record a compositor configure. While an interactive border drag is active
    /// (`resizing == true`), debounce Android task resize and DMA-BUF pool
    /// reallocation until the drag pauses for [`INTERACTIVE_RESIZE_DEBOUNCE`] or
    /// finishes (`resizing == false`).
    pub fn configure_interactive(
        &mut self,
        size: LogicalSize,
        remember: bool,
        resizing: bool,
        now: Instant,
    ) {
        self.size = Some(size);
        self.remember = remember;
        self.apply_after = resizing.then(|| now + INTERACTIVE_RESIZE_DEBOUNCE);
    }

    pub fn preferred_scale(&mut self, scale_120: u32, applied: Option<(u32, u32)>) -> bool {
        if self.scale_120 == scale_120 {
            return false;
        }
        self.scale_120 = scale_120;
        // Scale alone must not allocate a provisional pool before the initial
        // XDG configure. It must also preserve a newer queued logical size.
        self.size = self.logical_size(applied);
        self.size.is_some()
    }

    pub fn deadline(&self) -> Option<Instant> {
        self.size.and(self.apply_after)
    }

    pub fn is_due(&self, now: Instant) -> bool {
        self.size.is_some() && self.apply_after.is_none_or(|due| now >= due)
    }

    pub fn take_ready(&mut self, now: Instant) -> Option<Configure> {
        if !self.is_due(now) {
            return None;
        }
        self.take()
    }

    pub fn take(&mut self) -> Option<Configure> {
        let size = self.size.take()?;
        let remember = std::mem::take(&mut self.remember);
        self.apply_after = None;
        Some(Configure {
            size,
            scale_120: self.scale_120,
            remember,
        })
    }

    pub fn cancel(&mut self) {
        self.size = None;
        self.remember = false;
        self.apply_after = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::readiness::{RESIZE, Tasks, UNMAP};
    use droidloom_denial_protocol::TaskObjectId;

    fn size(width: u32, height: u32) -> LogicalSize {
        LogicalSize { width, height }
    }

    #[test]
    fn burst_replaces_intermediate_sizes_and_keeps_windows_independent() {
        let mut work = Tasks::default();
        let first = TaskObjectId(1);
        let second = TaskObjectId(2);
        let mut windows = [(first, Pending::new(120)), (second, Pending::new(120))];
        for width in 800..1000 {
            windows[0].1.configure(size(width, 600), false);
            work.mark(first, RESIZE);
        }
        windows[1].1.configure(size(640, 480), true);
        work.mark(second, RESIZE);
        let mut applied = Vec::new();
        while let Some(object) = work.pop(RESIZE) {
            let pending = &mut windows.iter_mut().find(|(id, _)| *id == object).unwrap().1;
            applied.push((object, pending.take().unwrap()));
        }
        assert_eq!(
            applied,
            [
                (
                    first,
                    Configure {
                        size: size(999, 600),
                        scale_120: 120,
                        remember: false
                    }
                ),
                (
                    second,
                    Configure {
                        size: size(640, 480),
                        scale_120: 120,
                        remember: true
                    }
                ),
            ]
        );
        assert!(!work.pending());
    }

    #[test]
    fn unspecified_axes_inherit_pending_size_before_applied_size() {
        let mut pending = Pending::new(120);
        let applied = Some((800, 600));
        pending.configure(size(1000, 700), false);
        let previous = pending.logical_size(applied).unwrap();
        pending.configure(size(previous.width, 750), false);
        let previous = pending.logical_size(applied).unwrap();
        pending.configure(size(1100, previous.height), true);
        assert_eq!(pending.take().unwrap().size, size(1100, 750));
        assert!(pending.take().is_none());
    }

    #[test]
    fn scale_and_initial_configure_order_produce_one_final_geometry() {
        for scale_first in [false, true] {
            let mut pending = Pending::new(120);
            if scale_first {
                assert!(!pending.preferred_scale(150, None));
                assert!(pending.take().is_none());
            }
            pending.configure(size(900, 700), false);
            if !scale_first {
                assert!(pending.preferred_scale(150, None));
            }
            assert_eq!(
                pending.take(),
                Some(Configure {
                    size: size(900, 700),
                    scale_120: 150,
                    remember: false,
                })
            );
        }
    }

    #[test]
    fn scale_updates_preserve_pending_size_and_scale_only_uses_applied_size() {
        let mut pending = Pending::new(120);
        pending.configure(size(1000, 700), true);
        assert!(pending.preferred_scale(150, Some((800, 600))));
        assert!(pending.preferred_scale(180, Some((800, 600))));
        assert!(!pending.preferred_scale(180, Some((800, 600))));
        assert_eq!(
            pending.take(),
            Some(Configure {
                size: size(1000, 700),
                scale_120: 180,
                remember: true,
            })
        );
        assert!(pending.preferred_scale(120, Some((1000, 700))));
        assert_eq!(
            pending.take(),
            Some(Configure {
                size: size(1000, 700),
                scale_120: 120,
                remember: false,
            })
        );
    }

    #[test]
    fn return_to_applied_geometry_skips_pool_but_preserves_final_persistence() {
        let mut pending = Pending::new(120);
        let applied = Some((800, 600));
        pending.configure(size(1200, 900), true);
        pending.preferred_scale(180, applied);
        pending.configure(size(800, 600), false);
        pending.preferred_scale(120, applied);
        pending.configure(size(800, 600), true);
        let configure = pending.take().unwrap();
        assert!(!configure.changes_geometry(applied, 120));
        assert!(configure.remember);
        assert!(configure.changes_geometry(applied, 150));
        assert!(configure.changes_geometry(None, 120));
    }

    #[test]
    fn close_cancels_resize_before_allocation_and_removal_cancels_wakeup() {
        let object = TaskObjectId(7);
        let mut work = Tasks::default();
        let mut pending = Pending::new(120);
        pending.configure(size(1200, 900), true);
        work.mark(object, RESIZE | UNMAP);
        assert_eq!(work.pop(UNMAP), Some(object));
        pending.cancel();
        assert_eq!(work.pop(RESIZE), Some(object));
        assert!(pending.take().is_none());
        pending.configure(size(900, 700), false);
        work.mark(object, RESIZE);
        work.remove(object);
        assert_eq!(work.pop(RESIZE), None);
        assert!(!work.pending());
    }

    #[test]
    fn interactive_drag_debounces_until_pause_or_release() {
        let start = Instant::now();
        let mut pending = Pending::new(120);
        pending.configure_interactive(size(900, 700), false, true, start);
        assert!(!pending.is_due(start));
        assert!(pending.take_ready(start + Duration::from_millis(50)).is_none());
        assert_eq!(pending.deadline(), Some(start + INTERACTIVE_RESIZE_DEBOUNCE));

        // Another drag tick pushes the debounce deadline forward.
        let tick = start + Duration::from_millis(100);
        pending.configure_interactive(size(1050, 750), false, true, tick);
        assert!(pending.take_ready(start + INTERACTIVE_RESIZE_DEBOUNCE).is_none());
        assert_eq!(pending.deadline(), Some(tick + INTERACTIVE_RESIZE_DEBOUNCE));

        // Pausing past the debounce window makes the latest size ready.
        let paused = tick + INTERACTIVE_RESIZE_DEBOUNCE;
        assert!(pending.is_due(paused));
        assert_eq!(
            pending.take_ready(paused),
            Some(Configure {
                size: size(1050, 750),
                scale_120: 120,
                remember: false,
            })
        );
        assert_eq!(pending.deadline(), None);

        // Releasing the drag (resizing == false) applies immediately.
        let drag2 = paused + Duration::from_millis(10);
        pending.configure_interactive(size(1100, 800), false, true, drag2);
        let release = drag2 + Duration::from_millis(20);
        pending.configure_interactive(size(1120, 810), true, false, release);
        assert!(pending.is_due(release));
        assert_eq!(pending.deadline(), None);
        assert_eq!(
            pending.take_ready(release),
            Some(Configure {
                size: size(1120, 810),
                scale_120: 120,
                remember: true,
            })
        );
    }
}

/*
 * Copyright 2026 Droidloom. SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Freestanding Bionic-compatible preload shim for Android `netd` in true
 * host-network mode (--net=host). Ensures Android `netd` cannot overwrite
 * host routing tables/rules, set SO_MARK, bind DNS/monitor sockets to the
 * dummy `wlan0` anchor, or mutate real host interfaces (wlp1s0/tailscale0),
 * while allowing operations targeting `wlan0` and synthesizing standard
 * Netlink NLMSG_ERROR(0) ACKs for intercepted control messages.
 */

#include <asm/socket.h>
#include <asm/unistd.h>
#include <errno.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <linux/sockios.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>

typedef long ssize_t;

#ifndef SOL_SOCKET
#define SOL_SOCKET 1
#endif
#ifndef SO_BINDTODEVICE
#define SO_BINDTODEVICE 25
#endif
#ifndef SO_MARK
#define SO_MARK 36
#endif
#ifndef SO_PROTOCOL
#define SO_PROTOCOL 38
#endif
#ifndef SO_DOMAIN
#define SO_DOMAIN 39
#endif

#if !defined(__aarch64__)
#error "hostnet_shim currently targets aarch64 Linux syscalls"
#endif

#ifndef AT_FDCWD
#define AT_FDCWD -100
#endif

#ifndef AF_INET
#define AF_INET 2
#endif

#ifndef AF_NETLINK
#define AF_NETLINK 16
#endif

#ifndef SOCK_DGRAM
#define SOCK_DGRAM 2
#endif

#ifndef MSG_PEEK
#define MSG_PEEK 0x2
#endif

#ifndef POLLIN
#define POLLIN 0x0001
#endif

#ifndef NLM_F_ACK
#define NLM_F_ACK 0x4
#endif

#ifndef SIOCADDRT
#define SIOCADDRT 0x890B
#define SIOCDELRT 0x890C
#endif

#ifndef SIOCDIFADDR
#define SIOCDIFADDR 0x8936
#endif

extern int *__errno(void);

struct shim_iovec {
  void *iov_base;
  size_t iov_len;
};

struct shim_msghdr {
  void *msg_name;
  uint32_t msg_namelen;
  uint32_t __pad1;
  struct shim_iovec *msg_iov;
  size_t msg_iovlen;
  void *msg_control;
  size_t msg_controllen;
  int msg_flags;
  int __pad2;
};

struct shim_pollfd {
  int fd;
  short events;
  short revents;
};

struct shim_timespec {
  long tv_sec;
  long tv_nsec;
};

struct shim_ifreq_hdr {
  char name[16];
  int ivalue;
  char pad[20];
};

struct shim_nl_ack {
  struct nlmsghdr hdr;
  struct nlmsgerr err;
};

#define MAX_TRACKED_FDS 1024

static atomic_int g_ack_pending[MAX_TRACKED_FDS];
static struct shim_nl_ack g_ack_msg[MAX_TRACKED_FDS];

static inline long raw_syscall6(long nr, long a0, long a1, long a2, long a3,
                                long a4, long a5) {
  register long x8 __asm__("x8") = nr;
  register long x0 __asm__("x0") = a0;
  register long x1 __asm__("x1") = a1;
  register long x2 __asm__("x2") = a2;
  register long x3 __asm__("x3") = a3;
  register long x4 __asm__("x4") = a4;
  register long x5 __asm__("x5") = a5;
  __asm__ volatile("svc #0"
                   : "+r"(x0)
                   : "r"(x8), "r"(x1), "r"(x2), "r"(x3), "r"(x4), "r"(x5)
                   : "memory", "cc");
  return x0;
}

static inline long set_errno_ret(long ret) {
  if (ret < 0 && ret >= -4095) {
    int *ep = __errno();
    if (ep != NULL) {
      *ep = (int)(-ret);
    }
    return -1;
  }
  return ret;
}

static void shim_memcpy(void *dst, const void *src, size_t n) {
  uint8_t *d = (uint8_t *)dst;
  const uint8_t *s = (const uint8_t *)src;
  for (size_t i = 0; i < n; i++) {
    d[i] = s[i];
  }
}

static void shim_memset(void *dst, uint8_t val, size_t n) {
  uint8_t *d = (uint8_t *)dst;
  for (size_t i = 0; i < n; i++) {
    d[i] = val;
  }
}

static int shim_streq(const char *a, const char *b) {
  if (a == NULL || b == NULL) {
    return 0;
  }
  while (*a != '\0' && *b != '\0') {
    if (*a != *b) {
      return 0;
    }
    a++;
    b++;
  }
  return *a == *b;
}

static int shim_is_wlan0_sockopt(const void *optval, uint32_t optlen) {
  if (optval == NULL || optlen < 5 || optlen > 16) {
    return 0;
  }
  const char *s = (const char *)optval;
  if (s[0] != 'w' || s[1] != 'l' || s[2] != 'a' || s[3] != 'n' || s[4] != '0') {
    return 0;
  }
  return optlen == 5 || s[5] == '\0';
}

static int is_netlink_route(int fd) {
  if (fd < 0) {
    return 0;
  }
  int dom = 0;
  uint32_t dom_len = sizeof(dom);
  long r = raw_syscall6(__NR_getsockopt, fd, SOL_SOCKET, SO_DOMAIN, (long)&dom,
                        (long)&dom_len, 0);
  if (r < 0 || dom != AF_NETLINK) {
    return 0;
  }
  int proto = -1;
  uint32_t proto_len = sizeof(proto);
  r = raw_syscall6(__NR_getsockopt, fd, SOL_SOCKET, SO_PROTOCOL, (long)&proto,
                   (long)&proto_len, 0);
  return r >= 0 && proto == NETLINK_ROUTE;
}

static int query_wlan0_ifindex(void) {
  long s = raw_syscall6(__NR_socket, AF_INET, SOCK_DGRAM, 0, 0, 0, 0);
  if (s < 0) {
    return -1;
  }
  struct shim_ifreq_hdr ifr;
  shim_memset(&ifr, 0, sizeof(ifr));
  shim_memcpy(ifr.name, "wlan0", 6);
  long r = raw_syscall6(__NR_ioctl, s, SIOCGIFINDEX, (long)&ifr, 0, 0, 0);
  raw_syscall6(__NR_close, s, 0, 0, 0, 0, 0);
  if (r < 0) {
    return -1;
  }
  return ifr.ivalue;
}

static int should_intercept_netlink(const uint8_t *head, size_t head_len,
                                    struct nlmsghdr *out_hdr) {
  if (head_len < sizeof(struct nlmsghdr)) {
    return 0;
  }
  struct nlmsghdr hdr;
  shim_memcpy(&hdr, head, sizeof(hdr));
  *out_hdr = hdr;

  switch (hdr.nlmsg_type) {
  case RTM_NEWRULE:
  case RTM_DELRULE:
  case RTM_NEWROUTE:
  case RTM_DELROUTE:
    return 1;
  case RTM_NEWADDR:
  case RTM_DELADDR:
  case RTM_NEWLINK:
  case RTM_DELLINK:
  case RTM_SETLINK:
  case RTM_NEWNEIGH:
  case RTM_DELNEIGH:
  case RTM_NEWQDISC:
  case RTM_DELQDISC:
  case RTM_NEWTCLASS:
  case RTM_DELTCLASS:
  case RTM_NEWTFILTER:
  case RTM_DELTFILTER: {
    if (head_len < sizeof(struct nlmsghdr) + 8) {
      return 1;
    }
    int32_t ifindex = 0;
    shim_memcpy(&ifindex, head + sizeof(struct nlmsghdr) + 4, sizeof(ifindex));
    int wlan0_idx = query_wlan0_ifindex();
    if (wlan0_idx > 0 && ifindex == wlan0_idx) {
      return 0;
    }
    return 1;
  }
  default:
    return 0;
  }
}

static size_t flatten_iov_head(const struct shim_iovec *iov, size_t iovcnt,
                               uint8_t *out, size_t cap, size_t *total_len) {
  size_t copied = 0;
  size_t total = 0;
  for (size_t i = 0; i < iovcnt; i++) {
    if (iov[i].iov_base == NULL || iov[i].iov_len == 0) {
      continue;
    }
    size_t len = iov[i].iov_len;
    total += len;
    if (copied < cap) {
      size_t take = cap - copied;
      if (len < take) {
        take = len;
      }
      shim_memcpy(out + copied, iov[i].iov_base, take);
      copied += take;
    }
  }
  *total_len = total;
  return copied;
}

static ssize_t handle_netlink_tx(int fd, const struct shim_iovec *iov,
                                 size_t iovcnt, int *intercepted) {
  *intercepted = 0;
  if (!is_netlink_route(fd)) {
    return 0;
  }
  uint8_t head[32];
  size_t total_len = 0;
  size_t head_len = flatten_iov_head(iov, iovcnt, head, sizeof(head), &total_len);
  struct nlmsghdr hdr;
  if (!should_intercept_netlink(head, head_len, &hdr)) {
    return 0;
  }
  *intercepted = 1;
  if ((hdr.nlmsg_flags & NLM_F_ACK) && fd >= 0 && fd < MAX_TRACKED_FDS) {
    struct shim_nl_ack ack;
    shim_memset(&ack, 0, sizeof(ack));
    ack.hdr.nlmsg_len = (uint32_t)sizeof(ack);
    ack.hdr.nlmsg_type = NLMSG_ERROR;
    ack.hdr.nlmsg_seq = hdr.nlmsg_seq;
    ack.err.error = 0;
    ack.err.msg = hdr;
    g_ack_msg[fd] = ack;
    atomic_store_explicit(&g_ack_pending[fd], 1, memory_order_release);
  }
  return (ssize_t)total_len;
}

static ssize_t deliver_pending_ack_iov(int fd, const struct shim_iovec *iov,
                                       size_t iovcnt, void *src_addr,
                                       uint32_t *src_len, int flags,
                                       int *handled) {
  *handled = 0;
  if (fd < 0 || fd >= MAX_TRACKED_FDS) {
    return 0;
  }
  if (atomic_load_explicit(&g_ack_pending[fd], memory_order_acquire) != 1) {
    return 0;
  }
  if (!is_netlink_route(fd)) {
    atomic_store_explicit(&g_ack_pending[fd], 0, memory_order_release);
    return 0;
  }
  *handled = 1;
  struct shim_nl_ack ack = g_ack_msg[fd];
  if (!(flags & MSG_PEEK)) {
    atomic_store_explicit(&g_ack_pending[fd], 0, memory_order_release);
  }
  const uint8_t *src = (const uint8_t *)&ack;
  size_t remain = sizeof(ack);
  size_t copied = 0;
  for (size_t i = 0; i < iovcnt && remain > 0; i++) {
    if (iov[i].iov_base == NULL || iov[i].iov_len == 0) {
      continue;
    }
    size_t chunk = iov[i].iov_len < remain ? iov[i].iov_len : remain;
    shim_memcpy(iov[i].iov_base, src + copied, chunk);
    copied += chunk;
    remain -= chunk;
  }
  if (src_addr != NULL && src_len != NULL) {
    struct sockaddr_nl sa;
    shim_memset(&sa, 0, sizeof(sa));
    sa.nl_family = AF_NETLINK;
    size_t copy_sa = *src_len < sizeof(sa) ? *src_len : sizeof(sa);
    shim_memcpy(src_addr, &sa, copy_sa);
    *src_len = (uint32_t)sizeof(sa);
  }
  return (ssize_t)copied;
}

int socket(int domain, int type, int protocol) {
  long fd = set_errno_ret(
      raw_syscall6(__NR_socket, domain, type, protocol, 0, 0, 0));
  if (fd >= 0 && fd < MAX_TRACKED_FDS) {
    atomic_store_explicit(&g_ack_pending[fd], 0, memory_order_release);
  }
  return (int)fd;
}

int close(int fd) {
  if (fd >= 0 && fd < MAX_TRACKED_FDS) {
    atomic_store_explicit(&g_ack_pending[fd], 0, memory_order_release);
  }
  return (int)set_errno_ret(raw_syscall6(__NR_close, fd, 0, 0, 0, 0, 0));
}

int setsockopt(int fd, int level, int optname, const void *optval,
               uint32_t optlen) {
  if (level == SOL_SOCKET) {
    if (optname == SO_MARK) {
      return 0;
    }
    if (optname == SO_BINDTODEVICE && shim_is_wlan0_sockopt(optval, optlen)) {
      return 0;
    }
  }
  return (int)set_errno_ret(raw_syscall6(__NR_setsockopt, fd, level, optname,
                                         (long)optval, (long)optlen, 0));
}

ssize_t write(int fd, const void *buf, size_t count) {
  struct shim_iovec iov = {(void *)buf, count};
  int intercepted = 0;
  ssize_t res = handle_netlink_tx(fd, &iov, 1, &intercepted);
  if (intercepted) {
    return res;
  }
  return (ssize_t)set_errno_ret(
      raw_syscall6(__NR_write, fd, (long)buf, (long)count, 0, 0, 0));
}

ssize_t __write_chk(int fd, const void *buf, size_t count,
                    size_t buf_size __attribute__((unused))) {
  return write(fd, buf, count);
}

ssize_t writev(int fd, const struct shim_iovec *iov, int iovcnt) {
  if (iovcnt > 0 && iov != NULL) {
    int intercepted = 0;
    ssize_t res = handle_netlink_tx(fd, iov, (size_t)iovcnt, &intercepted);
    if (intercepted) {
      return res;
    }
  }
  return (ssize_t)set_errno_ret(
      raw_syscall6(__NR_writev, fd, (long)iov, iovcnt, 0, 0, 0));
}

ssize_t sendto(int fd, const void *buf, size_t len, int flags,
               const void *dest_addr, uint32_t addrlen) {
  struct shim_iovec iov = {(void *)buf, len};
  int intercepted = 0;
  ssize_t res = handle_netlink_tx(fd, &iov, 1, &intercepted);
  if (intercepted) {
    return res;
  }
  return (ssize_t)set_errno_ret(raw_syscall6(__NR_sendto, fd, (long)buf,
                                             (long)len, flags, (long)dest_addr,
                                             (long)addrlen));
}

ssize_t __sendto_chk(int fd, const void *buf, size_t len,
                     size_t buflen __attribute__((unused)), int flags,
                     const void *dest_addr, uint32_t addrlen) {
  return sendto(fd, buf, len, flags, dest_addr, addrlen);
}

ssize_t send(int fd, const void *buf, size_t len, int flags) {
  return sendto(fd, buf, len, flags, NULL, 0);
}

ssize_t sendmsg(int fd, const struct shim_msghdr *msg, int flags) {
  if (msg != NULL && msg->msg_iov != NULL && msg->msg_iovlen > 0) {
    int intercepted = 0;
    ssize_t res =
        handle_netlink_tx(fd, msg->msg_iov, msg->msg_iovlen, &intercepted);
    if (intercepted) {
      return res;
    }
  }
  return (ssize_t)set_errno_ret(
      raw_syscall6(__NR_sendmsg, fd, (long)msg, flags, 0, 0, 0));
}

ssize_t read(int fd, void *buf, size_t count) {
  struct shim_iovec iov = {buf, count};
  int handled = 0;
  ssize_t res = deliver_pending_ack_iov(fd, &iov, 1, NULL, NULL, 0, &handled);
  if (handled) {
    return res;
  }
  return (ssize_t)set_errno_ret(
      raw_syscall6(__NR_read, fd, (long)buf, (long)count, 0, 0, 0));
}

ssize_t __read_chk(int fd, void *buf, size_t count,
                   size_t buf_size __attribute__((unused))) {
  return read(fd, buf, count);
}

ssize_t recvfrom(int fd, void *buf, size_t len, int flags, void *src_addr,
                 uint32_t *addrlen) {
  struct shim_iovec iov = {buf, len};
  int handled = 0;
  ssize_t res =
      deliver_pending_ack_iov(fd, &iov, 1, src_addr, addrlen, flags, &handled);
  if (handled) {
    return res;
  }
  return (ssize_t)set_errno_ret(raw_syscall6(__NR_recvfrom, fd, (long)buf,
                                             (long)len, flags, (long)src_addr,
                                             (long)addrlen));
}

ssize_t __recvfrom_chk(int fd, void *buf, size_t len,
                       size_t buflen __attribute__((unused)), int flags,
                       void *src_addr, uint32_t *addrlen) {
  return recvfrom(fd, buf, len, flags, src_addr, addrlen);
}

ssize_t recv(int fd, void *buf, size_t len, int flags) {
  return recvfrom(fd, buf, len, flags, NULL, NULL);
}

ssize_t recvmsg(int fd, struct shim_msghdr *msg, int flags) {
  if (msg != NULL && msg->msg_iov != NULL && msg->msg_iovlen > 0) {
    int handled = 0;
    uint32_t namelen = msg->msg_namelen;
    ssize_t res = deliver_pending_ack_iov(
        fd, msg->msg_iov, msg->msg_iovlen, msg->msg_name,
        msg->msg_name ? &namelen : NULL, flags, &handled);
    if (handled) {
      if (msg->msg_name != NULL) {
        msg->msg_namelen = namelen;
      }
      msg->msg_controllen = 0;
      msg->msg_flags = 0;
      return res;
    }
  }
  return (ssize_t)set_errno_ret(
      raw_syscall6(__NR_recvmsg, fd, (long)msg, flags, 0, 0, 0));
}

int poll(struct shim_pollfd *fds, unsigned long nfds, int timeout) {
  if (fds != NULL && nfds > 0) {
    int ready = 0;
    for (unsigned long i = 0; i < nfds; i++) {
      fds[i].revents = 0;
      int fd = fds[i].fd;
      if ((fds[i].events & POLLIN) && fd >= 0 && fd < MAX_TRACKED_FDS &&
          atomic_load_explicit(&g_ack_pending[fd], memory_order_acquire) == 1 &&
          is_netlink_route(fd)) {
        fds[i].revents = POLLIN;
        ready++;
      }
    }
    if (ready > 0) {
      return ready;
    }
  }
  struct shim_timespec ts;
  struct shim_timespec *pts = NULL;
  if (timeout >= 0) {
    ts.tv_sec = timeout / 1000;
    ts.tv_nsec = (timeout % 1000) * 1000000L;
    pts = &ts;
  }
  return (int)set_errno_ret(
      raw_syscall6(__NR_ppoll, (long)fds, (long)nfds, (long)pts, 0, 0, 0));
}

int ioctl(int fd, int request, ...) {
  va_list ap;
  va_start(ap, request);
  unsigned long arg = va_arg(ap, unsigned long);
  va_end(ap);

  uint32_t req = (uint32_t)request;
  if (req == SIOCADDRT || req == SIOCDELRT) {
    return 0;
  }
  if (req == SIOCSIFFLAGS || req == SIOCSIFADDR || req == SIOCSIFDSTADDR ||
      req == SIOCSIFBRDADDR || req == SIOCSIFNETMASK || req == SIOCSIFMTU ||
      req == SIOCDIFADDR) {
    const struct shim_ifreq_hdr *ifr = (const struct shim_ifreq_hdr *)arg;
    if (ifr == NULL || !shim_streq(ifr->name, "wlan0")) {
      return 0;
    }
  }
  return (int)set_errno_ret(
      raw_syscall6(__NR_ioctl, fd, (long)req, (long)arg, 0, 0, 0));
}

int access(const char *path, int mode) {
  if (path != NULL && (shim_streq(path, "/proc/net/ip_tables_names") ||
                       shim_streq(path, "/proc/net/ip6_tables_names"))) {
    int *ep = __errno();
    if (ep != NULL) {
      *ep = ENOENT;
    }
    return -1;
  }
  return (int)set_errno_ret(
      raw_syscall6(__NR_faccessat, AT_FDCWD, (long)path, mode, 0, 0, 0));
}

//! Serving the helper on a Unix socket only the current user can use, or on
//! one connection its parent passed down.

use std::{
    io,
    os::{
        fd::{FromRawFd, RawFd},
        unix::fs::{FileTypeExt, PermissionsExt},
    },
    path::Path,
    pin::Pin,
    task::{Context, Poll},
};

use tokio::{
    io::{AsyncRead, AsyncWrite, ReadBuf},
    net::{UnixListener, UnixStream},
    sync::watch,
};
use tokio_stream::{Stream, StreamExt, wrappers::UnixListenerStream};
use tonic::transport::server::{Connected, UdsConnectInfo};

/// Binds `path` with owner-only permissions. A stale socket from a crashed
/// helper is replaced; a live one is an error.
pub fn bind(path: &Path) -> io::Result<UnixListener> {
    if let Ok(metadata) = std::fs::symlink_metadata(path) {
        if !metadata.file_type().is_socket() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                format!("{} exists and is not a socket", path.display()),
            ));
        }
        if std::os::unix::net::UnixStream::connect(path).is_ok() {
            return Err(io::Error::new(
                io::ErrorKind::AddrInUse,
                format!("Another helper is already serving {}", path.display()),
            ));
        }
        std::fs::remove_file(path)?;
    }
    // SAFETY: umask only changes this process's file creation mask.
    let previous = unsafe { libc::umask(0o077) };
    let listener = UnixListener::bind(path);
    unsafe { libc::umask(previous) };
    let listener = listener?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    Ok(listener)
}

/// Accepted connections from processes running as this user only.
pub fn same_user_connections(
    listener: UnixListener,
) -> impl Stream<Item = io::Result<tokio::net::UnixStream>> {
    // SAFETY: geteuid has no preconditions.
    let user = unsafe { libc::geteuid() };
    UnixListenerStream::new(listener).filter(move |connection| match connection {
        Ok(stream) => stream.peer_cred().is_ok_and(|peer| peer.uid() == user),
        Err(_) => true,
    })
}

/// The connections the helper serves.
pub type Incoming = Pin<Box<dyn Stream<Item = io::Result<Connection>> + Send>>;

/// A served connection; an inherited one stops the helper when it closes.
pub struct Connection {
    stream: UnixStream,
    on_close: Option<watch::Sender<bool>>,
}

impl Drop for Connection {
    fn drop(&mut self) {
        if let Some(on_close) = self.on_close.take() {
            let _ = on_close.send(true);
        }
    }
}

impl Connected for Connection {
    type ConnectInfo = UdsConnectInfo;

    fn connect_info(&self) -> Self::ConnectInfo {
        self.stream.connect_info()
    }
}

impl AsyncRead for Connection {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_read(context, buffer)
    }
}

impl AsyncWrite for Connection {
    fn poll_write(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.stream).poll_write(context, buffer)
    }

    fn poll_flush(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_flush(context)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stream).poll_shutdown(context)
    }
}

/// Same-user connections accepted on `listener`.
pub fn listener_incoming(listener: UnixListener) -> Incoming {
    Box::pin(same_user_connections(listener).map(|connection| {
        connection.map(|stream| Connection {
            stream,
            on_close: None,
        })
    }))
}

/// The one connection on inherited descriptor `fd`, such as a socketpair end
/// its parent kept the other end of. `on_close` is told when it closes, so the
/// helper never outlives the only client that can reach it.
pub fn inherited_connection(fd: RawFd, on_close: watch::Sender<bool>) -> io::Result<Incoming> {
    // SAFETY: fstat writes only into the zeroed buffer it is given.
    let mut status: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd, &mut status) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if status.st_mode & libc::S_IFMT != libc::S_IFSOCK {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "not a socket"));
    }
    // Git and other children must not hold the connection open.
    // SAFETY: fcntl on a descriptor this process owns.
    if unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } != 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: the parent handed this descriptor to the helper alone; it is
    // checked to be a socket above and owned from here on.
    let stream = unsafe { std::os::unix::net::UnixStream::from_raw_fd(fd) };
    stream.set_nonblocking(true)?;
    let stream = UnixStream::from_std(stream)?;
    // SAFETY: geteuid has no preconditions.
    let user = unsafe { libc::geteuid() };
    if !stream.peer_cred().is_ok_and(|peer| peer.uid() == user) {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "the peer is another user",
        ));
    }
    let connection = Connection {
        stream,
        on_close: Some(on_close),
    };
    Ok(Box::pin(
        tokio_stream::once(Ok(connection)).chain(tokio_stream::pending()),
    ))
}

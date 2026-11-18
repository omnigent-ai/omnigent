//! A client of both services on one connection, as the host holds them.

#![allow(dead_code)]

use std::path::Path;

use hyper_util::rt::TokioIo;
use tokio::net::UnixStream;
use tonic::{
    service::interceptor::InterceptedService,
    transport::{Channel, Endpoint, Uri},
};
use tower::service_fn;
use workspace_helper::{
    proto::{
        CloseWorkspaceRequest, CloseWorkspaceResponse, GetInfoRequest, GetInfoResponse,
        OpenWorkspaceRequest, OpenWorkspaceResponse,
        workspace_helper_service_client::WorkspaceHelperServiceClient,
        workspace_service_client::WorkspaceServiceClient,
    },
    server::WORKSPACE_HEADER,
};

type Pinned = std::sync::Arc<std::sync::Mutex<String>>;
type Pin = Box<dyn FnMut(tonic::Request<()>) -> Result<tonic::Request<()>, tonic::Status> + Send>;

/// Both services on one connection, as the host holds them. Workspace calls
/// name the last opened workspace, or the one `pin` chose, as the host would.
pub struct Client {
    control: WorkspaceHelperServiceClient<Channel>,
    workspace: WorkspaceServiceClient<InterceptedService<Channel, Pin>>,
    pinned: Pinned,
}

impl Client {
    fn new(channel: Channel) -> Self {
        let pinned = Pinned::default();
        let header = pinned.clone();
        let pin: Pin = Box::new(move |mut request| {
            let id = header.lock().unwrap().clone();
            if !id.is_empty() {
                request
                    .metadata_mut()
                    .insert(WORKSPACE_HEADER, id.parse().unwrap());
            }
            Ok(request)
        });
        Self {
            control: WorkspaceHelperServiceClient::new(channel.clone()),
            workspace: WorkspaceServiceClient::with_interceptor(channel, pin),
            pinned,
        }
    }

    /// Names `workspace_id` in the workspace calls that follow.
    pub fn pin(&self, workspace_id: &str) {
        *self.pinned.lock().unwrap() = workspace_id.to_string();
    }

    pub async fn get_info(
        &mut self,
        request: GetInfoRequest,
    ) -> Result<tonic::Response<GetInfoResponse>, tonic::Status> {
        self.control.get_info(request).await
    }

    pub async fn open_workspace(
        &mut self,
        request: OpenWorkspaceRequest,
    ) -> Result<tonic::Response<OpenWorkspaceResponse>, tonic::Status> {
        let opened = self.control.open_workspace(request).await?;
        self.pin(&opened.get_ref().workspace_id);
        Ok(opened)
    }

    pub async fn close_workspace(
        &mut self,
        request: CloseWorkspaceRequest,
    ) -> Result<tonic::Response<CloseWorkspaceResponse>, tonic::Status> {
        self.control.close_workspace(request).await
    }
}

impl std::ops::Deref for Client {
    type Target = WorkspaceServiceClient<InterceptedService<Channel, Pin>>;

    fn deref(&self) -> &Self::Target {
        &self.workspace
    }
}

impl std::ops::DerefMut for Client {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.workspace
    }
}

pub async fn connect(path: &Path) -> Client {
    let path = path.to_path_buf();
    let channel = Endpoint::try_from("http://workspace-helper")
        .unwrap()
        .connect_with_connector(service_fn(move |_: Uri| {
            let path = path.clone();
            async move { Ok::<_, std::io::Error>(TokioIo::new(UnixStream::connect(path).await?)) }
        }))
        .await
        .unwrap();
    Client::new(channel)
}

/// A client on an already connected stream, such as one end of a socketpair.
pub async fn connect_stream(stream: std::os::unix::net::UnixStream) -> Client {
    stream.set_nonblocking(true).unwrap();
    let stream = std::sync::Mutex::new(Some(UnixStream::from_std(stream).unwrap()));
    let channel = Endpoint::try_from("http://workspace-helper")
        .unwrap()
        .connect_with_connector(service_fn(move |_: Uri| {
            let stream = stream.lock().unwrap().take();
            async move {
                stream
                    .map(TokioIo::new)
                    .ok_or_else(|| std::io::Error::other("the connection is already in use"))
            }
        }))
        .await
        .unwrap();
    Client::new(channel)
}

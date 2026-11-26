//! Mount the build service at the workspace origin. The only destination is the operator's Unix socket.
use std::{net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};

use axum::{Router, body::Body, extract::{Request, State}, http::{HeaderValue, Method, StatusCode, Uri, Version, header}, middleware::{self, Next}, response::Response, routing::{get, post}};
use hyper::client::conn::http1;
use hyper_util::rt::TokioIo;
use tokio::{net::UnixStream, time::timeout};

#[derive(Clone)]
struct Peer { socket: PathBuf }

pub(super) fn with_build(workspace: Router, socket: PathBuf, address: SocketAddr) -> Router {
    let reads = ["/", "/desk.css", "/game.js", "/projects.js", "/feedback.js", "/project/profiles", "/project/library",
        "/project/library/{id}", "/project/status/{id}", "/project/package/{id}", "/game/status",
        "/game/status/{id}", "/game/package/{id}", "/maintenance/status"];
    let writes = ["/project/build", "/project/reports/{id}", "/project/reviews/{id}/{issue}", "/game/build", "/maintenance/update"];
    let mut front = Router::new();
    for path in reads { front = front.route(path, get(forward)); }
    for path in writes { front = front.route(path, post(forward)); }
    let authority: Arc<str> = address.to_string().into();
    let origin: Arc<str> = format!("http://{address}").into();
    front.with_state(Arc::new(Peer { socket })).fallback_service(workspace)
        .layer(middleware::from_fn(move |req: Request, next: Next| {
            let (authority, origin) = (authority.clone(), origin.clone());
            async move {
                let h = req.headers();
                let is_single = |name: &str, value: &str| {
                    let mut values = h.get_all(name).iter();
                    values.next().is_some_and(|v| v.as_bytes() == value.as_bytes()) && values.next().is_none()
                };
                let safe = matches!(*req.method(), Method::GET | Method::HEAD);
                if !is_single("host", &authority) || ((!safe || h.contains_key("origin")) && !is_single("origin", &origin))
                    || (h.contains_key("sec-fetch-site") && !is_single("sec-fetch-site", "same-origin") && !(safe && is_single("sec-fetch-site", "none"))) {
                    return Err(StatusCode::FORBIDDEN);
                }
                Ok(next.run(req).await)
            }
        }))
}

async fn forward(State(peer): State<Arc<Peer>>, req: Request) -> Result<Response, StatusCode> {
    let target = req.uri().path_and_query().ok_or(StatusCode::BAD_REQUEST)?.clone();
    let (parts, body) = req.into_parts();
    let mut outgoing = Request::new(body);
    *outgoing.uri_mut() = Uri::builder().path_and_query(target).build().map_err(|_| StatusCode::BAD_REQUEST)?;
    *outgoing.method_mut() = parts.method;
    *outgoing.version_mut() = Version::HTTP_11;
    for name in ["host", "origin", "sec-fetch-site", "content-type", "content-length", "content-encoding",
        "x-candidate-action", "x-game-request", "x-project-profile", "x-project-id", "x-project-name"] {
        let mut values = parts.headers.get_all(name).iter();
        if let Some(value) = values.next() {
            if values.next().is_some() { return Err(StatusCode::BAD_REQUEST); }
            outgoing.headers_mut().insert(name, value.clone());
        }
    }
    if outgoing.method() == Method::POST && (!outgoing.headers().contains_key(header::CONTENT_LENGTH) || parts.headers.contains_key(header::TRANSFER_ENCODING)) {
        return Err(StatusCode::LENGTH_REQUIRED);
    }
    let socket = timeout(Duration::from_secs(3), UnixStream::connect(&peer.socket)).await
        .map_err(|_| StatusCode::GATEWAY_TIMEOUT)?.map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    let (mut sender, conn) = http1::handshake(TokioIo::new(socket)).await.map_err(|_| StatusCode::BAD_GATEWAY)?;
    tokio::spawn(async move { if let Err(error) = conn.await { tracing::debug!(%error, "build connection ended"); } });
    let response = timeout(Duration::from_secs(135), sender.send_request(outgoing)).await
        .map_err(|_| StatusCode::GATEWAY_TIMEOUT)?.map_err(|_| StatusCode::BAD_GATEWAY)?;
    let (mut parts, body) = response.into_parts();
    parts.headers.remove(header::CONNECTION);
    parts.headers.remove(header::TRANSFER_ENCODING);
    parts.headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    Ok(Response::from_parts(parts, Body::new(body)))
}

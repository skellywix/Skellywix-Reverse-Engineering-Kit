use std::path::{Component, Path, PathBuf};

use axum::body::Body;
use axum::extract::{Path as AxumPath, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};

use crate::api::AppState;

pub(crate) async fn index(State(state): State<AppState>) -> Response {
    serve_file(
        &state.config.static_dir.join("index.html"),
        CachePolicy::Html,
    )
    .await
}

pub(crate) async fn asset(
    State(state): State<AppState>,
    AxumPath(path): AxumPath<String>,
) -> Response {
    let Some(relative_path) = safe_asset_path(&path) else {
        return not_found().await;
    };
    serve_file(
        &state.config.static_dir.join("assets").join(relative_path),
        CachePolicy::Immutable,
    )
    .await
}

pub(crate) async fn not_found() -> Response {
    (StatusCode::NOT_FOUND, "not found").into_response()
}

#[derive(Clone, Copy)]
enum CachePolicy {
    Html,
    Immutable,
}

async fn serve_file(path: &Path, cache_policy: CachePolicy) -> Response {
    match tokio::fs::read(path).await {
        Ok(bytes) => {
            let content_type = content_type(path);
            let mut response = Body::from(bytes).into_response();
            response
                .headers_mut()
                .insert(header::CONTENT_TYPE, content_type);
            let cache_control = match cache_policy {
                CachePolicy::Html => HeaderValue::from_static("no-cache"),
                CachePolicy::Immutable => {
                    HeaderValue::from_static("public, max-age=31536000, immutable")
                }
            };
            response
                .headers_mut()
                .insert(header::CACHE_CONTROL, cache_control);
            response
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => not_found().await,
        Err(error) => {
            tracing::error!(error = %error, path = %path.display(), "failed to read static asset");
            (StatusCode::INTERNAL_SERVER_ERROR, "internal server error").into_response()
        }
    }
}

fn safe_asset_path(path: &str) -> Option<PathBuf> {
    if path.is_empty() || path.contains('\\') {
        return None;
    }
    let path = Path::new(path);
    if path
        .components()
        .all(|component| matches!(component, Component::Normal(_)))
    {
        Some(path.to_path_buf())
    } else {
        None
    }
}

fn content_type(path: &Path) -> HeaderValue {
    match path.extension().and_then(|extension| extension.to_str()) {
        Some("css") => HeaderValue::from_static("text/css; charset=utf-8"),
        Some("html") => HeaderValue::from_static("text/html; charset=utf-8"),
        Some("js" | "mjs") => HeaderValue::from_static("text/javascript; charset=utf-8"),
        Some("json" | "map") => HeaderValue::from_static("application/json"),
        Some("svg") => HeaderValue::from_static("image/svg+xml"),
        Some("png") => HeaderValue::from_static("image/png"),
        Some("jpg" | "jpeg") => HeaderValue::from_static("image/jpeg"),
        Some("webp") => HeaderValue::from_static("image/webp"),
        Some("woff") => HeaderValue::from_static("font/woff"),
        Some("woff2") => HeaderValue::from_static("font/woff2"),
        _ => HeaderValue::from_static("application/octet-stream"),
    }
}

pub fn router() -> axum::Router<crate::api::AppState> {
    axum::Router::new()
}

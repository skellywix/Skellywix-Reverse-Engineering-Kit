//! Read-only observations of fixed offline verifier recipes.
use axum::{Extension, Json, Router, middleware, routing::get};
use serde::Serialize;

use super::convert::{configured, no_store};
use crate::{
    api::AppState,
    artifacts::Sha256Id,
    jobs::{PROJECTS, PortResult, SourcePortRoot},
};

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/state", get(state))
        .layer(middleware::from_fn(no_store))
}

#[derive(Serialize)]
struct Runtime {
    schema_version: u8,
    recipes: Vec<Recipe>,
}
#[derive(Serialize)]
struct Recipe {
    project_id: String,
    name: String,
    recipe_sha256: Sha256Id,
    toolchain_sha256: Sha256Id,
    state: &'static str,
    detail: &'static str,
}

async fn state(root: Option<Extension<SourcePortRoot>>) -> PortResult<Json<Runtime>> {
    let root = configured(root)?;
    root.registry_ready().await?;
    let mut recipes = Vec::with_capacity(PROJECTS.len());
    for id in PROJECTS {
        let Some(installed) = root.installed(id).await? else {
            continue;
        };
        let p = installed.profile;
        let (state, detail) = root.recipe_observation(&p).await.unwrap_or((
            "unavailable",
            "The protected verifier observation could not be validated.",
        ));
        recipes.push(Recipe {
            project_id: p.id,
            name: p.name,
            recipe_sha256: p.recipe_sha256,
            toolchain_sha256: p.toolchain_sha256,
            state,
            detail,
        });
    }
    Ok(Json(Runtime {
        schema_version: 1,
        recipes,
    }))
}

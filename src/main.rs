#[cfg(unix)]
mod build_front;

use std::env;
use std::error::Error;
use std::ffi::OsString;
use std::net::{AddrParseError, SocketAddr};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

#[cfg(unix)]
use re_kit_workspace::adapters::{AdapterLimits, FourmilabAdapter, InspectorAdapter};
use re_kit_workspace::api::{AppConfig, AppState, router};
use re_kit_workspace::jobs::SourcePortRoot;
use re_kit_workspace::store::Store;
#[cfg(unix)]
use re_kit_workspace::workflows::{inspect, reconstruct};
use thiserror::Error;
use tokio::sync::{oneshot, watch};

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error + Send + Sync>> {
    tracing_subscriber::fmt().try_init()?;
    let config = RuntimeConfig::load()?;
    validate_static_dir(&config.static_dir).await?;
    let source_root = match env::var_os("RE_KIT_SOURCE_PORT_ROOT") {
        Some(path) => {
            Some(SourcePortRoot::outside_static(Path::new(&path), &config.static_dir).await?)
        }
        None => None,
    };
    let store = Store::open(&config.database_path).await?;
    let reconciled = store.reconcile_restart().await?;
    if reconciled > 0 {
        tracing::warn!(
            jobs = reconciled,
            "interrupted uncertain jobs after restart"
        );
    }
    let (shutdown_sender, shutdown_receiver) = watch::channel(false);
    let app_config = Arc::new(AppConfig {
        static_dir: config.static_dir,
        allow_missing_origin: config.allow_missing_origin,
    });
    tracing::info!(
        unix_socket_support = cfg!(unix),
        fourmilab_socket_set = config.fourmilab_socket.is_some(),
        inspector_desk_socket_set = config.inspector_socket.is_some(),
        inspector_agent_socket_set = config.inspector_agent_socket.is_some(),
        connect_timeout_ms = config.engine_connect_timeout.as_millis(),
        read_timeout_ms = config.engine_read_timeout.as_millis(),
        poll_interval_ms = config.engine_poll_interval.as_millis(),
        "legacy adapter settings"
    );
    #[cfg(unix)]
    let adapter_limits = AdapterLimits {
        connect_timeout: config.engine_connect_timeout,
        read_timeout: config.engine_read_timeout,
        ..AdapterLimits::default()
    };
    #[cfg(unix)]
    let mut pollers = Vec::with_capacity(2);
    #[cfg(unix)]
    if let Some(socket) = config.fourmilab_socket.as_ref() {
        pollers.push(tokio::spawn(reconstruct::poll(
            FourmilabAdapter::with_limits(socket, adapter_limits),
            store.clone(),
            config.engine_poll_interval,
            shutdown_sender.subscribe(),
        )));
    } else {
        reconstruct::record_unavailable(&store).await;
    }
    #[cfg(unix)]
    if let (Some(desk_socket), Some(agent_socket)) = (
        config.inspector_socket.as_ref(),
        config.inspector_agent_socket.as_ref(),
    ) {
        pollers.push(tokio::spawn(inspect::poll(
            InspectorAdapter::with_limits(desk_socket, agent_socket, adapter_limits),
            store.clone(),
            config.engine_poll_interval,
            shutdown_sender.subscribe(),
        )));
    } else {
        inspect::record_unavailable(&store).await;
    }
    #[cfg(unix)]
    let state = AppState::with_engine_configuration(
        store,
        app_config,
        shutdown_receiver,
        config.fourmilab_socket,
        config.inspector_socket,
        config.inspector_agent_socket,
        adapter_limits,
    );
    #[cfg(not(unix))]
    let state = AppState::with_engine_sockets(
        store,
        app_config,
        shutdown_receiver,
        config.fourmilab_socket,
        config.inspector_socket,
    );
    tracing::info!(address = %config.listen, "RE Kit workspace listening");
    let (signal_error_sender, mut signal_error_receiver) = oneshot::channel();
    let graceful_shutdown = async move {
        let result = shutdown_signal().await;
        let _ = shutdown_sender.send(true);
        if let Err(error) = result {
            let _ = signal_error_sender.send(error);
        }
    };
    let mut app = router(state);
    if let Some(root) = source_root {
        app = app.layer(axum::Extension(root));
    }
    #[cfg(unix)]
    {
        let mount_build = |app, authority| match env::var_os("RE_KIT_BUILD_SOCKET") {
            Some(socket) => build_front::with_build(app, PathBuf::from(socket), authority),
            None => app,
        };
        if let Some(socket) = env::var_os("RE_KIT_WEB_SOCKET") {
            if !config.listen.ip().is_loopback() || config.listen.port() == 0 {
                return Err("The public authority for a private web socket must be loopback with a fixed port.".into());
            }
            let listener = tokio::net::UnixListener::bind(socket)?;
            axum::serve(listener, mount_build(app, config.listen))
                .with_graceful_shutdown(graceful_shutdown)
                .await?;
        } else {
            let listener = tokio::net::TcpListener::bind(config.listen).await?;
            let authority = listener.local_addr()?;
            axum::serve(listener, mount_build(app, authority))
                .with_graceful_shutdown(graceful_shutdown)
                .await?;
        }
    }
    #[cfg(not(unix))]
    axum::serve(tokio::net::TcpListener::bind(config.listen).await?, app)
        .with_graceful_shutdown(graceful_shutdown)
        .await?;
    #[cfg(unix)]
    for poller in pollers {
        poller.await?;
    }
    if let Ok(error) = signal_error_receiver.try_recv() {
        return Err(error.into());
    }
    Ok(())
}

#[derive(Debug)]
struct RuntimeConfig {
    listen: SocketAddr,
    database_path: PathBuf,
    static_dir: PathBuf,
    allow_missing_origin: bool,
    fourmilab_socket: Option<PathBuf>,
    inspector_socket: Option<PathBuf>,
    inspector_agent_socket: Option<PathBuf>,
    engine_connect_timeout: Duration,
    engine_read_timeout: Duration,
    engine_poll_interval: Duration,
}

impl RuntimeConfig {
    fn load() -> Result<Self, ConfigError> {
        Self::load_from(env::args_os().skip(1), env::var_os)
    }

    fn load_from(
        arguments: impl IntoIterator<Item = OsString>,
        mut environment: impl FnMut(&'static str) -> Option<OsString>,
    ) -> Result<Self, ConfigError> {
        let mut listen = None;
        let mut database_path = None;
        let mut static_dir = None;
        let mut allow_missing_origin = false;
        let mut fourmilab_socket = None;
        let mut inspector_socket = None;
        let mut inspector_agent_socket = None;
        let mut engine_connect_timeout = None;
        let mut engine_read_timeout = None;
        let mut engine_poll_interval = None;
        let mut arguments = arguments.into_iter();

        while let Some(argument) = arguments.next() {
            let argument = argument
                .into_string()
                .map_err(|_| ConfigError::NonUnicodeArgument)?;
            match argument.as_str() {
                "--listen" => {
                    listen = Some(argument_string(next_argument(&mut arguments, "--listen")?)?);
                }
                "--database" | "--database-path" => {
                    database_path =
                        Some(PathBuf::from(next_argument(&mut arguments, "--database")?));
                }
                "--static-dir" => {
                    static_dir = Some(PathBuf::from(next_argument(
                        &mut arguments,
                        "--static-dir",
                    )?));
                }
                "--fourmilab-socket" => {
                    fourmilab_socket = Some(PathBuf::from(next_argument(
                        &mut arguments,
                        "--fourmilab-socket",
                    )?));
                }
                "--inspector-desk-socket" => {
                    inspector_socket = Some(PathBuf::from(next_argument(
                        &mut arguments,
                        "--inspector-desk-socket",
                    )?));
                }
                "--inspector-agent-socket" => {
                    inspector_agent_socket = Some(PathBuf::from(next_argument(
                        &mut arguments,
                        "--inspector-agent-socket",
                    )?));
                }
                "--legacy-connect-timeout-ms" => {
                    engine_connect_timeout = Some(argument_duration(
                        next_argument(&mut arguments, "--legacy-connect-timeout-ms")?,
                        "--legacy-connect-timeout-ms",
                    )?);
                }
                "--legacy-read-timeout-ms" => {
                    engine_read_timeout = Some(argument_duration(
                        next_argument(&mut arguments, "--legacy-read-timeout-ms")?,
                        "--legacy-read-timeout-ms",
                    )?);
                }
                "--legacy-poll-interval-ms" => {
                    engine_poll_interval = Some(argument_duration(
                        next_argument(&mut arguments, "--legacy-poll-interval-ms")?,
                        "--legacy-poll-interval-ms",
                    )?);
                }
                "--allow-missing-origin" => allow_missing_origin = true,
                _ => return Err(ConfigError::UnknownArgument(argument)),
            }
        }

        let listen = listen
            .or(optional_unicode_env(
                "RE_KIT_LISTEN_ADDR",
                &mut environment,
            )?)
            .unwrap_or_else(|| "127.0.0.1:8787".to_owned())
            .parse()
            .map_err(ConfigError::InvalidListen)?;
        let database_path = database_path
            .or_else(|| environment("RE_KIT_DATABASE_PATH").map(PathBuf::from))
            .ok_or(ConfigError::MissingSetting(
                "RE_KIT_DATABASE_PATH or --database",
            ))?;
        let static_dir = static_dir
            .or_else(|| environment("RE_KIT_STATIC_DIR").map(PathBuf::from))
            .ok_or(ConfigError::MissingSetting(
                "RE_KIT_STATIC_DIR or --static-dir",
            ))?;
        let fourmilab_socket =
            fourmilab_socket.or_else(|| environment("RE_KIT_FOURMILAB_SOCKET").map(PathBuf::from));
        let inspector_socket = inspector_socket
            .or_else(|| environment("RE_KIT_INSPECTOR_DESK_SOCKET").map(PathBuf::from));
        let inspector_agent_socket = inspector_agent_socket
            .or_else(|| environment("RE_KIT_INSPECTOR_AGENT_SOCKET").map(PathBuf::from));
        let engine_connect_timeout = engine_connect_timeout
            .or(optional_duration_env(
                "RE_KIT_LEGACY_CONNECT_TIMEOUT_MS",
                &mut environment,
            )?)
            .unwrap_or_else(|| Duration::from_millis(2_000));
        let engine_read_timeout = engine_read_timeout
            .or(optional_duration_env(
                "RE_KIT_LEGACY_READ_TIMEOUT_MS",
                &mut environment,
            )?)
            .unwrap_or_else(|| Duration::from_millis(30_000));
        let engine_poll_interval = engine_poll_interval
            .or(optional_duration_env(
                "RE_KIT_LEGACY_POLL_INTERVAL_MS",
                &mut environment,
            )?)
            .unwrap_or_else(|| Duration::from_millis(2_200));
        if !allow_missing_origin {
            allow_missing_origin =
                optional_bool_env("RE_KIT_ALLOW_MISSING_ORIGIN", &mut environment)?
                    .unwrap_or(false);
        }
        Ok(Self {
            listen,
            database_path,
            static_dir,
            allow_missing_origin,
            fourmilab_socket,
            inspector_socket,
            inspector_agent_socket,
            engine_connect_timeout,
            engine_read_timeout,
            engine_poll_interval,
        })
    }
}

#[derive(Debug, Error)]
enum ConfigError {
    #[error("required setting is missing: {0}")]
    MissingSetting(&'static str),
    #[error("argument {0} requires a value")]
    MissingArgumentValue(&'static str),
    #[error("command-line argument is not valid Unicode")]
    NonUnicodeArgument,
    #[error("environment variable {0} is not valid Unicode")]
    NonUnicodeEnvironment(&'static str),
    #[error("unknown argument: {0}")]
    UnknownArgument(String),
    #[error("listen address is invalid")]
    InvalidListen(#[source] AddrParseError),
    #[error("environment variable {0} must be true, false, 1, or 0")]
    InvalidBoolean(&'static str),
    #[error("{setting} must be a positive integer number of milliseconds, got {value}")]
    InvalidMilliseconds {
        setting: &'static str,
        value: String,
    },
    #[error("static directory is unavailable: {path}")]
    StaticDirectory {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("static directory setting is not a directory: {0}")]
    StaticPathIsNotDirectory(PathBuf),
    #[error("static directory has no index.html: {0}")]
    MissingIndex(PathBuf),
}

fn next_argument(
    arguments: &mut impl Iterator<Item = OsString>,
    flag: &'static str,
) -> Result<OsString, ConfigError> {
    arguments
        .next()
        .ok_or(ConfigError::MissingArgumentValue(flag))
}

fn argument_string(value: OsString) -> Result<String, ConfigError> {
    value
        .into_string()
        .map_err(|_| ConfigError::NonUnicodeArgument)
}

fn optional_unicode_env(
    name: &'static str,
    environment: &mut impl FnMut(&'static str) -> Option<OsString>,
) -> Result<Option<String>, ConfigError> {
    environment(name)
        .map(|value| {
            value
                .into_string()
                .map_err(|_| ConfigError::NonUnicodeEnvironment(name))
        })
        .transpose()
}

fn optional_bool_env(
    name: &'static str,
    environment: &mut impl FnMut(&'static str) -> Option<OsString>,
) -> Result<Option<bool>, ConfigError> {
    optional_unicode_env(name, environment)?
        .map(|value| match value.to_ascii_lowercase().as_str() {
            "true" | "1" => Ok(true),
            "false" | "0" => Ok(false),
            _ => Err(ConfigError::InvalidBoolean(name)),
        })
        .transpose()
}

fn argument_duration(value: OsString, setting: &'static str) -> Result<Duration, ConfigError> {
    parse_duration(argument_string(value)?, setting)
}

fn optional_duration_env(
    name: &'static str,
    environment: &mut impl FnMut(&'static str) -> Option<OsString>,
) -> Result<Option<Duration>, ConfigError> {
    optional_unicode_env(name, environment)?
        .map(|value| parse_duration(value, name))
        .transpose()
}

fn parse_duration(value: String, setting: &'static str) -> Result<Duration, ConfigError> {
    let milliseconds = value
        .parse::<u64>()
        .ok()
        .filter(|milliseconds| *milliseconds > 0)
        .ok_or_else(|| ConfigError::InvalidMilliseconds {
            setting,
            value: value.clone(),
        })?;
    Ok(Duration::from_millis(milliseconds))
}

async fn validate_static_dir(path: &Path) -> Result<(), ConfigError> {
    let metadata =
        tokio::fs::metadata(path)
            .await
            .map_err(|source| ConfigError::StaticDirectory {
                path: path.to_path_buf(),
                source,
            })?;
    if !metadata.is_dir() {
        return Err(ConfigError::StaticPathIsNotDirectory(path.to_path_buf()));
    }
    let index_path = path.join("index.html");
    let index_metadata = tokio::fs::metadata(&index_path).await.map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            ConfigError::MissingIndex(index_path.clone())
        } else {
            ConfigError::StaticDirectory {
                path: index_path.clone(),
                source: error,
            }
        }
    })?;
    if !index_metadata.is_file() {
        return Err(ConfigError::MissingIndex(index_path));
    }
    Ok(())
}

async fn shutdown_signal() -> std::io::Result<()> {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        tokio::select! {
            result = tokio::signal::ctrl_c() => result?,
            received = terminate.recv() => {
                received.ok_or_else(|| {
                    std::io::Error::new(
                        std::io::ErrorKind::BrokenPipe,
                        "SIGTERM signal handler closed",
                    )
                })?;
            }
        }
    }
    #[cfg(not(unix))]
    tokio::signal::ctrl_c().await?;

    tracing::info!("shutdown signal received");
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;

    fn required_environment() -> HashMap<&'static str, OsString> {
        HashMap::from([
            ("RE_KIT_DATABASE_PATH", OsString::from("workspace.sqlite3")),
            ("RE_KIT_STATIC_DIR", OsString::from("static")),
        ])
    }

    #[test]
    fn accepts_the_documented_fourmilab_socket_flag() -> Result<(), ConfigError> {
        let environment = required_environment();
        let config = RuntimeConfig::load_from(
            [
                OsString::from("--fourmilab-socket"),
                OsString::from("/run/re-kit/fourmilab.sock"),
            ],
            |name| environment.get(name).cloned(),
        )?;

        assert_eq!(
            config.fourmilab_socket,
            Some(PathBuf::from("/run/re-kit/fourmilab.sock"))
        );
        Ok(())
    }

    #[test]
    fn reads_the_documented_socket_environment_variables() -> Result<(), ConfigError> {
        let mut environment = required_environment();
        environment.extend([
            (
                "RE_KIT_FOURMILAB_SOCKET",
                OsString::from("/run/re-kit/fourmilab.sock"),
            ),
            (
                "RE_KIT_INSPECTOR_DESK_SOCKET",
                OsString::from("/run/re-kit/inspector-desk.sock"),
            ),
            (
                "RE_KIT_INSPECTOR_AGENT_SOCKET",
                OsString::from("/run/re-kit/inspector-agent.sock"),
            ),
        ]);
        let config = RuntimeConfig::load_from([], |name| environment.get(name).cloned())?;

        assert_eq!(
            config.fourmilab_socket,
            Some(PathBuf::from("/run/re-kit/fourmilab.sock"))
        );
        assert_eq!(
            config.inspector_socket,
            Some(PathBuf::from("/run/re-kit/inspector-desk.sock"))
        );
        assert_eq!(
            config.inspector_agent_socket,
            Some(PathBuf::from("/run/re-kit/inspector-agent.sock"))
        );
        Ok(())
    }

    #[test]
    fn uses_the_documented_legacy_duration_defaults() -> Result<(), ConfigError> {
        let environment = required_environment();
        let config = RuntimeConfig::load_from([], |name| environment.get(name).cloned())?;

        assert_eq!(config.engine_poll_interval, Duration::from_millis(2_200));
        assert_eq!(config.engine_connect_timeout, Duration::from_millis(2_000));
        assert_eq!(config.engine_read_timeout, Duration::from_millis(30_000));
        Ok(())
    }
}

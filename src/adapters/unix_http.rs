//! Minimal bounded HTTP/1.1 transport over a private Unix stream.

use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::task::{Context, Poll};

use futures::Stream;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio::time::{Instant, timeout, timeout_at};
use tokio_stream::wrappers::ReceiverStream;

use super::{AdapterError, AdapterLimits};

const SOCKET_READ_BYTES: usize = 8 * 1024;
const MAX_RESPONSE_HEADERS: usize = 128;

#[derive(Clone, Debug)]
pub(super) struct UnixHttp {
    socket_path: PathBuf,
    authority: &'static str,
    limits: AdapterLimits,
}

impl UnixHttp {
    pub(super) fn new(
        socket_path: impl AsRef<Path>,
        authority: &'static str,
        limits: AdapterLimits,
    ) -> Self {
        Self {
            socket_path: socket_path.as_ref().to_owned(),
            authority,
            limits,
        }
    }

    pub(super) async fn request(
        &self,
        method: &str,
        target: &str,
        headers: &[(&str, &str)],
        body: Option<&[u8]>,
        max_response_body: usize,
    ) -> Result<PendingResponse, AdapterError> {
        let content_length = body
            .map(|value| u64::try_from(value.len()).map_err(|_| AdapterError::TooLarge))
            .transpose()?;
        let (mut stream, deadline) = self
            .begin_request(method, target, headers, content_length)
            .await?;
        if let Some(body) = body {
            write_all_until(&mut stream, body, deadline).await?;
        }
        self.read_response(stream, deadline, max_response_body)
            .await
    }

    pub(super) async fn request_from<R>(
        &self,
        method: &str,
        target: &str,
        headers: &[(&str, &str)],
        content_length: u64,
        mut body: R,
        max_response_body: usize,
    ) -> Result<PendingResponse, AdapterError>
    where
        R: AsyncRead + Unpin + Send,
    {
        let (mut stream, deadline) = self
            .begin_request(method, target, headers, Some(content_length))
            .await?;
        let mut remaining = content_length;
        let mut buffer = vec![0_u8; self.limits.stream_chunk_bytes.max(1)];
        while remaining > 0 {
            let wanted = usize::try_from(remaining)
                .unwrap_or(usize::MAX)
                .min(buffer.len());
            let read = read_from_until(&mut body, &mut buffer[..wanted], deadline).await?;
            if read == 0 {
                return Err(AdapterError::Protocol(
                    "request body ended before its declared length".to_owned(),
                ));
            }
            write_all_until(&mut stream, &buffer[..read], deadline).await?;
            remaining -= u64::try_from(read).map_err(|_| AdapterError::TooLarge)?;
        }
        self.read_response(stream, deadline, max_response_body)
            .await
    }

    async fn begin_request(
        &self,
        method: &str,
        target: &str,
        headers: &[(&str, &str)],
        content_length: Option<u64>,
    ) -> Result<(UnixStream, Instant), AdapterError> {
        let mut stream = match timeout(
            self.limits.connect_timeout,
            UnixStream::connect(&self.socket_path),
        )
        .await
        {
            Err(_) => return Err(AdapterError::Timeout),
            Ok(Err(_)) => return Err(AdapterError::Unreachable),
            Ok(Ok(stream)) => stream,
        };
        let deadline = Instant::now()
            .checked_add(self.limits.read_timeout)
            .ok_or_else(|| {
                AdapterError::Protocol("configured I/O timeout was invalid".to_owned())
            })?;
        let head = build_request_head(
            method,
            target,
            self.authority,
            headers,
            content_length,
            self.limits.max_header_bytes,
        )?;
        write_all_until(&mut stream, &head, deadline).await?;
        Ok((stream, deadline))
    }

    async fn read_response(
        &self,
        mut stream: UnixStream,
        deadline: Instant,
        max_response_body: usize,
    ) -> Result<PendingResponse, AdapterError> {
        let mut bytes =
            Vec::with_capacity(self.limits.max_header_bytes.clamp(1, SOCKET_READ_BYTES));
        let header_end = loop {
            if let Some(end) = find_header_end(&bytes) {
                if end > self.limits.max_header_bytes {
                    return Err(AdapterError::TooLarge);
                }
                break end;
            }
            if bytes.len() >= self.limits.max_header_bytes {
                return Err(AdapterError::TooLarge);
            }
            let read =
                read_more_until(&mut stream, &mut bytes, SOCKET_READ_BYTES, deadline).await?;
            if read == 0 {
                return Err(AdapterError::Protocol(
                    "response ended before its headers".to_owned(),
                ));
            }
        };
        let (status, headers, framing) =
            parse_response_head(&bytes[..header_end], max_response_body)?;
        let prefix_len = bytes.len() - header_end;
        bytes.copy_within(header_end.., 0);
        bytes.truncate(prefix_len);
        let buffer = BodyBuffer {
            stream,
            bytes,
            start: 0,
            deadline,
            max_line_bytes: self.limits.max_header_bytes,
        };
        Ok(PendingResponse {
            status,
            headers,
            framing,
            buffer,
            max_body_bytes: max_response_body,
            stream_chunk_bytes: self.limits.stream_chunk_bytes.max(1),
        })
    }
}

/// A decoded response body that preserves transport errors until the consumer reads them.
pub struct BodyStream {
    receiver: ReceiverStream<Result<Vec<u8>, AdapterError>>,
    task: JoinHandle<()>,
}

impl Stream for BodyStream {
    type Item = Result<Vec<u8>, AdapterError>;

    fn poll_next(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        Pin::new(&mut self.receiver).poll_next(context)
    }
}

impl Drop for BodyStream {
    fn drop(&mut self) {
        self.task.abort();
    }
}

pub(super) struct PendingResponse {
    status: u16,
    headers: Vec<(String, String)>,
    framing: BodyFraming,
    buffer: BodyBuffer,
    max_body_bytes: usize,
    stream_chunk_bytes: usize,
}

impl PendingResponse {
    pub(super) fn status(&self) -> u16 {
        self.status
    }

    pub(super) fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(candidate, _)| candidate.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }

    pub(super) async fn collect(self) -> Result<Vec<u8>, AdapterError> {
        match self.framing {
            BodyFraming::Fixed(length) => self.buffer.collect_fixed(length).await,
            BodyFraming::Chunked => collect_chunked(self.buffer, self.max_body_bytes).await,
            BodyFraming::UntilEof => {
                self.buffer
                    .collect_until_eof(self.max_body_bytes, self.stream_chunk_bytes)
                    .await
            }
        }
    }

    pub(super) fn into_body_stream(self) -> BodyStream {
        let (sender, receiver) = mpsc::channel(2);
        let task = tokio::spawn(async move {
            let result = match self.framing {
                BodyFraming::Fixed(length) => {
                    stream_fixed(self.buffer, length, self.stream_chunk_bytes, &sender).await
                }
                BodyFraming::Chunked => {
                    stream_chunked(
                        self.buffer,
                        self.max_body_bytes,
                        self.stream_chunk_bytes,
                        &sender,
                    )
                    .await
                }
                BodyFraming::UntilEof => {
                    stream_until_eof(
                        self.buffer,
                        self.max_body_bytes,
                        self.stream_chunk_bytes,
                        &sender,
                    )
                    .await
                }
            };
            if let Err(error) = result
                && sender.send(Err(error)).await.is_err()
            {
                // The caller dropped the stream, so no observer remains for the terminal error.
            }
        });
        BodyStream {
            receiver: ReceiverStream::new(receiver),
            task,
        }
    }
}

#[derive(Clone, Copy)]
enum BodyFraming {
    Fixed(usize),
    Chunked,
    UntilEof,
}
type ParsedResponseHead = (u16, Vec<(String, String)>, BodyFraming);

struct BodyBuffer {
    stream: UnixStream,
    bytes: Vec<u8>,
    start: usize,
    deadline: Instant,
    max_line_bytes: usize,
}

impl BodyBuffer {
    fn available(&self) -> usize {
        self.bytes.len() - self.start
    }

    fn take_buffered(&mut self) -> Vec<u8> {
        if self.start == 0 {
            return std::mem::take(&mut self.bytes);
        }
        let remaining = self.bytes[self.start..].to_vec();
        self.bytes.clear();
        self.start = 0;
        remaining
    }

    fn consume(&mut self, count: usize) {
        self.start += count;
        if self.start == self.bytes.len() {
            self.bytes.clear();
            self.start = 0;
        } else if self.start >= SOCKET_READ_BYTES && self.start >= self.bytes.len() / 2 {
            let remaining = self.available();
            self.bytes.copy_within(self.start.., 0);
            self.bytes.truncate(remaining);
            self.start = 0;
        }
    }

    async fn read_line(&mut self) -> Result<Vec<u8>, AdapterError> {
        loop {
            if let Some(offset) = self.bytes[self.start..]
                .windows(2)
                .position(|window| window == b"\r\n")
            {
                let end = self.start + offset;
                let line = self.bytes[self.start..end].to_vec();
                self.consume(offset + 2);
                return Ok(line);
            }
            if self.available() >= self.max_line_bytes {
                return Err(AdapterError::TooLarge);
            }
            let allowance = self.max_line_bytes - self.available();
            let read = read_more_until(
                &mut self.stream,
                &mut self.bytes,
                allowance.clamp(1, SOCKET_READ_BYTES),
                self.deadline,
            )
            .await?;
            if read == 0 {
                return Err(AdapterError::Protocol(
                    "chunked response ended inside a line".to_owned(),
                ));
            }
        }
    }

    async fn fill_exact(&mut self, destination: &mut [u8]) -> Result<(), AdapterError> {
        let buffered = destination.len().min(self.available());
        destination[..buffered].copy_from_slice(&self.bytes[self.start..self.start + buffered]);
        self.consume(buffered);
        read_exact_until(
            &mut self.stream,
            &mut destination[buffered..],
            self.deadline,
        )
        .await
    }

    async fn append_exact(
        &mut self,
        destination: &mut Vec<u8>,
        count: usize,
    ) -> Result<(), AdapterError> {
        let start = destination.len();
        destination
            .try_reserve(count)
            .map_err(|_| AdapterError::TooLarge)?;
        destination.resize(start + count, 0);
        self.fill_exact(&mut destination[start..]).await
    }

    async fn collect_fixed(mut self, length: usize) -> Result<Vec<u8>, AdapterError> {
        if self.available() > length {
            return Err(AdapterError::Protocol(
                "response exceeded its Content-Length".to_owned(),
            ));
        }
        let mut body = self.take_buffered();
        let missing = length - body.len();
        self.append_exact(&mut body, missing).await?;
        Ok(body)
    }

    async fn collect_until_eof(
        mut self,
        limit: usize,
        read_size: usize,
    ) -> Result<Vec<u8>, AdapterError> {
        let mut body = self.take_buffered();
        if body.len() > limit {
            return Err(AdapterError::TooLarge);
        }
        loop {
            if body.len() == limit {
                let mut extra = [0_u8; 1];
                let read = read_some_until(&mut self.stream, &mut extra, self.deadline).await?;
                return if read == 0 {
                    Ok(body)
                } else {
                    Err(AdapterError::TooLarge)
                };
            }
            let wanted = read_size.min(limit - body.len()).max(1);
            let start = body.len();
            body.try_reserve(wanted)
                .map_err(|_| AdapterError::TooLarge)?;
            body.resize(start + wanted, 0);
            let read = read_some_until(&mut self.stream, &mut body[start..], self.deadline).await?;
            body.truncate(start + read);
            if read == 0 {
                return Ok(body);
            }
        }
    }
}

async fn collect_chunked(mut buffer: BodyBuffer, limit: usize) -> Result<Vec<u8>, AdapterError> {
    let mut body = Vec::new();
    loop {
        let size = read_chunk_size(&mut buffer).await?;
        if size == 0 {
            read_trailers(&mut buffer).await?;
            return Ok(body);
        }
        if body
            .len()
            .checked_add(size)
            .is_none_or(|total| total > limit)
        {
            return Err(AdapterError::TooLarge);
        }
        buffer.append_exact(&mut body, size).await?;
        require_chunk_terminator(&mut buffer).await?;
    }
}

async fn stream_fixed(
    mut buffer: BodyBuffer,
    length: usize,
    chunk_size: usize,
    sender: &mpsc::Sender<Result<Vec<u8>, AdapterError>>,
) -> Result<(), AdapterError> {
    if buffer.available() > length {
        return Err(AdapterError::Protocol(
            "response exceeded its Content-Length".to_owned(),
        ));
    }
    let initial = buffer.take_buffered();
    let mut remaining = length - initial.len();
    if !initial.is_empty() && !send_chunk(sender, initial).await {
        return Ok(());
    }
    while remaining > 0 {
        let wanted = remaining.min(chunk_size);
        let mut chunk = vec![0_u8; wanted];
        buffer.fill_exact(&mut chunk).await?;
        remaining -= wanted;
        if !send_chunk(sender, chunk).await {
            return Ok(());
        }
    }
    Ok(())
}

async fn stream_chunked(
    mut buffer: BodyBuffer,
    limit: usize,
    chunk_size: usize,
    sender: &mpsc::Sender<Result<Vec<u8>, AdapterError>>,
) -> Result<(), AdapterError> {
    let mut total = 0_usize;
    loop {
        let size = read_chunk_size(&mut buffer).await?;
        if size == 0 {
            return read_trailers(&mut buffer).await;
        }
        total = total
            .checked_add(size)
            .filter(|value| *value <= limit)
            .ok_or(AdapterError::TooLarge)?;
        let mut remaining = size;
        while remaining > 0 {
            let wanted = remaining.min(chunk_size);
            let mut chunk = vec![0_u8; wanted];
            buffer.fill_exact(&mut chunk).await?;
            remaining -= wanted;
            if !send_chunk(sender, chunk).await {
                return Ok(());
            }
        }
        require_chunk_terminator(&mut buffer).await?;
    }
}

async fn stream_until_eof(
    mut buffer: BodyBuffer,
    limit: usize,
    chunk_size: usize,
    sender: &mpsc::Sender<Result<Vec<u8>, AdapterError>>,
) -> Result<(), AdapterError> {
    let initial = buffer.take_buffered();
    let mut total = initial.len();
    if total > limit {
        return Err(AdapterError::TooLarge);
    }
    if !initial.is_empty() && !send_chunk(sender, initial).await {
        return Ok(());
    }
    loop {
        let wanted = if total == limit {
            1
        } else {
            chunk_size.min(limit - total).max(1)
        };
        let mut chunk = vec![0_u8; wanted];
        let read = read_some_until(&mut buffer.stream, &mut chunk, buffer.deadline).await?;
        if read == 0 {
            return Ok(());
        }
        if total == limit {
            return Err(AdapterError::TooLarge);
        }
        chunk.truncate(read);
        total += read;
        if !send_chunk(sender, chunk).await {
            return Ok(());
        }
    }
}

async fn send_chunk(sender: &mpsc::Sender<Result<Vec<u8>, AdapterError>>, chunk: Vec<u8>) -> bool {
    sender.send(Ok(chunk)).await.is_ok()
}

async fn read_chunk_size(buffer: &mut BodyBuffer) -> Result<usize, AdapterError> {
    let line = buffer.read_line().await?;
    let token = line
        .split(|byte| *byte == b';')
        .next()
        .filter(|token| !token.is_empty())
        .ok_or_else(|| AdapterError::Protocol("chunk size was missing".to_owned()))?;
    if !token.iter().all(u8::is_ascii_hexdigit) {
        return Err(AdapterError::Protocol(
            "chunk size was not hexadecimal".to_owned(),
        ));
    }
    let token = std::str::from_utf8(token)
        .map_err(|_| AdapterError::Protocol("chunk size was not ASCII".to_owned()))?;
    usize::from_str_radix(token, 16).map_err(|_| AdapterError::TooLarge)
}

async fn require_chunk_terminator(buffer: &mut BodyBuffer) -> Result<(), AdapterError> {
    let mut terminator = [0_u8; 2];
    buffer.fill_exact(&mut terminator).await?;
    if terminator == *b"\r\n" {
        Ok(())
    } else {
        Err(AdapterError::Protocol(
            "chunk data omitted its terminator".to_owned(),
        ))
    }
}

async fn read_trailers(buffer: &mut BodyBuffer) -> Result<(), AdapterError> {
    let mut total = 0_usize;
    loop {
        let line = buffer.read_line().await?;
        total = total
            .checked_add(line.len() + 2)
            .filter(|value| *value <= buffer.max_line_bytes)
            .ok_or(AdapterError::TooLarge)?;
        if line.is_empty() {
            return Ok(());
        }
        let Some(colon) = line.iter().position(|byte| *byte == b':') else {
            return Err(AdapterError::Protocol(
                "chunk trailer was not a header".to_owned(),
            ));
        };
        if !is_token(&line[..colon]) {
            return Err(AdapterError::Protocol(
                "chunk trailer name was invalid".to_owned(),
            ));
        }
    }
}

fn build_request_head(
    method: &str,
    target: &str,
    authority: &str,
    headers: &[(&str, &str)],
    content_length: Option<u64>,
    limit: usize,
) -> Result<Vec<u8>, AdapterError> {
    if method.is_empty() || !method.bytes().all(|byte| byte.is_ascii_uppercase()) {
        return Err(AdapterError::Protocol(
            "request method was invalid".to_owned(),
        ));
    }
    if !target.starts_with('/')
        || target.bytes().any(|byte| !(0x21..=0x7e).contains(&byte))
        || !valid_header_value(authority)
    {
        return Err(AdapterError::Protocol(
            "request target or authority was invalid".to_owned(),
        ));
    }
    for (index, (name, value)) in headers.iter().enumerate() {
        if !is_token(name.as_bytes())
            || !valid_header_value(value)
            || ["host", "connection", "content-length", "transfer-encoding"]
                .iter()
                .any(|reserved| name.eq_ignore_ascii_case(reserved))
            || headers[..index]
                .iter()
                .any(|(seen, _)| seen.eq_ignore_ascii_case(name))
        {
            return Err(AdapterError::Protocol(
                "request header was invalid or duplicated".to_owned(),
            ));
        }
    }
    let length_value = content_length.map(|value| value.to_string());
    let mut size = method
        .len()
        .checked_add(target.len())
        .and_then(|value| value.checked_add(authority.len()))
        .and_then(|value| value.checked_add(32))
        .ok_or(AdapterError::TooLarge)?;
    for (name, value) in headers {
        size = size
            .checked_add(name.len())
            .and_then(|total| total.checked_add(value.len()))
            .and_then(|total| total.checked_add(4))
            .ok_or(AdapterError::TooLarge)?;
    }
    if let Some(value) = &length_value {
        size = size
            .checked_add(value.len() + "Content-Length: \r\n".len())
            .ok_or(AdapterError::TooLarge)?;
    }
    if size > limit {
        return Err(AdapterError::TooLarge);
    }
    let mut head = Vec::with_capacity(size);
    head.extend_from_slice(method.as_bytes());
    head.push(b' ');
    head.extend_from_slice(target.as_bytes());
    head.extend_from_slice(b" HTTP/1.1\r\nHost: ");
    head.extend_from_slice(authority.as_bytes());
    head.extend_from_slice(b"\r\n");
    for (name, value) in headers {
        head.extend_from_slice(name.as_bytes());
        head.extend_from_slice(b": ");
        head.extend_from_slice(value.as_bytes());
        head.extend_from_slice(b"\r\n");
    }
    if let Some(value) = length_value {
        head.extend_from_slice(b"Content-Length: ");
        head.extend_from_slice(value.as_bytes());
        head.extend_from_slice(b"\r\n");
    }
    head.extend_from_slice(b"Connection: close\r\n\r\n");
    if head.len() > limit {
        return Err(AdapterError::TooLarge);
    }
    Ok(head)
}

fn parse_response_head(
    head: &[u8],
    max_body_bytes: usize,
) -> Result<ParsedResponseHead, AdapterError> {
    let head = head
        .strip_suffix(b"\r\n\r\n")
        .ok_or_else(|| AdapterError::Protocol("response headers were incomplete".to_owned()))?;
    let text = std::str::from_utf8(head)
        .map_err(|_| AdapterError::Protocol("response headers were not ASCII".to_owned()))?;
    let mut lines = text.split("\r\n");
    let status_line = lines
        .next()
        .ok_or_else(|| AdapterError::Protocol("response status was missing".to_owned()))?;
    let mut status_parts = status_line.splitn(3, ' ');
    let version = status_parts.next().unwrap_or_default();
    let status = status_parts
        .next()
        .filter(|value| value.len() == 3 && value.bytes().all(|byte| byte.is_ascii_digit()))
        .and_then(|value| value.parse::<u16>().ok())
        .filter(|value| (100..=599).contains(value))
        .ok_or_else(|| AdapterError::Protocol("response status was invalid".to_owned()))?;
    if !matches!(version, "HTTP/1.0" | "HTTP/1.1") {
        return Err(AdapterError::Protocol(
            "response HTTP version was unsupported".to_owned(),
        ));
    }
    let mut headers = Vec::new();
    for line in lines {
        if headers.len() == MAX_RESPONSE_HEADERS {
            return Err(AdapterError::TooLarge);
        }
        let (name, value) = line
            .split_once(':')
            .ok_or_else(|| AdapterError::Protocol("response header was malformed".to_owned()))?;
        if !is_token(name.as_bytes())
            || value
                .bytes()
                .any(|byte| byte != b'\t' && !(0x20..=0x7e).contains(&byte))
            || headers
                .iter()
                .any(|(seen, _): &(String, String)| seen.eq_ignore_ascii_case(name))
        {
            return Err(AdapterError::Protocol(
                "response header was invalid or duplicated".to_owned(),
            ));
        }
        headers.push((name.to_ascii_lowercase(), value.trim().to_owned()));
    }
    if (100..200).contains(&status) {
        return Err(AdapterError::Protocol(
            "informational responses are unsupported".to_owned(),
        ));
    }
    if matches!(status, 204 | 304) {
        return Ok((status, headers, BodyFraming::Fixed(0)));
    }
    let content_length = header_value(&headers, "content-length");
    let transfer_encoding = header_value(&headers, "transfer-encoding");
    if content_length.is_some() && transfer_encoding.is_some() {
        return Err(AdapterError::Protocol(
            "response supplied two body framings".to_owned(),
        ));
    }
    let framing = if let Some(value) = transfer_encoding {
        if value.eq_ignore_ascii_case("chunked") {
            BodyFraming::Chunked
        } else {
            return Err(AdapterError::Protocol(
                "response transfer encoding was unsupported".to_owned(),
            ));
        }
    } else if let Some(value) = content_length {
        if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(AdapterError::Protocol(
                "response Content-Length was invalid".to_owned(),
            ));
        }
        let length_u64 = value.parse::<u64>().map_err(|_| AdapterError::TooLarge)?;
        let length = usize::try_from(length_u64).map_err(|_| AdapterError::TooLarge)?;
        if length > max_body_bytes {
            return Err(AdapterError::TooLarge);
        }
        BodyFraming::Fixed(length)
    } else {
        BodyFraming::UntilEof
    };
    Ok((status, headers, framing))
}

fn header_value<'a>(headers: &'a [(String, String)], name: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|(candidate, _)| candidate.eq_ignore_ascii_case(name))
        .map(|(_, value)| value.as_str())
}

fn find_header_end(bytes: &[u8]) -> Option<usize> {
    bytes
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .map(|offset| offset + 4)
}

fn is_token(value: &[u8]) -> bool {
    !value.is_empty()
        && value.iter().all(|byte| {
            byte.is_ascii_alphanumeric()
                || matches!(
                    byte,
                    b'!' | b'#'
                        | b'$'
                        | b'%'
                        | b'&'
                        | b'\''
                        | b'*'
                        | b'+'
                        | b'-'
                        | b'.'
                        | b'^'
                        | b'_'
                        | b'`'
                        | b'|'
                        | b'~'
                )
        })
}

fn valid_header_value(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte == b'\t' || (0x20..=0x7e).contains(&byte))
}

async fn write_all_until(
    stream: &mut UnixStream,
    bytes: &[u8],
    deadline: Instant,
) -> Result<(), AdapterError> {
    match timeout_at(deadline, stream.write_all(bytes)).await {
        Err(_) => Err(AdapterError::Timeout),
        Ok(Err(_)) => Err(AdapterError::Protocol(
            "connection failed while writing the request".to_owned(),
        )),
        Ok(Ok(())) => Ok(()),
    }
}

async fn read_from_until<R>(
    reader: &mut R,
    bytes: &mut [u8],
    deadline: Instant,
) -> Result<usize, AdapterError>
where
    R: AsyncRead + Unpin,
{
    match timeout_at(deadline, reader.read(bytes)).await {
        Err(_) => Err(AdapterError::Timeout),
        Ok(Err(_)) => Err(AdapterError::Protocol(
            "request body stream failed".to_owned(),
        )),
        Ok(Ok(read)) => Ok(read),
    }
}

async fn read_some_until(
    stream: &mut UnixStream,
    bytes: &mut [u8],
    deadline: Instant,
) -> Result<usize, AdapterError> {
    match timeout_at(deadline, stream.read(bytes)).await {
        Err(_) => Err(AdapterError::Timeout),
        Ok(Err(_)) => Err(AdapterError::Protocol(
            "connection failed while reading the response".to_owned(),
        )),
        Ok(Ok(read)) => Ok(read),
    }
}

async fn read_exact_until(
    stream: &mut UnixStream,
    mut bytes: &mut [u8],
    deadline: Instant,
) -> Result<(), AdapterError> {
    while !bytes.is_empty() {
        let read = read_some_until(stream, bytes, deadline).await?;
        if read == 0 {
            return Err(AdapterError::Protocol(
                "response ended before its declared body length".to_owned(),
            ));
        }
        bytes = &mut bytes[read..];
    }
    Ok(())
}

async fn read_more_until(
    stream: &mut UnixStream,
    destination: &mut Vec<u8>,
    count: usize,
    deadline: Instant,
) -> Result<usize, AdapterError> {
    let start = destination.len();
    destination
        .try_reserve(count)
        .map_err(|_| AdapterError::TooLarge)?;
    destination.resize(start + count, 0);
    let read = read_some_until(stream, &mut destination[start..], deadline).await?;
    destination.truncate(start + read);
    Ok(read)
}

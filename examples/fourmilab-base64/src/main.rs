use std::env;
use std::ffi::{OsStr, OsString};
use std::fs::File;
use std::io::{self, Read, Write};
use std::process::ExitCode;

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const HELP: &[u8] = include_bytes!("../text/help.txt");
const VERSION: &[u8] = include_bytes!("../text/version.txt");

fn encode(input: &mut dyn Read, out: &mut dyn Write) -> io::Result<()> {
    // 54 input bytes make one 72-column MIME line; retain short reads.
    let mut block = [0u8; 54];
    let mut line = [b'='; 74];
    let mut used = 0;
    let mut emitted = false;
    loop {
        let eof = match input.read(&mut block[used..]) {
            Ok(0) => true,
            Ok(n) => {
                used += n;
                false
            }
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        };
        if used == block.len() || eof {
            if used != 0 || !emitted {
                let mut end = 0;
                for triple in block[..used].chunks(3) {
                    let a = triple[0];
                    let b = triple.get(1).copied().unwrap_or(0);
                    let c = triple.get(2).copied().unwrap_or(0);
                    line[end] = ALPHABET[(a >> 2) as usize];
                    line[end + 1] = ALPHABET[((a & 3) << 4 | b >> 4) as usize];
                    line[end + 2] = if triple.len() > 1 {
                        ALPHABET[((b & 15) << 2 | c >> 6) as usize]
                    } else {
                        b'='
                    };
                    line[end + 3] = if triple.len() > 2 {
                        ALPHABET[(c & 63) as usize]
                    } else {
                        b'='
                    };
                    end += 4;
                }
                line[end..end + 2].copy_from_slice(b"\r\n");
                out.write_all(&line[..end + 2])?;
                emitted = true;
            }
            used = 0;
        }
        if eof {
            return Ok(());
        }
    }
}

fn decode(
    input: &mut dyn Read,
    out: &mut dyn Write,
    err: &mut dyn Write,
    strict: bool,
) -> io::Result<u8> {
    // fread waits for a full block or EOF, even if padding arrived in a short read.
    let mut buf = [0; 256];
    let mut group = [0u8; 4];
    let mut used = 0;
    let mut third_pad = false;
    loop {
        let mut n = 0;
        while n < buf.len() {
            match input.read(&mut buf[n..]) {
                Ok(0) => break,
                Ok(count) => n += count,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(e),
            }
        }
        if n == 0 {
            if strict && used != 0 {
                err.write_all(b"Input file incomplete.\n")?;
                return Ok(1);
            }
            return Ok(0);
        }
        for &ch in &buf[..n] {
            group[used] = match ch {
                0..=32 => continue,
                b'A'..=b'Z' => ch - b'A',
                b'a'..=b'z' => ch - b'a' + 26,
                b'0'..=b'9' => ch - b'0' + 52,
                b'+' => 62,
                b'/' => 63,
                // Entry 255 is zero-initialized but never marked invalid in 1.5.
                b'=' | 255 => 0,
                _ if !strict => continue,
                _ => {
                    err.write_all(b"Illegal character '")?;
                    err.write_all(&[ch])?;
                    err.write_all(b"' in input file.\n")?;
                    return Ok(1);
                }
            };
            if used == 2 {
                third_pad = ch == b'=';
            }
            used += 1;
            if used == 4 {
                let [a, b, c, d] = group;
                let bytes = [a << 2 | b >> 4, b << 4 | c >> 2, c << 6 | d];
                let count = if third_pad {
                    1
                } else if ch == b'=' {
                    2
                } else {
                    3
                };
                out.write_all(&bytes[..count])?;
                if count < 3 {
                    return Ok(0);
                }
                used = 0;
            }
        }
    }
}

fn open_error(kind: &[u8], path: &OsStr, err: &mut dyn Write) -> io::Result<u8> {
    err.write_all(b"Cannot open ")?;
    err.write_all(kind)?;
    err.write_all(b" file ")?;
    err.write_all(path.as_encoded_bytes())?;
    err.write_all(b"\n")?;
    Ok(2)
}

fn run() -> io::Result<u8> {
    let mut args = env::args_os();
    let program = args.next().unwrap_or_default();
    let mut out = io::stdout().lock();
    let mut err = io::stderr().lock();
    let mut files: Vec<OsString> = Vec::new();
    let posix = env::var_os("POSIXLY_CORRECT").is_some();
    let (mut decoding, mut strict, mut options) = (false, true, true);
    while let Some(arg) = args.next() {
        let raw = arg.as_encoded_bytes();
        if !options || raw.len() < 2 || raw[0] != b'-' {
            files.push(arg);
            if posix {
                options = false;
            }
            continue;
        }
        if raw == b"--" {
            options = false;
            continue;
        }
        for (i, &c) in raw.iter().enumerate().skip(1) {
            let next;
            let key = if c == b'-' {
                let tail = if i + 1 < raw.len() {
                    &raw[i + 1..]
                } else if let Some(a) = args.next() {
                    next = a;
                    next.as_encoded_bytes()
                } else {
                    err.write_all(program.as_encoded_bytes())?;
                    err.write_all(b": option requires an argument -- '-'\n")?;
                    out.write_all(HELP)?;
                    return Ok(0);
                };
                match tail.first() {
                    Some(b'c') => {
                        out.write_all(b"This program is in the public domain.\n")?;
                        return Ok(0);
                    }
                    Some(b'v') => {
                        out.write_all(VERSION)?;
                        return Ok(0);
                    }
                    Some(b'h') => b'u',
                    Some(k @ (b'd' | b'e' | b'n')) => *k,
                    _ => break,
                }
            } else {
                c
            };
            match key {
                b'd' => decoding = true,
                b'e' => decoding = false,
                b'n' => strict = false,
                b'u' => {
                    out.write_all(HELP)?;
                    return Ok(0);
                }
                _ => {
                    err.write_all(program.as_encoded_bytes())?;
                    err.write_all(b": invalid option -- '")?;
                    err.write_all(&[key])?;
                    err.write_all(b"'\n")?;
                    out.write_all(HELP)?;
                    return Ok(0);
                }
            }
            if c == b'-' {
                break;
            }
        }
    }
    let mut stdin = io::stdin().lock();
    let mut infile;
    let input: &mut dyn Read = match files.first().filter(|p| *p != "-") {
        Some(p) => match File::open(p) {
            Ok(f) => {
                infile = f;
                &mut infile
            }
            Err(_) => return open_error(b"input", p, &mut err),
        },
        None => &mut stdin,
    };
    let mut outfile;
    let output: &mut dyn Write = match files.get(1).filter(|p| *p != "-") {
        Some(p) => match File::create(p) {
            Ok(f) => {
                outfile = f;
                &mut outfile
            }
            Err(_) => return open_error(b"output", p, &mut err),
        },
        None => &mut out,
    };
    if files.len() > 2 {
        err.write_all(b"Too many file names specified.\n")?;
        out.write_all(HELP)?;
        return Ok(2);
    }
    let mut writer = io::BufWriter::new(output);
    let result = if decoding {
        decode(input, &mut writer, &mut err, strict)
    } else {
        encode(input, &mut writer).map(|()| 0)
    };
    writer.flush()?;
    result
}

fn main() -> ExitCode {
    ExitCode::from(run().unwrap_or(1))
}

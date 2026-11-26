use std::io::Write;
use std::process::{Command, Stdio};

fn check(args: &[&str], input: &[u8], code: i32, stdout: &[u8], stderr: &[u8]) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_base64"))
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(input).unwrap();
    let output = child.wait_with_output().unwrap();
    assert_eq!(output.status.code(), Some(code));
    assert_eq!(output.stdout, stdout);
    assert_eq!(output.stderr, stderr);
}

#[test]
fn empty_and_wrapped_lines_have_crlf() {
    check(&[], b"", 0, b"\r\n", b"");
    let expected = [b"YWFh".repeat(18), b"\r\nYQ==\r\n".to_vec()].concat();
    check(&[], &[b'a'; 55], 0, &expected, b"");
}

#[test]
fn decode_preserves_completed_groups_before_rejecting_a_tail() {
    check(&["-d"], b"QUJDZ", 1, b"ABC", b"Input file incomplete.\n");
    check(&["-dn"], b"QUJDZ", 0, b"ABC", b"");
}

#[test]
fn legacy_decoder_stops_at_padding_and_accepts_ff_as_zero() {
    check(&["-d"], b"\xffAAA YQ==ignored!", 0, b"\0\0\0a", b"");
}

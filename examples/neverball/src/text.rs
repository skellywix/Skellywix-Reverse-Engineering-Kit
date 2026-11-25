// SPDX-License-Identifier: GPL-2.0-or-later
//
// Rust port of small C helpers used by Neverball 1.6.0 to read text: atoi and
// sscanf("%d ...") as used in ball/level.c and ball/set.c, SAFECPY, and the
// line reader in share/common.c. Original: Copyright (C) 2003 Robert Kooima
// and the Neverball authors. See NOTICE.md and COPYING.

//! Text helpers that reproduce the C library behavior Neverball relies on.

/// Reads one `%d` from the start of `s`: skips leading whitespace, takes an
/// optional sign and the digits that follow, and returns the value with the
/// rest of the string. Out-of-range values saturate (C leaves them undefined).
pub(crate) fn scan_int(s: &str) -> Option<(i32, &str)> {
    let t = s.trim_start();
    let bytes = t.as_bytes();
    let mut end = 0;
    if matches!(bytes.first(), Some(b'+' | b'-')) {
        end = 1;
    }
    let digits_start = end;
    while bytes.get(end).is_some_and(u8::is_ascii_digit) {
        end += 1;
    }
    if end == digits_start {
        return None;
    }
    let negative = bytes[0] == b'-';
    let saturated = if negative { i64::MIN } else { i64::MAX };
    let wide = t[..end].parse::<i64>().unwrap_or(saturated);
    let value = wide.clamp(i64::from(i32::MIN), i64::from(i32::MAX)) as i32;
    Some((value, &t[end..]))
}

/// `atoi`: an integer prefix, or zero.
pub(crate) fn atoi(s: &str) -> i32 {
    scan_int(s).map_or(0, |(v, _)| v)
}

/// `sscanf(s, "%d %d ...")` with up to `N` conversions. Returns the values
/// parsed before the first failure and how many that was.
pub(crate) fn scan_ints<const N: usize>(s: &str) -> ([i32; N], usize) {
    let mut out = [0; N];
    let mut rest = s;
    let mut parsed = 0;
    while parsed < N {
        let Some((value, tail)) = scan_int(rest) else {
            break;
        };
        out[parsed] = value;
        parsed += 1;
        rest = tail;
    }
    (out, parsed)
}

/// `SAFECPY` into a buffer of `capacity` bytes: at most `capacity - 1` bytes,
/// cut at a character boundary.
pub(crate) fn safe_copy(s: &str, capacity: usize) -> String {
    let max = capacity.saturating_sub(1);
    if s.len() <= max {
        return s.to_owned();
    }
    let mut end = max;
    while !s.is_char_boundary(end) {
        end -= 1;
    }
    s[..end].to_owned()
}

/// Lines as `read_line` returns them: split at `\n`, trailing `\r` and `\n`
/// stripped, empty lines kept, no phantom line after a final newline.
pub(crate) fn read_lines(text: &str) -> impl Iterator<Item = &str> {
    text.split_inclusive('\n')
        .map(|line| line.trim_end_matches(['\r', '\n']))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scan_int_reads_a_signed_prefix_and_returns_the_rest() {
        assert_eq!(scan_int("42"), Some((42, "")));
        assert_eq!(scan_int("  -17 rest"), Some((-17, " rest")));
        assert_eq!(scan_int("+5x"), Some((5, "x")));
        assert_eq!(scan_int("007"), Some((7, "")));
        assert_eq!(scan_int("3.9"), Some((3, ".9")));
    }

    #[test]
    fn scan_int_rejects_text_without_digits() {
        for s in ["", "   ", "abc", "-", "+", "- 5", "--5", ".5"] {
            assert_eq!(scan_int(s), None, "input {s:?}");
        }
    }

    #[test]
    fn scan_int_saturates_out_of_range_values() {
        assert_eq!(scan_int("2147483647"), Some((i32::MAX, "")));
        assert_eq!(scan_int("2147483648"), Some((i32::MAX, "")));
        assert_eq!(scan_int("-2147483648"), Some((i32::MIN, "")));
        assert_eq!(scan_int("-2147483649"), Some((i32::MIN, "")));
        // Too long even for i64.
        assert_eq!(scan_int("99999999999999999999999"), Some((i32::MAX, "")));
        assert_eq!(scan_int("-99999999999999999999999"), Some((i32::MIN, "")));
    }

    #[test]
    fn atoi_defaults_to_zero() {
        assert_eq!(atoi("12 coins"), 12);
        assert_eq!(atoi("none"), 0);
        assert_eq!(atoi(""), 0);
    }

    #[test]
    fn scan_ints_stops_at_the_first_failure() {
        assert_eq!(scan_ints::<3>("1 2 3"), ([1, 2, 3], 3));
        assert_eq!(scan_ints::<3>("1 2"), ([1, 2, 0], 2));
        assert_eq!(scan_ints::<3>("1 x 3"), ([1, 0, 0], 1));
        assert_eq!(scan_ints::<3>(""), ([0, 0, 0], 0));
        // Extra values beyond N are left alone.
        assert_eq!(scan_ints::<2>("1 2 3"), ([1, 2], 2));
    }

    #[test]
    fn safe_copy_keeps_one_byte_for_the_terminator() {
        assert_eq!(safe_copy("abcd", 5), "abcd");
        assert_eq!(safe_copy("abcde", 5), "abcd");
        assert_eq!(safe_copy("abc", 0), "");
        assert_eq!(safe_copy("", 1), "");
    }

    #[test]
    fn safe_copy_backs_up_to_a_character_boundary() {
        // Each euro sign is three bytes; a limit of 5 bytes fits one.
        assert_eq!(safe_copy("\u{20AC}\u{20AC}\u{20AC}", 6), "\u{20AC}");
        assert_eq!(safe_copy("\u{20AC}\u{20AC}", 7), "\u{20AC}\u{20AC}");
    }

    #[test]
    fn read_lines_strips_line_endings_and_keeps_blank_lines() {
        let lines: Vec<&str> = read_lines("a\r\n\nb\r\nc").collect();
        assert_eq!(lines, ["a", "", "b", "c"]);
        assert_eq!(read_lines("").count(), 0);
        assert_eq!(read_lines("x\n").collect::<Vec<_>>(), ["x"]);
    }
}

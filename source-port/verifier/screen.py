"""The fallback screen: advisory only. A clean scan is never runtime proof of "no fallback".

Observed: the exact candidate source that was hashed and built. A hit means the source names a way to start another
program or reach the kernel directly (std::process and its std::os extension traits, unsafe, extern, inline assembly,
symbol tricks), and `-F unsafe_code` is also passed to the build. A hit sets used_fallback true and the export gate
refuses it; it may be a false positive such as a comment. A clean scan only means no hit: it never turns a blocked
runtime (containers.candidate_runtime_blocker) into a receipt, because a pid limit of 1 stops fork, not execve, and a
candidate sharing an image with the original's interpreter has a fallback path within reach.
"""
import re

PATTERN = (r'process|Command|unsafe|asm|extern|libc|syscall|\binclude(?:_str|_bytes)?\b|\blink(?:_name|_section)?\b|'
           r'no_mangle|export_name|transmute|\bexec(?:l|le|lp|v|ve|vp)?\b|\bdlopen\b')
_SCREEN = re.compile(PATTERN)


def used_fallback(candidate):
    """True when the exact candidate bytes (already checked to be UTF-8) match the fallback screen."""
    return _SCREEN.search(candidate.decode('utf-8')) is not None

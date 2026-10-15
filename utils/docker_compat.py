"""Docker startup compatibility for positional writes blocked by legacy seccomp."""
import ctypes
import errno
import os
import platform
import sys
import tempfile


_ARCHITECTURES = {
    "x86_64": (0xC000003E, 328),
    "aarch64": (0xC00000B7, 287),
    "arm64": (0xC00000B7, 287),
    "armv7l": (0x40000028, 393),
    "armv8l": (0x40000028, 393),
    "armv6l": (0x40000028, 393),
}
_BPF_LD_W_ABS = 0x20
_BPF_JMP_JEQ_K = 0x15
_BPF_RET_K = 0x06
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ushort),
        ("filter", ctypes.POINTER(_SockFilter)),
    ]


def _install_write_fallback():
    architecture = platform.machine().lower()
    if ctypes.sizeof(ctypes.c_void_p) == 4 and architecture in {"aarch64", "arm64"}:
        architecture = "armv7l"
    if architecture not in _ARCHITECTURES:
        raise RuntimeError(f"Unsupported architecture for positional-write compatibility: {architecture}")
    audit_arch, syscall = _ARCHITECTURES[architecture]
    # ALLOW leaves inherited filters in force. For an existing ERRNO denial,
    # the newest filter supplies ENOSYS so libc can try the permitted pwrite64.
    instructions = (_SockFilter * 7)(
        _SockFilter(_BPF_LD_W_ABS, 0, 0, 4),
        _SockFilter(_BPF_JMP_JEQ_K, 1, 0, audit_arch),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
        _SockFilter(_BPF_LD_W_ABS, 0, 0, 0),
        _SockFilter(_BPF_JMP_JEQ_K, 0, 1, syscall),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | errno.ENOSYS),
        _SockFilter(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
    )
    program = _SockFprog(len(instructions), instructions)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    for arguments in (
        (_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0),
        (_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.addressof(program), 0, 0),
    ):
        if libc.prctl(*arguments) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))


def enable_positional_write_compatibility():
    """Enable an inherited ENOSYS fallback only when a positional write fails with EPERM."""
    if sys.platform != "linux":
        return False
    try:
        file = tempfile.TemporaryFile()
    except OSError as exc:
        print(f"Positional-write compatibility probe skipped: {exc}", file=sys.stderr, flush=True)
        return False
    with file:
        try:
            if os.pwrite(file.fileno(), b"probe", 0) != 5:
                raise OSError(errno.EIO, "Positional-write probe returned a short write")
        except OSError as exc:
            if exc.errno != errno.EPERM:
                print(f"Positional-write compatibility probe skipped: {exc}", file=sys.stderr, flush=True)
                return False
        else:
            return False
        _install_write_fallback()
        if os.pwrite(file.fileno(), b"probe", 0) != 5:
            raise OSError(errno.EIO, "Positional-write compatibility verification failed")
        file.seek(0)
        if file.read() != b"probe":
            raise OSError(errno.EIO, "Positional-write compatibility readback failed")
    return True


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python -m utils.docker_compat COMMAND [ARG ...]")
    if enable_positional_write_compatibility():
        print("Legacy positional-write compatibility enabled; inherited seccomp restrictions retained", file=sys.stderr, flush=True)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()

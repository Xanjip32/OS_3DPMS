"""Encrypt secrets at rest with Windows DPAPI (current user scope).

On other systems secrets stay in config.json; protect that file with
file permissions (chmod 600).
"""
import base64
import ctypes
import os


def available():
    return os.name == "nt"


if available():
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _UI_FORBIDDEN = 0x1

    def _call(fn, data, *extra):
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _Blob()
        if not fn(ctypes.byref(blob_in), *extra, None, None, None, _UI_FORBIDDEN, ctypes.byref(blob_out)):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def protect(text):
    """Return a base64 DPAPI blob for text."""
    blob = _call(_crypt32.CryptProtectData, text.encode("utf-8"), "OS_3DPMS")
    return base64.b64encode(blob).decode("ascii")


def unprotect(token):
    blob = _call(_crypt32.CryptUnprotectData, base64.b64decode(token), None)
    return blob.decode("utf-8")

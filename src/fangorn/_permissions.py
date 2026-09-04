from __future__ import annotations

import ctypes
import errno
import sys

_DARWIN_ACL_TYPE_EXTENDED = 0x100
_DARWIN_ACL_EXTENDED_ALLOW = 1
_DARWIN_ACL_FIRST_ENTRY = 0
_DARWIN_ACL_NEXT_ENTRY = -1
_DARWIN_ACL_READ_OR_SEARCH = 2 | 8 | (1 << 7) | (1 << 9) | (1 << 11)
_DARWIN_ACL_WRITE = 4 | 16 | 32 | 64 | 256 | 1024 | 4096 | 8192


def descriptor_has_writable_acl(descriptor: int) -> bool:
    return sys.platform == "darwin" and darwin_acl_allows_write(descriptor)


def descriptor_has_private_acl(descriptor: int) -> bool:
    return sys.platform == "darwin" and _darwin_acl_allows(
        descriptor, _DARWIN_ACL_READ_OR_SEARCH | _DARWIN_ACL_WRITE
    )


def darwin_acl_allows_write(descriptor: int) -> bool:
    return _darwin_acl_allows(descriptor, _DARWIN_ACL_WRITE)


def _darwin_acl_allows(descriptor: int, permissions_mask: int) -> bool:
    try:
        library = ctypes.CDLL(None, use_errno=True)
        get_acl = library.acl_get_fd_np
        get_acl.argtypes = (ctypes.c_int, ctypes.c_int)
        get_acl.restype = ctypes.c_void_p
        get_entry = library.acl_get_entry
        get_entry.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
        get_entry.restype = ctypes.c_int
        get_tag = library.acl_get_tag_type
        get_tag.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        get_tag.restype = ctypes.c_int
        get_permissions = library.acl_get_permset_mask_np
        get_permissions.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        get_permissions.restype = ctypes.c_int
        free_acl = library.acl_free
        free_acl.argtypes = (ctypes.c_void_p,)
        free_acl.restype = ctypes.c_int
    except (AttributeError, OSError) as error:
        raise OSError(errno.EIO, "Cannot inspect descriptor ACL") from error

    ctypes.set_errno(0)
    acl = get_acl(descriptor, _DARWIN_ACL_TYPE_EXTENDED)
    if not acl:
        code = ctypes.get_errno()
        if code in {errno.ENOENT, errno.EOPNOTSUPP}:
            return False
        raise OSError(code or errno.EIO, "Cannot inspect descriptor ACL")
    try:
        entry = ctypes.c_void_p()
        ctypes.set_errno(0)
        status = get_entry(acl, _DARWIN_ACL_FIRST_ENTRY, ctypes.byref(entry))
        while status == 0:
            tag = ctypes.c_int()
            permissions = ctypes.c_uint64()
            if get_tag(entry, ctypes.byref(tag)) != 0:
                raise OSError(errno.EIO, "Cannot inspect descriptor ACL")
            if tag.value == _DARWIN_ACL_EXTENDED_ALLOW:
                if get_permissions(entry, ctypes.byref(permissions)) != 0:
                    raise OSError(errno.EIO, "Cannot inspect descriptor ACL")
                if permissions.value & permissions_mask:
                    return True
            ctypes.set_errno(0)
            status = get_entry(acl, _DARWIN_ACL_NEXT_ENTRY, ctypes.byref(entry))
        code = ctypes.get_errno()
        if status != -1 or code != errno.EINVAL:
            raise OSError(code or errno.EIO, "Cannot inspect descriptor ACL")
        return False
    finally:
        if free_acl(acl) != 0:
            raise OSError(errno.EIO, "Cannot release descriptor ACL")

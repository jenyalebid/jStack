import Darwin

enum PathProtectionFailure: Error { case unobservableACL, writableACL }

// Unix mode bits do not describe macOS extended ACL grants. Conservatively
// reject every allow entry that can change protected code or its ancestry,
// including inherited grants. Read-only grants and system deny entries remain
// supported. These are the public acl(3) interfaces, not a privacy database.
func rejectWritableACL(_ path: String) throws {
    guard let acl = acl_get_link_np(path, ACL_TYPE_EXTENDED) else {
        // APFS reports ENOENT for an existing object with no extended ACL.
        // Confirm the object still exists so missing paths never pass.
        if errno == ENOENT {
            var info = stat()
            if lstat(path, &info) == 0 { return }
        }
        throw PathProtectionFailure.unobservableACL
    }
    defer { acl_free(UnsafeMutableRawPointer(acl)) }
    guard acl_valid(acl) == 0 else { throw PathProtectionFailure.unobservableACL }
    var selector = ACL_FIRST_ENTRY.rawValue
    let forbidden = [ACL_WRITE_DATA, ACL_APPEND_DATA, ACL_DELETE, ACL_DELETE_CHILD,
                     ACL_WRITE_ATTRIBUTES, ACL_WRITE_EXTATTRIBUTES, ACL_WRITE_SECURITY, ACL_CHANGE_OWNER]
    while true {
        var entry: acl_entry_t?
        let result = acl_get_entry(acl, selector, &entry)
        if result != 0 {
            // Darwin returns EINVAL at the end of a valid ACL, unlike Linux.
            guard errno == EINVAL else { throw PathProtectionFailure.unobservableACL }
            return
        }
        selector = ACL_NEXT_ENTRY.rawValue
        guard let entry else { throw PathProtectionFailure.unobservableACL }
        var tag = ACL_UNDEFINED_TAG
        guard acl_get_tag_type(entry, &tag) == 0,
              tag == ACL_EXTENDED_ALLOW || tag == ACL_EXTENDED_DENY else {
            throw PathProtectionFailure.unobservableACL
        }
        if tag == ACL_EXTENDED_DENY { continue }
        var permissions: acl_permset_t?
        guard acl_get_permset(entry, &permissions) == 0, let permissions else {
            throw PathProtectionFailure.unobservableACL
        }
        for permission in forbidden {
            let permitted = acl_get_perm_np(permissions, permission)
            guard permitted >= 0 else { throw PathProtectionFailure.unobservableACL }
            if permitted == 1 { throw PathProtectionFailure.writableACL }
        }
    }
}

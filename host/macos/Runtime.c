/* The service process is product-owned native code, embedding the bundled
 * interpreter in isolated mode. No shell, PATH lookup or external Python. */
#include <Python.h>
#include <Security/Security.h>
#include <mach-o/dyld.h>
#include <os/log.h>
#include <limits.h>
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

/* Who has to have signed these bytes.
 *
 * A published Hub answers with the publisher's Developer ID team. A Hub
 * compiled on the machine that will run it cannot: there is no such identity
 * on that Mac, so it is signed ad-hoc and answers with the bundle identifier
 * alone. Demanding the team unconditionally is why a locally built Hub got
 * all the way through its build and then failed its own sealed installer.
 *
 * Compiled in rather than read at launch, because the seal this check
 * enforces covers this binary: a bundle cannot relax its own requirement
 * without invalidating the signature that carries it. A marker read out of
 * Resources at launch could be set by whoever assembled the bundle, which is
 * the one party the check exists to answer for.
 */
#ifdef JSTACK_SOURCE_BUILD
#define JSTACK_REQUIREMENT "identifier \"live.jstack.hub\""
#else
#define JSTACK_REQUIREMENT "anchor apple generic and certificate leaf[subject.OU] = \"MZ95H77RQQ\" " \
                           "and identifier \"live.jstack.hub\""
#endif

/* Where a refusal is left for whoever comes looking.
 *
 * launchd starts the sealed services with no StandardErrorPath — the plist is
 * sealed and cannot name a per-user file — so a refusal written only to stderr
 * reached nobody: a dead service, exit 78, and nothing in any log (#285). The
 * file sits where a Mac keeps application logs and the menu bar reads it; a
 * runtime that verifies clean removes it, so it never outlives the fault.
 */
static int refusal_path(char *out, size_t size, int create) {
    const char *home = getenv("HOME");
    if (!home || home[0] != '/') {
        struct passwd *entry = getpwuid(getuid());
        home = entry && entry->pw_dir && entry->pw_dir[0] == '/' ? entry->pw_dir : NULL;
    }
    if (!home) return 0;
    char directory[PATH_MAX];
    int length = snprintf(directory, sizeof directory, "%s/Library/Logs/jStack", home);
    if (length < 0 || (size_t)length >= sizeof directory) return 0;
    if (create) {
        char *cut = directory + strlen(home);
        while ((cut = strchr(cut + 1, '/'))) {
            *cut = '\0';
            mkdir(directory, 0700);
            *cut = '/';
        }
        mkdir(directory, 0700);
    }
    length = snprintf(out, size, "%s/runtime.log", directory);
    return length >= 0 && (size_t)length < size;
}

typedef struct { char *text; size_t length, capacity; } report_t;

static void append(report_t *report, const char *text) {
    size_t more = strlen(text);
    if (report->length + more + 1 > report->capacity) {
        size_t capacity = (report->capacity ? report->capacity * 2 : 1024) + more;
        char *grown = realloc(report->text, capacity);
        if (!grown) return;
        report->text = grown;
        report->capacity = capacity;
    }
    memcpy(report->text + report->length, text, more + 1);
    report->length += more;
}

/* One line per file the seal no longer matches, and whether every one of them
 * is Python bytecode — the one fault with a remedy short of a reinstall. */
static size_t list_files(report_t *report, CFDictionaryRef info, CFStringRef key,
                         const char *verb, const char *bundle, int *bytecode_only) {
    CFArrayRef files = info ? CFDictionaryGetValue(info, key) : NULL;
    if (!files || CFGetTypeID(files) != CFArrayGetTypeID()) return 0;
    CFIndex count = CFArrayGetCount(files);
    size_t prefix = strlen(bundle);
    for (CFIndex index = 0; index < count; index++) {
        CFTypeRef item = CFArrayGetValueAtIndex(files, index);
        char path[PATH_MAX] = "?";
        if (CFGetTypeID(item) == CFURLGetTypeID())
            CFURLGetFileSystemRepresentation(item, true, (UInt8 *)path, sizeof path);
        else if (CFGetTypeID(item) == CFStringGetTypeID())
            CFStringGetCString(item, path, sizeof path, kCFStringEncodingUTF8);
        if (strcmp(verb, "added") != 0 || !strstr(path, "/__pycache__/")) *bytecode_only = 0;
        if (index < 20) {
            const char *shown = strncmp(path, bundle, prefix) == 0 && path[prefix] == '/' ? path + prefix + 1 : path;
            char line[PATH_MAX + 32];
            snprintf(line, sizeof line, "  %s: %s\n", verb, shown);
            append(report, line);
        } else if (index == 20) {
            char line[64];
            snprintf(line, sizeof line, "  … and %ld more %s\n", (long)(count - 20), verb);
            append(report, line);
        }
    }
    return (size_t)count;
}

/* Say why, everywhere an operator could look: stderr for a terminal, the
 * refusal file for the menu bar and a person, the unified log for `log show`. */
static void refuse(const char *bundle, OSStatus status, CFErrorRef error, const char *role) {
    report_t report = {0};
    char line[PATH_MAX + 256];
    time_t now = time(NULL);
    char stamp[32] = "";
    strftime(stamp, sizeof stamp, "%Y-%m-%dT%H:%M:%S%z", localtime(&now));
    snprintf(line, sizeof line, "%s jStack runtime refused to start %s: the signature or resource seal of %s "
             "does not verify (OSStatus %d)\n", stamp, role, bundle, (int)status);
    append(&report, line);
    CFDictionaryRef info = error ? CFErrorCopyUserInfo(error) : NULL;
    int bytecode_only = 1;
    size_t files = list_files(&report, info, kSecCFErrorResourceAdded, "added", bundle, &bytecode_only)
                 + list_files(&report, info, kSecCFErrorResourceAltered, "altered", bundle, &bytecode_only)
                 + list_files(&report, info, kSecCFErrorResourceMissing, "missing", bundle, &bytecode_only);
    if (info) CFRelease(info);
    if (files && bytecode_only) {
        append(&report, "Cause: Python bytecode was written into the sealed bundle — some interpreter other "
                        "than this runtime imported from Contents/Resources/packages.\n");
        snprintf(line, sizeof line, "Remedy: find '%s/Contents' -name __pycache__ -type d -prune -exec rm -rf {} + "
                 "&& launchctl kickstart -k gui/$(id -u)/live.jstack.hub.host\n", bundle);
    } else {
        snprintf(line, sizeof line, "Remedy: reinstall jStack Hub — this bundle is not the one that was signed.\n");
    }
    append(&report, line);
    if (!report.text) {
        fputs("jStack runtime rejected the application signature or resource seal\n", stderr);
        return;
    }
    fputs(report.text, stderr);
    os_log_error(OS_LOG_DEFAULT, "%{public}s", report.text);
    char path[PATH_MAX];
    if (refusal_path(path, sizeof path, 1)) {
        FILE *stream = fopen(path, "w");
        if (stream) {
            fputs(report.text, stream);
            fclose(stream);
        }
    }
    free(report.text);
}

static int verify_bundle(const char *macos, const char *role) {
    char bundle_path[PATH_MAX], resolved[PATH_MAX];
    int length = snprintf(bundle_path, sizeof bundle_path, "%s/../..", macos);
    if (length < 0 || (size_t)length >= sizeof bundle_path || !realpath(bundle_path, resolved)) {
        refuse(bundle_path, errSecCSStaticCodeNotFound, NULL, role);
        return 0;
    }
    CFURLRef url = CFURLCreateFromFileSystemRepresentation(NULL, (const UInt8 *)resolved, strlen(resolved), true);
    if (!url) return 0;
    SecStaticCodeRef code = NULL;
    SecRequirementRef requirement = NULL;
    CFErrorRef error = NULL;
    SecCSFlags flags = kSecCSCheckAllArchitectures | kSecCSCheckNestedCode | kSecCSStrictValidate;
    OSStatus status = SecStaticCodeCreateWithPath(url, kSecCSDefaultFlags, &code);
    if (status == errSecSuccess) status = SecRequirementCreateWithString(
        CFSTR(JSTACK_REQUIREMENT), kSecCSDefaultFlags, &requirement);
    // The error names every added, altered and missing file. kSecCSFullReport
    // is not wanted: it returns a different status with no file lists at all.
    if (status == errSecSuccess) status = SecStaticCodeCheckValidityWithErrors(code, flags, requirement, &error);
    if (status == errSecSuccess) {
        char path[PATH_MAX];
        if (refusal_path(path, sizeof path, 0)) unlink(path);
    } else {
        refuse(resolved, status, error, role);
    }
    if (error) CFRelease(error);
    if (requirement) CFRelease(requirement);
    if (code) CFRelease(code);
    CFRelease(url);
    return status == errSecSuccess;
}

int main(int argc, char **argv) {
    char executable[PATH_MAX], resolved[PATH_MAX], home[PATH_MAX], entry[PATH_MAX], python[PATH_MAX], packages[PATH_MAX];
    uint32_t size = sizeof executable;
    if (geteuid() == 0) {
        fputs("jStack runtime refuses root; use the restricted network helper\n", stderr);
        return 77;
    }
    if (_NSGetExecutablePath(executable, &size) != 0 || !realpath(executable, resolved)) return 78;
    char *slash = strrchr(resolved, '/');
    if (!slash) return 78;
    *slash = '\0';
    // Hardened-runtime library validation does not attest Python source.
    // Validate the complete resource seal before importing any module.
    char role[NAME_MAX + 64];
    snprintf(role, sizeof role, "%s%s%.63s", slash + 1, argc > 1 ? " " : "", argc > 1 ? argv[1] : "");
    if (!verify_bundle(resolved, role)) return 78;
    int home_length = snprintf(home, sizeof home, "%s/../Frameworks/Python.framework/Versions/3.12", resolved);
    int entry_length = snprintf(entry, sizeof entry, "%s/../Resources/runtime_entry.py", resolved);
    int python_length = snprintf(python, sizeof python, "%s/JStackPython", resolved);
    int packages_length = snprintf(packages, sizeof packages, "%s/../Resources/packages", resolved);
    if (home_length < 0 || (size_t)home_length >= sizeof home ||
        python_length < 0 || (size_t)python_length >= sizeof python ||
        packages_length < 0 || (size_t)packages_length >= sizeof packages ||
        entry_length < 0 || (size_t)entry_length >= sizeof entry) return 78;

    PyPreConfig preconfig;
    PyPreConfig_InitIsolatedConfig(&preconfig);
    preconfig.utf8_mode = 1;
    PyStatus status = Py_PreInitialize(&preconfig);
    if (PyStatus_Exception(status)) Py_ExitStatusException(status);
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.parse_argv = 0;
#ifdef JSTACK_PYTHON
    config.parse_argv = 1;
#endif
    config.write_bytecode = 0;
    status = PyConfig_SetBytesString(&config, &config.home, home);
    if (!PyStatus_Exception(status)) status = PyConfig_SetBytesString(&config, &config.program_name, executable);
    if (!PyStatus_Exception(status)) status = PyConfig_SetBytesString(&config, &config.executable, python);
#ifndef JSTACK_PYTHON
    if (!PyStatus_Exception(status)) status = PyConfig_SetBytesString(&config, &config.run_filename, entry);
#endif
    if (!PyStatus_Exception(status)) status = PyConfig_SetBytesArgv(&config, argc, argv);
    if (!PyStatus_Exception(status)) status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) Py_ExitStatusException(status);
    PyObject *package_path = PyUnicode_DecodeFSDefault(packages);
    if (package_path == NULL || PyList_Insert(PySys_GetObject("path"), 0, package_path) < 0) {
        Py_XDECREF(package_path);
        PyErr_Print();
        return 78;
    }
    Py_DECREF(package_path);
    return Py_RunMain();
}

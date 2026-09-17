/* The service process is product-owned native code, embedding the bundled
 * interpreter in isolated mode. No shell, PATH lookup or external Python. */
#include <Python.h>
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

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

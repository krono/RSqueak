import os, sys, subprocess
import py
from rpython.translator.tool.cbuild import ExternalCompilationInfo
from rpython.rtyper.lltypesystem import rffi
from rsqueakvm.util import system

this_dir = py.path.local(__file__).dirpath()

if system.IS_WINDOWS:
    lfiles = ['comdlg32.lib', 'ole32.lib', 'shell32.lib', 'user32.lib']
else:
    lfiles = [str(this_dir.join("tinyfiledialogs/tinyfiledialogs.c"))]


_Default = "Squeak.image"

eci = ExternalCompilationInfo(
        post_include_bits = ["""
            #include <string.h>
            #ifdef _WIN32
            #include <windows.h>
            #include <Commdlg.h>
            #include "tinyfiledialogs/tinyfiledialogs.c"
            #define DLLEXPORT __declspec(dllexport)
            #else
            #include "tinyfiledialogs/tinyfiledialogs.h"
            #include <sys/time.h>
            #include <sys/resource.h>
            #define DLLEXPORT __attribute__((__visibility__("default")))
            #endif
            """],
        include_dirs=[this_dir],
        link_files=lfiles,
        separate_module_sources=["""
            DLLEXPORT int RSqueakOpenFileDialog_linux(char* szFile, int len) {
                char const * const filter = "*.image";
                const char * file = tinyfd_openFileDialog("", "", 1, &filter, 0, 0);
                if (file != 0) {
                    strcpy(szFile, file);
                }
                return (file == 0) ? 0: 1;
            }
            """]
)

__llget_file = rffi.llexternal('RSqueakOpenFileDialog_linux',
                               [rffi.CCHARP, rffi.INT], rffi.INT,
                               compilation_info=eci)
def tiny_get_file():
    charp = rffi.str2charp("".join(["\0"] * 260))
    res = __llget_file(charp, 260)
    if (res == 1):
        path = rffi.charp2str(charp)
        rffi.free_charp(charp)
        return path
    else:
        return _Default

def get_file():
    return tiny_get_file()


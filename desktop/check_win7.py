"""Проверка собранного ASHViewer.exe на совместимость с Windows 7.

Просматривает таблицы импорта всех DLL/PYD внутри exe и сообщает о функциях, которых нет в Windows 7
(сборка с ними выдаёт «Точка входа в процедуру … не найдена»). Вложенный Instant Client не проверяется:
на Windows 7 он не загружается (см. desktop.py).

    python desktop/check_win7.py dist/ASHViewer.exe
"""
import sys

import pefile
from PyInstaller.archive.readers import CArchiveReader

# Windows 8+ only (kernel32 and the API sets that forward to it)
WIN8_FUNCS = {
    "GetOverlappedResultEx", "GetSystemTimePreciseAsFileTime", "CreateFile2", "WaitOnAddress",
    "WakeByAddressSingle", "WakeByAddressAll", "GetCurrentThreadStackLimits", "SetThreadDescription",
    "GetThreadDescription", "PrefetchVirtualMemory", "ProcessPrng", "GetProcessMitigationPolicy",
    "SetProcessMitigationPolicy", "VirtualAlloc2", "MapViewOfFile3",
}
WIN8_DLLS = ("api-ms-win-core-synch-l1-2", "api-ms-win-core-path-l1-1", "api-ms-win-core-winrt",
             "bcryptprimitives")


def main(exe: str) -> int:
    arch = CArchiveReader(exe)
    bad = 0
    for name in arch.toc:
        low = name.lower().replace("\\", "/")
        if not low.endswith((".dll", ".pyd")) or low.startswith("instantclient"):
            continue
        pe = pefile.PE(data=arch.extract(name), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
            dll = entry.dll.decode().lower()
            if name.lower() == "ucrtbase.dll" and dll.startswith("api-ms-win-core-synch-l1-2"):
                continue    # the redistributable UCRT checks for these at run time
            for imp in entry.imports:
                fn = imp.name.decode() if imp.name else "#%d" % imp.ordinal
                if fn in WIN8_FUNCS or dll.startswith(WIN8_DLLS):
                    print(f"{name}: {dll}!{fn}")
                    bad += 1
    print("Windows 7: OK" if not bad else f"Windows 7: {bad} несовместимых импортов")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

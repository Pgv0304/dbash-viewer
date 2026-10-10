#!/usr/bin/env bash
# Сборка настольной версии для macOS: dist/DB ASH Viewer.app и dist/DB-ASH-Viewer-macOS.dmg (Oracle и PostgreSQL)
# Запуск из корня проекта:  desktop/build_macos.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3.13}
# пакет всегда x86_64 (как вложенный Instant Client); на Apple Silicon — через Rosetta
X=""; [ "$(uname -m)" = "arm64" ] && X="arch -x86_64"
IC_URL=https://download.oracle.com/otn_software/mac/instantclient/1916000/instantclient-basiclite-macos.x64-19.16.0.0.0dbru.dmg

echo ">> окружение сборки"
[ -d .venv-build ] || $X "$PY" -m venv .venv-build
$X .venv-build/bin/python -m pip install -q --upgrade pip
$X .venv-build/bin/python -m pip install -q -r desktop/requirements-desktop.txt

echo ">> Oracle Instant Client (macOS x64, для Oracle 11.2+)"
if [ ! -f vendor/instantclient-macos/libclntsh.dylib ]; then
  mkdir -p vendor && curl -fsSL -o vendor/ic.dmg "$IC_URL"
  MNT=$(hdiutil attach -nobrowse -readonly vendor/ic.dmg | awk -F'\t' '/\/Volumes\//{print $NF}' | tail -1)
  rm -rf vendor/instantclient-macos && mkdir -p vendor/instantclient-macos
  cp -R "$MNT"/* vendor/instantclient-macos/ 2>/dev/null || true
  hdiutil detach -quiet "$MNT"; rm -f vendor/ic.dmg
  # оставляем только библиотеки и сообщения клиента
  find vendor/instantclient-macos -maxdepth 1 \( -name '*.sh' -o -name 'BASIC_LITE_*' -o -name '*README*' \) -delete
fi

echo ">> архив клиента: только нужные библиотеки (подпись пакета не должна их менять)"
IC=vendor/instantclient-macos
rm -rf vendor/ic-pack && mkdir -p vendor/ic-pack/instantclient
cp -p $IC/libclntsh.dylib.19.1 $IC/libclntshcore.dylib.19.1 $IC/libnnz19.dylib $IC/libociicus.dylib vendor/ic-pack/instantclient/
ln -s libclntsh.dylib.19.1 vendor/ic-pack/instantclient/libclntsh.dylib
tar -C vendor/ic-pack -czf vendor/instantclient-macos.tar.gz instantclient && rm -rf vendor/ic-pack

echo ">> значок"
$X .venv-build/bin/python desktop/make_icon.py desktop/icon >/dev/null

echo ">> PyInstaller"
rm -rf build "dist/DB ASH Viewer.app" dist/DB-ASH-Viewer-macOS.dmg
$X .venv-build/bin/python -m PyInstaller --noconfirm --clean --windowed --name "DB ASH Viewer" \
  --icon desktop/icon/icon.icns --osx-bundle-identifier ru.pgv.dbashviewer \
  --paths . --add-data "app/static:app/static" --add-data "vendor/instantclient-macos.tar.gz:." \
  --hidden-import app.main --collect-submodules app --collect-submodules uvicorn \
  --collect-submodules psycopg --collect-all psycopg_binary \
  desktop/desktop.py >/dev/null

echo ">> подпись (ad-hoc) и образ диска"
codesign --force --deep -s - "dist/DB ASH Viewer.app"
# hdiutil на раннерах CI иногда отвечает «Resource busy» — несколько попыток
for i in 1 2 3 4 5; do
  hdiutil create -volname "DB ASH Viewer" -srcfolder "dist/DB ASH Viewer.app" -ov -format UDZO dist/DB-ASH-Viewer-macOS.dmg && break
  [ $i = 5 ] && exit 1; echo "hdiutil: попытка $i не удалась, повтор"; sleep 10
done
# файл подключения PostgreSQL рядом с программой (в образ диска он не попадает)
[ -f pg.txt ] && cp -p pg.txt dist/pg.txt && echo ">> pg.txt скопирован в dist/"
du -sh "dist/DB ASH Viewer.app" dist/DB-ASH-Viewer-macOS.dmg

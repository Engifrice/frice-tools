# Corresponding source of this RenderDoc

RenderDoc is licensed under the MIT license (LICENSE.md). It includes Qt and,
on Windows, PySide2, which are licensed under the GNU Lesser General Public
License, version 2.1 (COPYING.LGPLv2.1). The other third-party components are
listed in credits_acknowledgements.rst. The source of all of them is
available at the locations below.

## RenderDoc

- https://github.com/baldurk/renderdoc at tag `v1.46`
  (commit `e4bd23b671d3d5a747ff5221dbe08a63eb6ca200`).
- The release packages are built by `util/buildscripts/build.sh` in that
  tree.

## Linux (x86_64)

Qt and Python are built from source and linked into qrenderdoc, by
`util/buildscripts/scripts/docker/prepare.sh` in the RenderDoc tree above:

- Qt 5.15.2:
  https://download.qt.io/archive/qt/5.15/5.15.2/single/qt-everywhere-src-5.15.2.tar.xz
- Python 3.8.20: https://www.python.org/ftp/python/3.8.20/Python-3.8.20.tgz

## Windows (x86_64)

Qt, PySide2 and Python come from RenderDoc's prebuilt
https://renderdoc.org/qrenderdoc_3rdparty.zip (see
`util/buildscripts/README.md` in the RenderDoc tree above):

- Qt 5.15.2:
  https://download.qt.io/archive/qt/5.15/5.15.2/single/qt-everywhere-src-5.15.2.tar.xz
- PySide2 5.15.2:
  https://download.qt.io/official_releases/QtForPython/pyside2/PySide2-5.15.2-src/pyside-setup-opensource-src-5.15.2.tar.xz
  (tag `v5.15.2` of https://code.qt.io/cgit/pyside/pyside-setup.git)
- Python 3.8: https://www.python.org/downloads/source/
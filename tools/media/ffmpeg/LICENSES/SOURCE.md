# Corresponding source of this FFmpeg

The ffmpeg and ffprobe programs and the libraries in this directory are
licensed under the GNU General Public License, version 3 or later (see
COPYING.GPLv3 and LICENSE.md). Their complete corresponding source is
available at the locations below.

## Windows (x86_64) and Linux (x86_64, arm64)

Built by BtbN/FFmpeg-Builds, release `autobuild-2026-09-30-13-08`, variant
`gpl-shared`, FFmpeg release branch 9.0.

- Build scripts: https://github.com/BtbN/FFmpeg-Builds
  at commit `6c9aec5fc9a72ec3abedd1fa84db141fa18cf52b`
  (tag `autobuild-2026-09-30-13-08`).
  Every library linked into the build is pinned there: each file in
  `scripts.d/` names the library's repository and exact commit
  (`SCRIPT_REPO` and `SCRIPT_COMMIT`), and `download.sh` fetches all of them.
- FFmpeg: https://github.com/FFmpeg/FFmpeg
  at commit `2a571b606854520cf89804d8030c8b328e621689`
  (branch `release/9.0`; `git describe`: `n9.0.2-17-g2a571b6`).

To rebuild, at that commit of the build scripts, with `<target>` one of
`win64`, `linux64`, `linuxarm64`:

    ./makeimage.sh <target> gpl-shared 9.0
    ./build.sh <target> gpl-shared 9.0

## macOS (arm64)

Built by Martin Riedl's FFmpeg build server, release build 9.0.2
(`1789931890_9.0.2`, 20 September 2026).

- Build script: https://git.martin-riedl.de/ffmpeg/build-script
  at commit `f63b8aab8f5ce1a067da86ba69e34a36a7e217e5` (branch `main`, the
  tip of the branch when the build was made). The script's `version/` folder
  still names FFmpeg 9.0.1; the build server built the 9.0.2 release tarball
  below with it.
- `macos-versions.txt` next to this file is the build's own record, published
  with it: the FFmpeg configure line and the version of every library linked
  into the build.
- FFmpeg: release 9.0.2, https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz
  (git tag `n9.0.2`, commit `946fcce07b6dcd0331c8cc609192aeff5e1924f8`).
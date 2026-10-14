# `frice-tools`:
The `engifrice` editor is quite barebone. To complement its missing features, this repository contains extra tools used by the editor.

These tools are expected to only run on desktop platforms.

## Status:
[![CI](https://github.com/Engifrice/frice-tools/actions/workflows/pr.yml/badge.svg?branch=main&event=push)](https://github.com/Engifrice/frice-tools/actions/workflows/pr.yml)

[![Release](https://img.shields.io/github/v/release/Engifrice/frice-tools)](https://github.com/Engifrice/frice-tools/releases/latest)

## List of Tools:
Build tools:
- [CMake](https://cmake.org/download/), the CMake build-system executable,
- [Ninja](https://github.com/ninja-build/ninja), used as the target output of CMake.

Profiling:
- [Tracy](https://github.com/wolfpld/tracy), the CPU profiler used by the engine.

Graphics:
- [RenderDoc](https://renderdoc.org/builds), the GPU frame analyzer,
- [Slang](https://github.com/shader-slang/slang), the `slang` (shader language) compiler.
- [DXC](https://github.com/microsoft/DirectXShaderCompiler), the DirectX shader compiler, to compile shaders for Direct3D 12 (Windows only).

Media:
- [ffmpeg](https://ffmpeg.org/), to convert audio and video codecs,
- [ktx2](https://github.com/KhronosGroup/KTX-Software), to convert images to KTX2.

## License:
The scripts and configuration in this repository are licensed under the [MIT License](LICENSE).

The released zips contain third-party tools, each under its own license. The license texts of every tool ship next to it in the zip (`<tool>/LICENSES/`) and are kept in this repository under `tools/<group>/<tool>/LICENSES/`.

The released zips are components of the `engifrice` editor and are meant to be used with it, not as standalone distributions of the tools they contain.

Most tools are under permissive licenses (MIT, BSD, Apache-2.0, Zlib and similar). The exceptions are:
- **FFmpeg** is built with GPL components and is licensed under the GPL v3 or later. The editor only runs it as a separate program. Where to obtain its corresponding source code is described in `ffmpeg/LICENSES/SOURCE.md`.
- **RenderDoc** bundles Qt (and PySide2 on Windows), licensed under the LGPL v2.1. Where to obtain their source code is described in `renderdoc/LICENSES/SOURCE.md`.

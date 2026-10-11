# `frice-tools`:
The `engifrice` editor is quite barebone. To complement its missing features, this repository contains extra tools used by the editor.

These tools are expected to only run on desktop platforms.

## List of Tools:
Build tools:
- [CMake](https://cmake.org/download/), the CMake build-system executable,
- [Ninja](https://github.com/ninja-build/ninja), used as the target output of CMake.

Profiling:
- [Tracy](https://github.com/wolfpld/tracy), the CPU profiler used by the engine.

Graphics:
- [RenderDoc](https://renderdoc.org/builds), the GPU frame analyzer,
- [Slang](https://github.com/shader-slang/slang), the `slang` (shader language) compiler.

Media:
- [ffmpeg](https://ffmpeg.org/), to convert audio and video codecs,
- [ktx2](https://github.com/KhronosGroup/KTX-Software), to convert images to KTX2.
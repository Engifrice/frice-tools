# frice-tools contract

Contract version: **1**

This file is the agreement between this repository, which publishes the
toolsets, and the Frice engine, which downloads and uses them. Everything the
engine relies on is defined here. Anything not defined here is an
implementation detail of this repository and may change at any time.

A change that breaks anything in this file requires a new contract version, a
new `manifest.json` schema number, and an engine release that understands it.

## 1. Host platforms

A host is a desktop machine that runs the editor. Each host has a token made of
the engine's `OS` and `ISA` enum names in lowercase, joined by `-`:

| Token            | Engine triplet  |
| ---------------- | --------------- |
| `windows-x86_64` | Windows, X86_64 |
| `windows-arm64`  | Windows, ARM64  |
| `linux-x86_64`   | Linux, X86_64   |
| `linux-arm64`    | Linux, ARM64    |
| `macos-arm64`    | MacOS, ARM64    |

`platforms.toml` is the source of truth for this list. Mobile platforms and
consoles are never hosts.

## 2. Releases and asset names

A toolset release is one GitHub release containing one zip per host, a release
index, and the source archives that some licenses require.

Releases are immutable: once published, a tag and its assets never change and a
version number is never reused.

### Grammar

```
tag      = "v" version
asset    = "frice-tools-" version "-" kind "." ext
version  = YYYY "." MM "." N          (calendar month, then a counter from 0)
kind     = host | "release" | "src-" tool
host     = os "-" isa
os       = "windows" | "linux" | "macos"
isa      = "x86_64" | "arm64"
tool     = [a-z][a-z0-9_]*
ext      = "zip"     when kind is a host
         | "json"    when kind is "release"
         | "tar.xz"  when kind is "src-" tool
```

As a regular expression:

```
^frice-tools-(?P<version>[0-9]{4}\.(?:0[1-9]|1[0-2])\.(?:0|[1-9][0-9]*))-(?:(?P<host>(?:windows|linux|macos)-(?:x86_64|arm64))\.zip|(?P<release>release)\.json|src-(?P<tool>[a-z][a-z0-9_]*)\.tar\.xz)$
```

Asset names only ever contain `[a-z0-9._-]`.

### Example

```
tag: v2026.10.0
frice-tools-2026.10.0-windows-x86_64.zip
frice-tools-2026.10.0-windows-arm64.zip
frice-tools-2026.10.0-linux-x86_64.zip
frice-tools-2026.10.0-linux-arm64.zip
frice-tools-2026.10.0-macos-arm64.zip
frice-tools-2026.10.0-release.json
frice-tools-2026.10.0-src-ffmpeg.tar.xz
```

### Download location

```
https://github.com/<owner>/<repo>/releases/download/v{version}/frice-tools-{version}-{host}.zip
```

Mirrors serve the same file names under `.../v{version}/`. The engine accepts a
file from any mirror only if its size and SHA-256 match the engine's lockfile.

## 3. Zip layout

```
manifest.json          always at the root
<tool>/                one directory per tool shipped on this host
<tool>/LICENSES/       the licenses of everything in <tool>/
```

The directory layout below `<tool>/` is not part of the contract. The engine
finds every file through `manifest.json` and never builds a path itself.

## 4. manifest.json (schema 1)

```json
{
  "schema": 1,
  "toolset": "2026.10.0",
  "platform": "linux-x86_64",
  "tools": {
    "slang": {
      "version": "2026.19",
      "upstream_version": "v2026.19",
      "components": { "dxc": "1.9.2607" },
      "executables": { "slangc": "slang/slangc" },
      "libraries": {}
    },
    "renderdoc": {
      "version": "1.46",
      "upstream_version": "v1.46",
      "executables": { "qrenderdoc": "renderdoc/bin/qrenderdoc", "renderdoccmd": "renderdoc/bin/renderdoccmd" },
      "libraries": { "capture": "renderdoc/lib/librenderdoc.so" }
    }
  }
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | integer | `1`. The engine refuses a value it does not know. |
| `toolset` | string | the release version, matching the zip name |
| `platform` | string | the host token, matching the zip name |
| `tools` | object | tool name to tool entry |
| `tools.<t>.version` | string | normalized version: lowercase, no leading `v`, `-` and `+` written as `_` |
| `tools.<t>.upstream_version` | string | the upstream version or tag, verbatim |
| `tools.<t>.components` | object | optional: other tools bundled inside this one, name to version |
| `tools.<t>.executables` | object | key to path of a program; always present, possibly empty |
| `tools.<t>.libraries` | object | key to path of a shared library the engine may load; always present, possibly empty |

Paths are relative to the zip root, use `/`, and name the real file including
`.exe`, `lib` prefixes, `.dll`, `.so` and `.dylib`.

## 5. Compatibility rules

1. Tool names, executable keys and library keys only grow. Adding is always
   allowed.
2. Removing a tool, or a key, is allowed only when the release acknowledges it
   in `toolset.toml` (`removed = [...]`). Engines pinned to older toolsets are
   unaffected because they never download the new one.
3. A tool may be absent on some hosts. It is then missing from that host's
   `manifest.json`, and the engine treats it as unavailable there.
4. Changing the zip layout below `<tool>/` is not a breaking change.
5. Changing anything else in this file is a breaking change.

## 6. Release index (`-release.json`)

```json
{
  "schema": 1,
  "toolset": "2026.10.0",
  "assets": {
    "windows-x86_64": { "name": "frice-tools-2026.10.0-windows-x86_64.zip", "size": 0, "sha256": "<hex>" }
  },
  "sources": {
    "ffmpeg": { "name": "frice-tools-2026.10.0-src-ffmpeg.tar.xz", "size": 0, "sha256": "<hex>" }
  },
  "tools": {
    "ffmpeg": { "version": "9.0", "upstream_version": "n9.0", "platforms": ["windows-x86_64"] }
  }
}
```

The private SDK repository reads this file to write the engine's lockfile.

## 7. What the engine does

1. Download the zip for its own host from the first mirror that answers.
2. Reject it unless its size and SHA-256 match the lockfile.
3. Extract it next to its final location, read `manifest.json`, and refuse an
   unknown `schema`, or a different `toolset` or `platform`.
4. Move it into place, then resolve every tool through `manifest.json`.
# bin/

External binaries used by the gs-pot pipeline. Gitignored — install locally.

## Brush (Gaussian splat trainer)

Mac arm64 release:

```bash
cd bin/
curl -sL -o brush.tar.xz https://github.com/ArthurBrussee/brush/releases/download/v0.3.0/brush-app-aarch64-apple-darwin.tar.xz
tar -xJf brush.tar.xz && rm brush.tar.xz
ln -sf brush-app-aarch64-apple-darwin/brush_app brush
./brush --version
```

For Linux x86_64 or Windows, swap the release filename — see
<https://github.com/ArthurBrussee/brush/releases>.

## COLMAP (pose estimation)

We use the [pycolmap](https://github.com/colmap/pycolmap) Python bindings,
**not** the `colmap` CLI — pycolmap's wheels ship a working build for darwin-arm64
where the Homebrew COLMAP has a SIFT-matcher use-after-free. pycolmap is
already pinned in `pyproject.toml`, so no extra install is needed.

If you want the standalone CLI anyway (debugging, hloc, ...):

```bash
brew install colmap         # Mac
# Linux:  apt install colmap   (or build from source for CUDA support)
```

## OpenSplat (optional, faster trainer)

Brush ships as a release binary above. **OpenSplat** is the opt-in fast
trainer for Apple Silicon — 3–5× faster than Brush on M-series GPUs by using
native Metal via libtorch's MPS backend. Build it once:

```bash
# 1. Build deps (heavy, ~10–20 min one-time)
brew install cmake opencv pytorch
xcode-select --install            # if you don't have Xcode CLI tools yet

# 2. Clone + build
cd bin/
git clone --depth 1 https://github.com/pierotofy/OpenSplat.git opensplat-src
cd opensplat-src
mkdir -p build && cd build
LIBTORCH_DIR="$(brew --prefix pytorch)"
cmake -DCMAKE_PREFIX_PATH="$LIBTORCH_DIR" -DGPU_RUNTIME=MPS ..
make -j"$(sysctl -n hw.logicalcpu)"

# 3. Link into bin/
cd ../../..
ln -sf opensplat-src/build/opensplat bin/opensplat
./bin/opensplat --help            # sanity check
```

Then opt into it per-scan:

```bash
./scripts/scan-room.sh livingroom "Apt 3F" --trainer opensplat --steps 2000
```

OpenSplat's step counts are different from Brush — `--steps 2000` for
OpenSplat ≈ `--steps 7000` for Brush. The `--trainer` flag defaults to
`brush` so existing scripts keep working.

Override the binary path with `OPENSPLAT_BIN=/path/to/opensplat` if needed.

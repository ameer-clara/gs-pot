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

Installed system-wide:

```bash
brew install colmap   # Mac
# Linux:  apt install colmap   (or build from source for CUDA support)
```

The pipeline expects `colmap` on `$PATH`.

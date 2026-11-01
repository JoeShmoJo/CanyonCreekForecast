#!/usr/bin/env bash
# Optional independent decoder for the sampled NDFD complex-packed files.
# PNG/JPEG/AEC packing support and interpolation are outside this bounded probe.
set -euo pipefail
task_root="$(cd "$(dirname "$0")/.." && pwd)"
native_root="${WATERSHED_NATIVE_DIR:-$task_root/.cache/native}"
task_python="${WATERSHED_PYTHON:-python}"
mkdir -p "$native_root"
install_packages() {
  if command -v uv >/dev/null 2>&1; then
    UV_CACHE_DIR="${UV_CACHE_DIR:-$task_root/.cache/uv}" uv pip install --python "$task_python" "$@"
  else
    "$task_python" -m pip install "$@"
  fi
}
install_packages 'cmake==4.4.4'
if [ ! -d "$native_root/g2c-src" ]; then
  git clone --depth 1 --branch v2.3.0 https://github.com/NOAA-EMC/NCEPLIBS-g2c.git "$native_root/g2c-src"
fi
test "$(git -C "$native_root/g2c-src" rev-parse HEAD)" = acbccb8a894255cd30056b722781a7c9f7b1fe3c
cmake_bin="$(dirname "$(command -v "$task_python")")/cmake"
"$cmake_bin" -S "$native_root/g2c-src" -B "$native_root/g2c-build" \
  -DCMAKE_INSTALL_PREFIX="$native_root/g2c" -DBUILD_TESTING=OFF \
  -DUSE_PNG=OFF -DUSE_Jasper=OFF -DUSE_OpenJPEG=OFF -DUTILS=OFF -DBUILD_STATIC_LIBS=OFF
"$cmake_bin" --build "$native_root/g2c-build" --parallel 2
"$cmake_bin" --install "$native_root/g2c-build"
G2C_DIR="$native_root/g2c" install_packages -r "$task_root/requirements-basin.txt" \
  --config-settings=cmake.define.GRIB2IO_WITH_IP=OFF \
  --config-settings="cmake.define.CMAKE_INSTALL_RPATH=$native_root/g2c/lib"

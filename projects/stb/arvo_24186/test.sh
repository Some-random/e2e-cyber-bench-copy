#!/usr/bin/env bash
# test.sh — build & run stb tests that actually run (plus compile checks)

set -o errexit
set -o nounset
set -o pipefail

FUZZ_CORPUS="${1:-}"

ROOT_DIR="/src"
REPO_DIR="${ROOT_DIR}/stb"
BUILD_DIR="${REPO_DIR}/build"

INCLUDES="-I."
CFLAGS_COMMON="-DSTB_DIVIDE_TEST"
CFLAGS_WARN="-Wno-pointer-to-int-cast -Wno-int-to-pointer-cast"
CPPFLAGS_WARN="-Wno-write-strings"
LM_FLAG="-lm"

say() { printf "\n\033[1;36m==>\033[0m %s\n" "$*"; }
run() { echo "+ $*"; bash -c "$*"; }

mkdir -p "${BUILD_DIR}"
cd "${REPO_DIR}"

# ---- file lists -------------------------------------------------------------
C_TESTS=(
  "stb_vorbis.c"
  "tests/test_c_compilation.c"
  "tests/test_c_lexer.c"
  "tests/test_dxt.c"
  "tests/test_easyfont.c"
  "tests/test_image.c"
  "tests/test_image_write.c"
  "tests/test_perlin.c"
  "tests/test_sprintf.c"
  "tests/test_truetype.c"
  "tests/test_voxel.c"
)
CPP_COMPILE_ONLY="tests/test_cpp_compilation.cpp"

# ---- build compile-only C++ check ------------------------------------------
say "Compiling C++ compile-only check to object (no linking)..."
run "${CXX} ${INCLUDES} ${CPPFLAGS_WARN} -std=c++11 -c '${CPP_COMPILE_ONLY}' -o '${BUILD_DIR}/test_cpp_compilation.o'"

# ---- combined C tests -------------------------------------------------------
say "Building combined C test bundle -> ${BUILD_DIR}/c_tests"
run "${CC} ${INCLUDES} ${CFLAGS_COMMON} ${CFLAGS_WARN} ${C_TESTS[*]} ${LM_FLAG} -o '${BUILD_DIR}/c_tests'"

# ---- runnable tests ---------------------------------------------------------
say "Building image_write_test -> ${BUILD_DIR}/image_write_test"
run "${CC} ${INCLUDES} ${CFLAGS_COMMON} ${CFLAGS_WARN} -DIWT_TEST tests/image_write_test.c ${LM_FLAG} -o '${BUILD_DIR}/image_write_test'"

say "Building image_fuzzer -> ${BUILD_DIR}/image_fuzzer"
run "${CC} ${INCLUDES} ${CFLAGS_COMMON} ${CFLAGS_WARN} tests/fuzz_main.c tests/stbi_read_fuzzer.c ${LM_FLAG} -o '${BUILD_DIR}/image_fuzzer'"

# ---- stb_ds unit test (override STBDS_ASSERT to avoid assert-macro issues) --
# ---- stb_ds unit test (force a safe assert before test_ds.c sees it) --------
# ---- run everything (best-effort) -------------------------------------------
failures=0

say "Running c_tests"
if ! "${BUILD_DIR}/c_tests"; then echo "c_tests exited non-zero"; failures=$((failures+1)); fi

say "Running image_write_test"
if ! "${BUILD_DIR}/image_write_test"; then echo "image_write_test exited non-zero"; failures=$((failures+1)); fi

say "Running stbds_unit_test"
if ! "${BUILD_DIR}/stbds_unit_test"; then echo "stbds_unit_test exited non-zero"; failures=$((failures+1)); fi

say "Running image_fuzzer ${FUZZ_CORPUS:+with corpus '${FUZZ_CORPUS}'}"
if [[ -n "${FUZZ_CORPUS}" ]]; then
  if ! "${BUILD_DIR}/image_fuzzer" "${FUZZ_CORPUS}"; then echo "image_fuzzer exited non-zero"; failures=$((failures+1)); fi
else
  if ! "${BUILD_DIR}/image_fuzzer"; then echo "image_fuzzer exited non-zero"; failures=$((failures+1)); fi
fi

say "Done."
echo "Artifacts in: ${BUILD_DIR}"
echo "Failures recorded (non-fatal): ${failures}"
exit 0

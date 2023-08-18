#ifndef CAFFE2_CORE_COMMON_H_
#define CAFFE2_CORE_COMMON_H_

#include <algorithm>
#include <cmath>
#include <map>
#include <memory>
#include <numeric>
#include <set>
#include <sstream>
#include <string>
#include <type_traits>
#include <vector>

#ifdef __APPLE__
#include <TargetConditionals.h>
#endif

#if defined(_MSC_VER)
#include <io.h>
#else
#include <unistd.h>
#endif

// Macros used during the build of this caffe2 instance. This header file
// is automatically generated by the cmake script during build.
#include "caffe2/core/macros.h"

#include <c10/macros/Macros.h>

#include "c10/util/string_utils.h"

namespace caffe2 {

// Note(Yangqing): NVCC does not play well with unordered_map on some platforms,
// forcing us to use std::map instead of unordered_map. This may affect speed
// in some cases, but in most of the computation code we do not access map very
// often, so it should be fine for us. I am putting a CaffeMap alias so we can
// change it more easily if things work out for unordered_map down the road.
template <typename Key, typename Value>
using CaffeMap = std::map<Key, Value>;
// using CaffeMap = std::unordered_map;

// Using statements for common classes that we refer to in caffe2 very often.
// Note that we only place it inside caffe2 so the global namespace is not
// polluted.
/* using override */
using std::set;
using std::string;
using std::unique_ptr;
using std::vector;

// Just in order to mark things as not implemented. Do not use in final code.
#define CAFFE_NOT_IMPLEMENTED CAFFE_THROW("Not Implemented.")

// suppress an unused variable.
#if defined(_MSC_VER) && !defined(__clang__)
#define CAFFE2_UNUSED __pragma(warning(suppress : 4100 4101))
#define CAFFE2_USED
#else
#define CAFFE2_UNUSED __attribute__((__unused__))
#define CAFFE2_USED __attribute__((__used__))
#endif //_MSC_VER

// Define alignment macro that is cross platform
#if defined(_MSC_VER) && !defined(__clang__)
#define CAFFE2_ALIGNED(x) __declspec(align(x))
#else
#define CAFFE2_ALIGNED(x) __attribute__((aligned(x)))
#endif

#if (defined _MSC_VER && !defined NOMINMAX)
#define NOMINMAX
#endif

#if defined(__has_cpp_attribute)
#if __has_cpp_attribute(nodiscard)
#define CAFFE2_NODISCARD [[nodiscard]]
#endif
#endif
#if !defined(CAFFE2_NODISCARD)
#define CAFFE2_NODISCARD
#endif

using std::make_unique;

#if defined(__ANDROID__) && !defined(__NDK_MAJOR__)
using ::round;
#else
using std::round;
#endif // defined(__ANDROID__) && !defined(__NDK_MAJOR__)

// dynamic cast reroute: if RTTI is disabled, go to reinterpret_cast
template <typename Dst, typename Src>
inline Dst dynamic_cast_if_rtti(Src ptr) {
#ifdef __GXX_RTTI
  return dynamic_cast<Dst>(ptr);
#else
  return static_cast<Dst>(ptr);
#endif
}

// SkipIndices are used in operator_fallback_gpu.h and operator_fallback_mkl.h
// as utility functions that marks input / output indices to skip when we use a
// CPU operator as the fallback of GPU/MKL operator option.
template <int... values>
class SkipIndices {
 private:
  template <int V>
  static inline bool ContainsInternal(const int i) {
    return (i == V);
  }
  template <int First, int Second, int... Rest>
  static inline bool ContainsInternal(const int i) {
    return (i == First) || ContainsInternal<Second, Rest...>(i);
  }

 public:
  static inline bool Contains(const int i) {
    return ContainsInternal<values...>(i);
  }
};

template <>
class SkipIndices<> {
 public:
  static inline bool Contains(const int /*i*/) {
    return false;
  }
};

// HasCudaRuntime() tells the program whether the binary has Cuda runtime
// linked. This function should not be used in static initialization functions
// as the underlying boolean variable is going to be switched on when one
// loads libtorch_gpu.so.
TORCH_API bool HasCudaRuntime();
TORCH_API bool HasHipRuntime();
namespace internal {
// Sets the Cuda Runtime flag that is used by HasCudaRuntime(). You should
// never use this function - it is only used by the Caffe2 gpu code to notify
// Caffe2 core that cuda runtime has been loaded.
TORCH_API void SetCudaRuntimeFlag();
TORCH_API void SetHipRuntimeFlag();
} // namespace internal
// Returns which setting Caffe2 was configured and built with (exported from
// CMake)
TORCH_API const std::map<string, string>& GetBuildOptions();

TORCH_API bool IsHipMasqueradingAsCuda();
TORCH_API void SetHipMasqueradingAsCuda(bool value);

} // namespace caffe2

#endif // CAFFE2_CORE_COMMON_H_

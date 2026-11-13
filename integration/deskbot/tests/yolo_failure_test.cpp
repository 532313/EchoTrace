#include <cassert>
#include <cstdlib>
#include <cstring>

#include "yolov5.h"

static int fail_create_at;
static int fail_set_at;
static int fail_set_result;
static int create_calls;
static int set_calls;
static int destroyed_mem;
static int destroyed_context;
static int live_mem;
static int reported_inputs = 1;

extern "C" int rknn_init(rknn_context *context, void *, uint32_t, uint32_t,
                         rknn_init_extend *) {
  *context = 7;
  return RKNN_SUCC;
}

extern "C" int rknn_query(rknn_context, rknn_query_cmd command, void *info,
                          uint32_t) {
  if (command == RKNN_QUERY_IN_OUT_NUM) {
    auto *counts = static_cast<rknn_input_output_num *>(info);
    counts->n_input = reported_inputs;
    counts->n_output = 3;
  } else {
    auto *attr = static_cast<rknn_tensor_attr *>(info);
    const uint32_t index = attr->index;
    std::memset(attr, 0, sizeof(*attr));
    attr->index = index;
    attr->n_dims = 4;
    attr->dims[1] = 640;
    attr->dims[2] = 640;
    attr->dims[3] = 3;
    attr->fmt = RKNN_TENSOR_NHWC;
    attr->size_with_stride = 128;
  }
  return RKNN_SUCC;
}

extern "C" rknn_tensor_mem *rknn_create_mem(rknn_context, uint32_t size) {
  ++create_calls;
  if (create_calls == fail_create_at)
    return nullptr;
  auto *mem = static_cast<rknn_tensor_mem *>(
      std::calloc(1, sizeof(rknn_tensor_mem)));
  assert(mem);
  mem->virt_addr = std::calloc(1, size);
  assert(mem->virt_addr);
  ++live_mem;
  return mem;
}

extern "C" int rknn_set_io_mem(rknn_context, rknn_tensor_mem *,
                               rknn_tensor_attr *) {
  ++set_calls;
  return set_calls == fail_set_at ? fail_set_result : RKNN_SUCC;
}

extern "C" int rknn_destroy_mem(rknn_context context,
                                rknn_tensor_mem *mem) {
  assert(context == 7 && mem);
  std::free(mem->virt_addr);
  std::free(mem);
  ++destroyed_mem;
  --live_mem;
  return RKNN_SUCC;
}

extern "C" int rknn_destroy(rknn_context context) {
  assert(context == 7 && live_mem == 0);
  ++destroyed_context;
  return RKNN_SUCC;
}

extern "C" int rknn_run(rknn_context, rknn_run_extend *) {
  return RKNN_SUCC;
}

int post_process(rknn_app_context_t *, void *, float, float,
                 object_detect_result_list *) {
  return 0;
}

static void reset() {
  fail_create_at = 0;
  fail_set_at = 0;
  fail_set_result = -1;
  create_calls = 0;
  set_calls = 0;
  destroyed_mem = 0;
  destroyed_context = 0;
  live_mem = 0;
  reported_inputs = 1;
}

int main() {
  rknn_app_context_t context{};

  reset();
  fail_create_at = 1;
  assert(init_yolov5_model("unused", &context) != 0);
  assert(destroyed_mem == 0 && destroyed_context == 1 && live_mem == 0);

  reset();
  fail_create_at = 2;
  assert(init_yolov5_model("unused", &context) != 0);
  assert(destroyed_mem == 1 && destroyed_context == 1 && live_mem == 0);

  reset();
  fail_set_at = 1;
  fail_set_result = 1;
  assert(init_yolov5_model("unused", &context) != 0);
  assert(destroyed_mem == 1 && destroyed_context == 1 && live_mem == 0);

  reset();
  fail_set_at = 1;
  assert(init_yolov5_model("unused", &context) != 0);
  assert(destroyed_mem == 1 && destroyed_context == 1 && live_mem == 0);

  reset();
  reported_inputs = 2;
  assert(init_yolov5_model("unused", &context) != 0);
  assert(destroyed_mem == 0 && destroyed_context == 1 && live_mem == 0);

  reset();
  assert(init_yolov5_model("unused", &context) == 0);
  assert(live_mem == 4);
  assert(release_yolov5_model(&context) == 0);
  assert(destroyed_mem == 4 && destroyed_context == 1 && live_mem == 0);
  assert(release_yolov5_model(&context) == 0);
  assert(destroyed_mem == 4 && destroyed_context == 1);
}

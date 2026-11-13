// Adapter for Rockchip RKLLM 1.3.1, Qwen3.5 vision + W4A16 language on RK3576.
#include <QImage>
#include <QPainter>
#include <algorithm>
#include <chrono>
#include <cstring>
#include <iostream>
#include <json/json.h>
#include <memory>
#include <sstream>
#include <vector>
extern "C" {
#include "image_enc.h"
#include "rkllm.h"
#include <libswscale/swscale.h>
}
namespace {
struct Result {
  std::string text;
  bool failed = false;
};
int output(RKLLMResult *token, void *userdata, LLMCallState state) {
  auto *result = static_cast<Result *>(userdata);
  if (state == RKLLM_RUN_ERROR)
    result->failed = true;
  else if (state == RKLLM_RUN_NORMAL && token && token->text) {
    if (result->text.size() < 16000)
      result->text += token->text;
  }
  return 0;
}
struct Engine {
  LLMHandle language = nullptr;
  rknn_app_context_t vision{};
  bool vision_ready = false;
  ~Engine() {
    if (vision_ready)
      release_imgenc(&vision);
    if (language)
      rkllm_destroy(language);
  }
};
} // namespace
int main(int argc, char **argv) {
  try {
    std::string vision_path, language_path, platform = "rk3576";
    int max_tokens = 64, max_context = 2048;
    for (int i = 1; i < argc; ++i) {
      std::string a = argv[i];
      if (i + 1 >= argc)
        throw std::runtime_error("option requires a value");
      std::string v = argv[++i];
      if (a == "--vision")
        vision_path = v;
      else if (a == "--language")
        language_path = v;
      else if (a == "--platform")
        platform = v;
      else if (a == "--max-context")
        max_context = std::stoi(v);
      else if (a == "--max-tokens")
        max_tokens = std::stoi(v);
      else
        throw std::runtime_error("unknown option");
    }
    if (platform != "rk3576" || vision_path.empty() || language_path.empty() ||
        max_tokens < 1 || max_tokens > 256 || max_context < 512 ||
        max_context > 4096)
      throw std::runtime_error("invalid RK3576 Qwen3.5 configuration");
    std::string line;
    std::getline(std::cin, line);
    if (line.size() > 32768)
      throw std::runtime_error("request too large");
    Json::CharReaderBuilder builder;
    Json::Value request;
    std::string errors;
    std::istringstream stream(line);
    if (!Json::parseFromStream(builder, stream, &request, &errors))
      throw std::runtime_error("invalid JSON request");
    QImage input(QString::fromStdString(request["image"].asString()));
    if (input.isNull())
      throw std::runtime_error("image cannot be decoded");
    if (input.width() > 4096 || input.height() > 4096)
      throw std::runtime_error("oversized image");
    Engine engine;
    auto param = rkllm_createDefaultParam();
    param.model_path = language_path.c_str();
    param.top_k = 1;
    param.max_new_tokens = max_tokens;
    param.max_context_len = max_context;
    param.skip_special_token = true;
    param.extend_param.base_domain_id = 1;
    RKLLMCallback callback{};
    callback.result_callback = output;
    if (rkllm_init(&engine.language, &param, &callback) != 0)
      throw std::runtime_error("rkllm_init failed; check the sealed "
                               "runtime/model/driver combination");
    if (rkllm_set_chat_template(
            engine.language,
            "<|im_start|>system\n你根据图像进行简洁描述，不猜测不可见细节。<|"
            "im_end|>\n",
            "<|im_start|>user\n",
            "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n") != 0)
      throw std::runtime_error("Qwen3.5 chat template configuration failed");
    if (init_imgenc(vision_path.c_str(), &engine.vision, 0) != 0)
      throw std::runtime_error("RKNN image encoder initialization failed");
    engine.vision_ready = true;
    // Same square RGB pad and linear resize as the official multimodal example.
    const int side = std::max(input.width(), input.height());
    QImage square(side, side, QImage::Format_RGB888);
    square.fill(QColor(128, 128, 128));
    {
      QPainter painter(&square);
      painter.drawImage((side - input.width()) / 2, (side - input.height()) / 2,
                        input);
    }
    const int width = engine.vision.model_width,
              height = engine.vision.model_height;
    if (width != 448 || height != 448)
      throw std::runtime_error("expected the sealed 448x448 Qwen3.5 encoder");
    std::vector<uint8_t> rgb(size_t(width) * height * 3);
    auto *sw = sws_getContext(side, side, AV_PIX_FMT_RGB24, width, height,
                              AV_PIX_FMT_RGB24, SWS_BILINEAR, nullptr, nullptr,
                              nullptr);
    if (!sw)
      throw std::runtime_error("image scaler unavailable");
    const uint8_t *source[4] = {square.bits(), nullptr, nullptr, nullptr};
    int source_stride[4] = {square.bytesPerLine(), 0, 0, 0};
    uint8_t *dest[4] = {rgb.data(), nullptr, nullptr, nullptr};
    int dest_stride[4] = {width * 3, 0, 0, 0};
    sws_scale(sw, source, source_stride, 0, side, dest, dest_stride);
    sws_freeContext(sw);
    size_t count = size_t(engine.vision.model_image_token) *
                   engine.vision.model_embed_size *
                   engine.vision.io_num.n_output;
    if (!count || count > 16 * 1024 * 1024)
      throw std::runtime_error("unexpected vision embedding shape");
    std::vector<float> embeddings(count);
    if (run_imgenc(&engine.vision, rgb.data(), embeddings.data()) != 0)
      throw std::runtime_error("RKNN image inference failed");
    std::string prompt =
        "<image>" +
        request.get("prompt", "用一句中文描述可见物品；看不清就说明看不清。")
            .asString();
    if (prompt.size() > 4000)
      throw std::runtime_error("prompt too large");
    RKLLMInput in{};
    in.input_type = RKLLM_INPUT_MULTIMODAL;
    in.role = "user";
    auto &mm = in.multimodal_input;
    mm.prompt = const_cast<char *>(prompt.c_str());
    mm.image.image_embed = embeddings.data();
    mm.image.n_image_tokens = engine.vision.model_image_token;
    mm.image.n_image = 1;
    mm.image.image_start = "<|vision_start|>";
    mm.image.image_end = "<|vision_end|>";
    mm.image.image_content = "<|image_pad|>";
    mm.image.image_height = height;
    mm.image.image_width = width;
    RKLLMInferParam infer{};
    infer.mode = RKLLM_INFER_GENERATE;
    infer.keep_history = 0;
    Result result;
    if (rkllm_run(engine.language, &in, &infer, &result) != 0 || result.failed)
      throw std::runtime_error("RKLLM generation failed");
    Json::Value response;
    response["text"] = result.text;
    Json::StreamWriterBuilder writer;
    writer["indentation"] = "";
    std::cout << Json::writeString(writer, response) << std::endl;
    return 0;
  } catch (const std::exception &e) {
    std::cerr << "echotrace-vlm: " << e.what() << "\n";
    return 1;
  }
}

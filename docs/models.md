# 模型与离线运行

应用只读取本地资产。构建阶段下载权重与工具，运行阶段不调用云 ASR、聊天或 TTS 接口。`/api/status` 区分“文件存在”和“已在当前进程成功执行”。

## CPU 模型

- SenseVoiceSmall INT8：官方 sherpa-onnx 发布仓库，revision `2365baeacb507f821a0c8120fcee3d484dba7a07`。
- BGE-small-zh-v1.5：BAAI revision `7999e1d3359715c523056ef9478215996d62a620`，CLS、512 维、L2 归一化、最大 128 token。`export_bge.py` 导出 ONNX，再对 MatMul/Gemm 做动态 INT8 量化。词嵌入保留浮点，不能把整个文件都称为纯 INT8。
- Silero VAD：revision `066409f3d9db45a30d21dfcbf9147826ffdd7052`，16kHz 时每次 512 样本，保留 64 样本上下文和循环状态。采用语音区间定位派生锚点，保留原始音频与父事件。
- Matcha-icefall-zh-baker＋Vocos 22kHz：输出转为 16kHz PCM，短反馈通过带摘要校验的分块传输回 Echo。该声学模型的 Baker 数据集标注为非商业用途，当前个人学习使用；商业化前需替换为许可合适的声音模型。

```sh
python3 scripts/fetch_models.py asr vad bge-source tts
# 在安装了 PyTorch / Transformers / ONNX 的构建机上执行：
python3 scripts/export_bge.py
python3 scripts/seal_models.py
python3 scripts/seal_models.py --check
```

BGE 转换只在构建机运行；鲁班猫运行 ONNX Runtime，不安装 PyTorch。SenseVoice 官方测试音频用于运行通路验证，不能据此报告工作台领域的中文识别准确率。

当前语音笔记由连续录音的闭合短段进行 VAD 和转写，具备数秒至一个分段的等待。记录中说“查一下，为什么更换排线”可触发语音查询，问题不会反过来成为检索答案；普通口述进入过程记忆。停止记录后的查询可使用 Qt/浏览器文字入口。实时 Opus 交互与唤醒词低延迟对话仍需单独实现、验收。

## 视觉模型

Qwen3.5-0.8B 原始 revision 为 `2fc06364715b967f1860aea9cf38778875588b17`。官方 Rockchip SDK 固定为 `release-v1.3.1`，commit `f7390530443bf84f0394255a449d7cbe81e69d1c`。

```sh
python3 scripts/fetch_qwen.py
python3 scripts/fetch_rkllm.py
```

SDK 的语言转换工具要求 PyTorch 2.6.0、Transformers 5.8.0；RKNN Toolkit2 2.3.2 要求 PyTorch ≤2.4.0，因此使用两个独立转换环境，不能混装。RKNN 侧另外固定 ONNX 1.16.1，避免较新 ONNX 删除 `onnx.mapping` 导致转换失败。配置清单在 `config/conversion/`，封装脚本为 `scripts/convert_qwen.py`。ARM runtime 位于工程私有目录，以 RPATH 加载，不替换系统 NPU 库。

已准备好对应构建环境后，按顺序执行：

```sh
python3 scripts/convert_qwen.py language --python .rk-convert/bin/python
python3 scripts/convert_qwen.py vision-onnx --python .rk-convert/bin/python
python3 scripts/convert_qwen.py vision --python .rknn-convert/bin/python
```

转换产物进入 `models/assets/qwen/`，配置文件可参照 `models/config.example.json` 填入模型路径和转换版本，编译推理工作进程后再运行 `seal_models.py`。校验清单和实际部署配置必须一起保存；重新编译工作进程后也应复核其摘要。模型清单包含原始权重 revision、转换记录和运行文件 SHA-256。

视觉部分使用官方 `export_vision.py` 导出 448×448，`export_vision_rknn.py --model_name qwen3.5 --target-platform rk3576` 转换；语言部分为 RK3576、2 NPU 核、W4A16。官方多模态校准脚本生成语言输入嵌入；这些官方校准照片不属于项目评测集。

`echotrace-vlm` 读取一行 JSON `{image,prompt}`，返回一行 `{text}`；调用进程设定上下文和生成上限，最长 90 秒后终止工作进程。图像采用与官方示例对应的 RGB 方形填充、线性缩放，归一化由转换图执行。模型文件、tokenizer、预处理与运行库版本都需要一起固定。

关键帧理解是自动描述，不能用来认证接线正确、确认细小丝印或证明物品当前仍在原地。查询中的时间和证据 ID 由数据库与媒体清单计算。

## 一手资料

- [SenseVoice 官方部署](https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html)
- [BGE 官方模型](https://huggingface.co/BAAI/bge-small-zh-v1.5)
- [Silero 官方运行代码](https://github.com/snakers4/silero-vad)
- [Matcha 中文模型](https://k2-fsa.github.io/sherpa/onnx/tts/all/Chinese/matcha-icefall-zh-baker.html)
- [固定版本的 Rockchip 多模态示例](https://github.com/airockchip/rknn-llm/tree/f7390530443bf84f0394255a449d7cbe81e69d1c/examples/multimodal_model_demo)

## 实际运行记录

`docs/models-coexistence.json` 记录了同一服务中的五种模型实际执行、冷调用耗时与系统内存采样；它不是识别质量评测。首轮视觉调用约 22～24 秒。需要按锚点低频分析，后台积压时保留原件并延后描述。

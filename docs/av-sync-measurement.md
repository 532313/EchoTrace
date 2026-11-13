# 原始录制的物理声画同步测量

`scripts/evaluate_av_sync.py` 读取已有的 `echotrace-evidence-v1` 导出 ZIP，不重新封装原件。测量对象是**同一次物理动作在原始视频和音频中的来源时间差**，不包括 Qt 显示器、网络或 Echo 扬声器的回放延迟。

当前还没有人工标注的真实同步数据。自动化用例中的脉冲、画面和时间偏移均为合成素材，不计入实板验收。

## 采集与标注

1. 固定机位，让拍手接触点清晰可见，避免过曝或遮挡。进行至少一小时连续录制，在开始、中段和结束各做至少三次相互间隔的拍手。开始一组可安排在第 30–60 秒，避开带初始化不连续标记的首段。镜头与声音都必须来自被测 Echo 的原始记录。
2. 每组拍手附近建立步骤标记。会话结束后，在时间线下载对应声画片段，再导出这几条标记的证据包。保留原件和导出包，不使用剪辑、变速、降噪或重新编码的素材。
3. 解开证据包，依据 `evidence.json` 的 `files` 查到媒体 ID 对应文件。逐帧找到双手接触的第一帧；以原始 WAV 的波形确定相应拍手的音频样本位置。两者都从 **0** 开始编号。对含糊、遮挡或无法区分的拍手重新采集，不凭时间最近自动配对。
4. 将配对填入下面的 JSON。`session_id`、`device_id`、`boot_id` 和媒体 ID 均复制自证据包。真实人工标注使用 `synthetic:false` 并填写标注人；练习、算法生成或测试素材使用 `synthetic:true`。

```json
{
  "format": "echotrace-av-sync-v1",
  "synthetic": false,
  "annotator": "实际标注人",
  "session_id": "实际会话 ID",
  "device_id": "echo-mate",
  "boot_id": "本次启动 ID",
  "points": [
    {
      "id": "begin-clap-1",
      "video_media_id": "对应视频 ID",
      "video_frame_index": 42,
      "audio_media_id": "对应音频 ID",
      "audio_sample_index": 29120
    }
  ]
}
```

这里的编号只演示格式，不代表一次实际测量。如果需要逐帧查看，可在展开的证据包旁新建目录并运行：

```sh
ffmpeg -i media/实际视频文件.mp4 -fps_mode passthrough -start_number 0 frames/frame-%05d.png
```

工具内部使用 `ffprobe -show_frames` 的 `best_effort_timestamp_time`，将首个显示帧的时间归零后映射到媒体 `start_us`。它不按 `帧号 / 25` 假定视频恒定帧率；有 B 帧或非零容器起点也按实际显示顺序处理。[FFprobe 官方说明](https://ffmpeg.org/ffprobe.html)

## 计算与结果

```sh
python3 scripts/evaluate_av_sync.py --bundle evidence.zip \
  --annotations claps.json --output runtime/av-sync-report.json
```

工具先验证选中原件的大小和 SHA-256，拒绝跨会话、跨启动、重复事件、超范围帧/样本、带不连续标记的片段，以及解码器报错的输入。原始视频按显示帧定位；原始音频按 16kHz 单声道 PCM 的样本定位。

- `audio_minus_video_ms` 为音频来源时间减视频来源时间，正数表示声音较晚。
- `absolute_error_p95_ms` 使用绝对误差的 nearest-rank P95；同时保留最大值和每个测量点。
- `frame_quantization_ms` 报告该帧附近的时间间隔。25fps 时约 40ms，拍手接触边界的人工判断还有额外不确定性。
- `one_hour_coverage` 要求已结束会话持续至少 3600 秒，且前 10%、40%–60%、后 10% 三个窗口各至少三组配对。较少点仍可计算误差，但不会通过一小时门槛。
- `one_hour_sampled_200ms_gate_passed` 还要求素材明确标为非合成，且上述 P95 ≤ 200ms。此字段依赖标注真实性，不能替代对拍手画面和波形的人工复核，也不测量未采样时刻。
- `continuous_recording_verified` 始终为 false：所选证据片段不能证明整小时连续记录。还需对同一会话使用 `audit_edge_session.py` 和全量时间线检查缺口与原件完整性。

相邻媒体时长接近、时间戳连续或某帧提取位置准确，都不能代替这个物理测量。播放同步需要另行测量实际屏幕与扬声器。
